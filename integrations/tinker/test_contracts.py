import asyncio
import json
import os
import socket
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import tinker
from aiohttp import web
from overnight_tinker import Case, EpisodeConfig, OvernightDatasetBuilder, OvernightEnvGroupBuilder
from tinker_cookbook.rl.data_processing import trajectory_to_data
from tinker_cookbook.rl.rollout_strategy import FailFast, MinViableGroup, RolloutStrategy
from tinker_cookbook.rl.rollouts import do_group_rollout_and_filter_constant_reward
from tinker_cookbook.rl.types import TrajectoryGroup
from tinker_cookbook.tokenizer_utils import get_tokenizer

REPOSITORY = Path(__file__).resolve().parents[2]
PYTHON = os.environ.get("OVERNIGHT_PYTHON") or str(REPOSITORY / ".venv/bin/python")
MODEL = "Qwen/Qwen3-0.6B"
OPERATOR_KEY = "FAKE_OPERATOR_KEY"
REPLY = '{"message": null}'

RELAY_AGENT = """import json, os, pathlib, sys, time
import anyio, httpx
from overnight.adapters.http import serve_http
from overnight.adapters.provider import CompatibleProvider
from overnight.adapters.provider_relay import ProviderRelay, RequestBudget, run_capture

base_url, key_env, model = sys.argv[1:4]
provenance = pathlib.Path(os.environ["OVERNIGHT_PROVENANCE_PATH"])
leaked = [name for name in ("FAKE_OPERATOR_KEY", "TINKER_API_KEY") if name in os.environ]
provenance.write_text(json.dumps({
    "implementation": "relay-agent",
    "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
}))


async def ask():
    provider = CompatibleProvider(base_url=base_url, api_key_env=key_env)
    budget = RequestBudget(input_usd_per_million=0.2, output_usd_per_million=0.75)
    # A patient harness waits for the sampler; the default client gives up after 5 seconds.
    async with httpx.AsyncClient(timeout=300 if "patient" in sys.argv[4:] else 5) as client:
        relay = ProviderRelay(
            budget, provider, model, os.environ[key_env], client, lambda record: None,
            capture=run_capture(),
        )
        async with serve_http(relay.app) as url:
            answer = await client.post(
                url + "/v1/chat/completions",
                json={"model": model, "stream": True,
                      "messages": [{"role": "user", "content": f"{provenance} leaked={leaked}"}]}
                | ({"presence_penalty": 0.5} if sys.argv[4:] == ["penalty"] else {}),
                headers={"Authorization": f"Bearer {relay.token}"},
            )
            answer.raise_for_status()


for count, line in enumerate(sys.stdin):
    if count == 0:
        anyio.run(ask)
        if sys.argv[4:] == ["stall"]:
            time.sleep(300)
        if sys.argv[4:] == ["crash"]:
            print("not json", flush=True)
            continue
        action = {"kind": "report", "message": "Asked the model."}
    else:
        action = {"kind": "done"}
    print(json.dumps(action), flush=True)
"""


@dataclass
class Sampled:
    tokens: list[int]
    logprobs: list[float]
    stop_reason: str = "stop"
    sequence_id: str | None = None


@dataclass
class Sample:
    sequences: list[Sampled]
    prompt_cache_hit_tokens: int = 0


class FakeSamplingClient:
    def __init__(self, fail: bool = False, delay: float = 0.0) -> None:
        tokenizer = get_tokenizer(MODEL)
        self.reply = tokenizer.encode(REPLY, add_special_tokens=False)
        self.reply.append(tokenizer.convert_tokens_to_ids("<|im_end|>"))
        self.prompts: list[tinker.ModelInput] = []
        self.fail = fail
        self.delay = delay

    async def sample(self, prompt, num_samples, sampling_params) -> Sample:
        self.prompts.append(prompt)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("engine unavailable")
        return Sample([Sampled(list(self.reply), [-0.5] * len(self.reply))])


def operator_engine(received: list[dict]) -> web.Application:
    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        received.append(body)
        return web.json_response(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": REPLY},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setenv(OPERATOR_KEY, "operator-key")
    monkeypatch.setenv("TINKER_API_KEY", "tinker-key")
    monkeypatch.setenv("OVERNIGHT_PYTHON", PYTHON)


@pytest.fixture(scope="module")
def family(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("families") / "order-resolution"
    command = [str(Path(PYTHON).parent / "overnight"), "generate", "order-resolution"]
    subprocess.run([*command, "--count", "2", "--seed", "100", "--out", str(folder)], check=True)
    return folder


@pytest.fixture(scope="module")
def operator_url():
    received: list[dict] = []
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(operator_engine(received))
    loop.run_until_complete(runner.setup())
    port = free_port()
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
    import threading

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1", received
    loop.call_soon_threadsafe(loop.stop)
    thread.join()


def files(tmp_path: Path, operator_url: str, *mode: str) -> tuple[Path, Path]:
    script = tmp_path / "relay_agent.py"
    script.write_text(RELAY_AGENT)
    command = [PYTHON, str(script), "{policy.base_url}", "{policy.api_key_env}"]
    agent = tmp_path / f"agent{'-'.join(('', *mode))}.json"
    agent.write_text(
        json.dumps({"kind": "persistent_command", "command": [*command, "{policy.model}", *mode]})
    )
    operator = tmp_path / "operator.json"
    provider = {"base_url": operator_url, "api_key_env": OPERATOR_KEY}
    operator.write_text(json.dumps({"provider": provider, "model": "operator/fixed"}))
    return agent, operator


def builder(family: Path, tmp_path: Path, operator_url: str, *mode: str, **options):
    agent, operator = files(tmp_path, operator_url, *mode)
    dataset = OvernightDatasetBuilder(
        family=str(family),
        agent=str(agent),
        operator_model=str(operator),
        model_name=MODEL,
        renderer_name="qwen3",
        root=str(tmp_path / "episodes"),
        **options,
    )
    return dataset


def rollout(
    group: OvernightEnvGroupBuilder, client: FakeSamplingClient, strategy: RolloutStrategy
) -> TrajectoryGroup | None:
    return asyncio.run(
        do_group_rollout_and_filter_constant_reward(
            client,
            group,
            max_tokens=64,
            temperature=1.0,
            do_remove_constant_reward_groups=False,
            enable_logging=False,
            strategy=strategy,
        )
    )


def groups(dataset: OvernightDatasetBuilder) -> list[OvernightEnvGroupBuilder]:
    loaded, _ = asyncio.run(dataset())
    return list(loaded.get_batch(0))  # type: ignore[arg-type]


def test_cases_load_only_apart_from_the_held_out_cases_and_with_a_named_operator(
    family, tmp_path, operator_url, monkeypatch
):
    url, _ = operator_url
    loaded = groups(builder(family, tmp_path, url, group_size=2))
    assert [group.case.id for group in loaded] == ["order-resolution-0100", "order-resolution-0101"]

    with pytest.raises(ValueError, match="split check failed"):
        groups(builder(family, tmp_path, url, held_out=(str(family),)))

    monkeypatch.delenv(OPERATOR_KEY)
    with pytest.raises(ValueError, match=OPERATOR_KEY):
        groups(builder(family, tmp_path, url))


def test_policy_calls_become_the_trajectory_and_the_score_its_reward(
    family, tmp_path, operator_url
):
    url, operator_calls = operator_url
    before = len(operator_calls)
    (group, _) = groups(builder(family, tmp_path, url, group_size=2))
    made: list = []
    make_envs = group.make_envs

    async def recorded():
        envs = await make_envs()
        made.extend(envs)
        return envs

    group.make_envs = recorded
    client = FakeSamplingClient()
    result = rollout(group, client, FailFast())

    assert result is not None and len(client.prompts) == 2
    tokenizer = get_tokenizer(MODEL)
    episodes = [env.episode for env in made]
    assert len(set(episodes)) == 2
    for trajectory, total, env in zip(
        result.trajectories_G, result.get_total_rewards(), made, strict=True
    ):
        (transition,) = trajectory.transitions
        assert transition.ac.tokens == client.reply
        assert transition.ac.logprobs == [-0.5] * len(client.reply)
        (datum,) = trajectory_to_data(trajectory, 1.0)
        assert -0.5 in datum.loss_fn_inputs["logprobs"].tolist()
        assert transition.episode_done and transition.metrics["overnight/agent_failure"] == 0.0
        assert env.result is not None and total == env.result["score"]
        prompt = tokenizer.decode(transition.ob.to_ints())
        assert env.episode in prompt and not any(e in prompt for e in set(episodes) - {env.episode})
        assert "leaked=[]" in prompt
    assert len(operator_calls) > before
    assert not any((tmp_path / "episodes").iterdir())


def test_an_agent_failure_scores_zero_and_an_ungraded_episode_is_dropped(
    family, tmp_path, operator_url
):
    url, _ = operator_url
    (crashing, _) = groups(builder(family, tmp_path, url, "crash", group_size=1))
    crashed = rollout(crashing, FakeSamplingClient(), FailFast())
    assert crashed is not None and crashed.get_total_rewards() == [0.0]
    (transition,) = crashed.trajectories_G[0].transitions
    assert transition.ac.tokens and transition.metrics["overnight/agent_failure"] == 1.0

    drop = MinViableGroup(min_successful=1, max_retries=0)
    missing = builder(family, tmp_path, url, group_size=1)
    (unstartable, _) = groups(missing)
    unstartable = OvernightEnvGroupBuilder(
        replace(
            unstartable.config,
            agent={"kind": "persistent_command", "command": ["/no/agent", "{policy.model}"]},
        ),
        unstartable.case,
        1,
    )
    assert rollout(unstartable, FakeSamplingClient(), drop) is None
    (refused, _) = groups(builder(family, tmp_path, url, "penalty", group_size=1))
    assert rollout(refused, FakeSamplingClient(), drop) is None
    (outage, _) = groups(builder(family, tmp_path, url, group_size=1))
    assert rollout(outage, FakeSamplingClient(fail=True), drop) is None
    assert not any((tmp_path / "episodes").iterdir())


def test_an_episode_scores_zero_for_time_only_when_every_policy_call_was_answered(
    family, tmp_path, operator_url
):
    url, _ = operator_url
    drop = MinViableGroup(min_successful=1, max_retries=0)
    (gave_up, _) = groups(builder(family, tmp_path, url, group_size=1))
    assert rollout(gave_up, FakeSamplingClient(delay=8), drop) is None
    timed = {"group_size": 1, "rollout_timeout_seconds": 12}
    (waiting, _) = groups(builder(family, tmp_path, url, "patient", **timed))
    assert rollout(waiting, FakeSamplingClient(delay=30), drop) is None
    (stalled, _) = groups(builder(family, tmp_path, url, "stall", **timed))
    result = rollout(stalled, FakeSamplingClient(), drop)
    assert result is not None and result.get_total_rewards() == [0.0]
    (transition,) = result.trajectories_G[0].transitions
    assert transition.ac.tokens and transition.metrics["stop/rollout_timeout"] == 1.0
    assert not any((tmp_path / "episodes").iterdir())


def test_a_case_without_policy_calls_scores_without_tokens(tmp_path):
    scenario = json.loads((REPOSITORY / "examples/catering-chaos.json").read_text())
    config = EpisodeConfig(
        python=PYTHON,
        model_name=MODEL,
        operator_model={"provider": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "X"}},
        renderer_name="qwen3",
        root=str(tmp_path),
    )
    group = OvernightEnvGroupBuilder(config, Case("catering-chaos", "examples", 0, scenario), 2)
    client = FakeSamplingClient()
    result = rollout(group, client, FailFast())
    assert result is not None and result.get_total_rewards() == [1.0, 1.0]
    assert not client.prompts
    assert all(trajectory_to_data(t, 1.0) == [] for t in result.trajectories_G)
