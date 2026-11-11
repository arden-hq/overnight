"""The verifiers wrap's contracts, against a fake OpenAI-compatible engine and no live model.

Overnight runs from OVERNIGHT_PYTHON (default: the repository's `.venv`). The training-client test
needs the Qwen3-0.6B tokenizer in the Hugging Face cache.
"""

import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
import verifiers.v1 as vf
from aiohttp import web
from overnight_verifiers import OvernightData, OvernightTask
from overnight_verifiers import load_environment as _load_environment
from verifiers.v1.clients import ModelContext
from verifiers.v1.utils.loaders import load_environment as vf_load_environment
from verifiers.v1.utils.loaders import resolve_env_config

REPOSITORY = Path(__file__).resolve().parents[2]
PYTHON = os.environ.get("OVERNIGHT_PYTHON") or str(REPOSITORY / ".venv/bin/python")
TOKENIZER = "Qwen/Qwen3-0.6B"
KEY_VARIABLE = "FAKE_ENGINE_KEY"
# Every reply, whoever asks: an operator answer that says nothing, and text for the agent.
REPLY = '{"message": null}'
OPERATOR = {"model": "operator/fixed"}

# A process agent that calls the policy through Overnight's provider relay, as a connector does,
# with capture off. Its one message names its episode folder, so a call shows whose it is, and
# whether the operator's key reached it.
RELAY_AGENT = """import json, os, pathlib, sys, time
import anyio, httpx
from overnight.adapters.http import serve_http
from overnight.adapters.provider import CompatibleProvider
from overnight.adapters.provider_relay import ProviderRelay, RequestBudget, run_capture

base_url, key_env, model = sys.argv[1:4]
provenance = pathlib.Path(os.environ["OVERNIGHT_PROVENANCE_PATH"])
operator_key = "operator-key=" + str("OVERNIGHT_OPERATOR_KEY" in os.environ)
provenance.write_text(json.dumps({
    "implementation": "relay-agent",
    "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
}))


async def ask():
    provider = CompatibleProvider(base_url=base_url, api_key_env=key_env)
    budget = RequestBudget(input_usd_per_million=0.2, output_usd_per_million=0.75)
    # A patient harness waits for the engine; the default client gives up after 5 seconds.
    async with httpx.AsyncClient(timeout=300 if "patient" in sys.argv[4:] else 5) as client:
        relay = ProviderRelay(
            budget, provider, model, os.environ[key_env], client, lambda record: None,
            capture=run_capture(),
        )
        async with serve_http(relay.app) as url:
            answer = await client.post(
                url + "/v1/chat/completions",
                json={"model": model, "stream": True,
                      "messages": [{"role": "user", "content": f"{provenance} {operator_key}"}]},
                headers={"Authorization": f"Bearer {relay.token}"},
            )
            answer.raise_for_status()


for count, line in enumerate(sys.stdin):
    if count == 0:
        if sys.argv[4:] == ["crash"]:
            print("not json", flush=True)
            continue
        try:
            anyio.run(ask)
        except Exception:
            # A lingering harness keeps working for a while after a failed call.
            if "linger" in sys.argv[4:]:
                time.sleep(3)
            raise
        action = {"kind": "report", "message": "Asked the model."}
    else:
        action = {"kind": "done"}
    print(json.dumps(action), flush=True)
"""


def load_environment(family: Path, **options):
    """The env with its operator seat pinned, as every config must pin it."""
    return _load_environment(family, operator=OPERATOR, **options)


def fake_engine(received: list[dict]) -> web.Application:
    """Chat completions for an eval client, and vLLM's token route for a training client."""

    async def chat(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        received.append(body)
        message = {"role": "assistant", "content": REPLY}
        if not body.get("stream"):
            return web.json_response(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                }
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for delta, finish in ((message, None), ({}, "stop")):
            chunk = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    async def generate(request: web.Request) -> web.Response:
        from transformers import AutoTokenizer

        body = await request.json()
        received.append(body)
        tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)
        ids = tokenizer.encode(REPLY, add_special_tokens=False)
        ids.append(tokenizer.convert_tokens_to_ids("<|im_end|>"))
        logprobs = [{"token": f"token_id:{i}", "logprob": -0.5, "top_logprobs": []} for i in ids]
        return web.json_response(
            {
                "request_id": "gen-fake",
                "prompt_token_ids": body["token_ids"],
                "choices": [
                    {
                        "index": 0,
                        "token_ids": ids,
                        "logprobs": {"content": logprobs},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/inference/v1/generate", generate)
    return app


async def serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, f"http://127.0.0.1:{port}"


@pytest.fixture(autouse=True)
def engine_key(monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, "engine-key")
    monkeypatch.setenv("OVERNIGHT_PYTHON", PYTHON)


@pytest.fixture(scope="module")
def family(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("families") / "order-resolution"
    command = [
        str(Path(PYTHON).parent / "overnight"),
        "generate",
        "order-resolution",
        "--count",
        "2",
    ]
    subprocess.run([*command, "--seed", "100", "--out", str(folder)], check=True)
    return folder


def relay_agent(tmp_path: Path, *mode: str) -> Path:
    script = tmp_path / "relay_agent.py"
    script.write_text(RELAY_AGENT)
    document = {
        "kind": "persistent_command",
        "command": [
            PYTHON,
            str(script),
            "{policy.base_url}",
            "{policy.api_key_env}",
            "{policy.model}",
            *mode,
        ],
    }
    path = tmp_path / f"agent{'-'.join(('', *mode))}.json"
    path.write_text(json.dumps(document))
    return path


def scripted_task() -> OvernightTask:
    scenario = json.loads((REPOSITORY / "examples/catering-chaos.json").read_text())
    return OvernightTask(OvernightData(scenario=scenario, family="examples", seed=0))


async def episodes(env, tasks, client: vf.ClientConfig) -> list[vf.Episode]:
    """Run the tasks as concurrent episodes of one env, as a trainer's env worker does."""
    ctx = ModelContext(model="policy/v7", client=client)
    async with env.serving():
        return await asyncio.gather(*(env.run_episode(task, ctx) for task in tasks))


def eval_client(url: str) -> vf.EvalClientConfig:
    return vf.EvalClientConfig(base_url=f"{url}/v1", api_key_var=KEY_VARIABLE)


def seat(episode: vf.Episode, name: str) -> vf.Trace:
    (trace,) = [trace for trace in episode.traces if trace.agent.name == name]
    return trace


def test_rows_come_from_a_family_only_when_it_is_apart_from_the_held_out_cases(family):
    rows = list(load_environment(family).taskset)
    assert [row.data.id for row in rows] == ["order-resolution-0100", "order-resolution-0101"]
    assert all(row.data.scenario["id"] == row.data.id for row in rows)

    # The same folder named as held out overlaps itself, so it loads no row.
    overlapping = load_environment(family, taskset={"held_out": [str(family)]})
    with pytest.raises(ValueError, match="split check failed"):
        list(overlapping.taskset)


def test_a_scripted_case_scores_one_on_every_rollout(family):
    async def run() -> list[vf.Episode]:
        runner, url = await serve(fake_engine([]))
        try:
            return await episodes(load_environment(family), [scripted_task()] * 2, eval_client(url))
        finally:
            await runner.cleanup()

    for episode in asyncio.run(run()):
        trace = seat(episode, "agent")
        assert episode.ok and trace.reward == 1.0
        assert trace.metrics["overnight_passed"] == 1.0


def test_policy_calls_land_on_the_trained_seat_with_token_ids(family, tmp_path):
    pytest.importorskip("transformers")
    received: list[dict] = []
    client_config = {"renderer_model_name": TOKENIZER, "api_key_var": KEY_VARIABLE}

    async def run() -> vf.Episode:
        runner, url = await serve(fake_engine(received))
        try:
            env = load_environment(family, agent=relay_agent(tmp_path))
            task = next(iter(env.taskset))
            client = vf.TrainClientConfig(base_url=f"{url}/v1", **client_config)
            (episode,) = await episodes(env, [task], client)
            return episode
        finally:
            await runner.cleanup()

    episode = asyncio.run(run())
    agent, operator = seat(episode, "agent"), seat(episode, "operator")
    assert episode.ok and agent.reward is not None
    assert agent.info["overnight"]["policy_model"] == "policy/v7"
    # The agent's one call is on its own trainable trace, sampled tokens with their logprobs.
    assert agent.agent.trainable and not operator.agent.trainable
    assert len(agent.calls) == 1
    sampled = [node for node in agent.nodes if node.sampled]
    assert sampled and all(len(node.token_ids) and len(node.logprobs) for node in sampled)
    texts = json.dumps([n.model_dump(mode="json") for n in agent.nodes])
    # The call names this episode, and the operator's secret never reached the agent.
    assert agent.info["overnight"]["episode"] in texts and "operator-key=False" in texts
    # The operator answered through its own session, which never holds the agent's call.
    assert operator.calls and all(
        call.node not in {c.node for c in agent.calls} for call in operator.calls
    )
    assert all("token_ids" in body for body in received)


def test_an_agent_failure_scores_zero_and_an_infrastructure_failure_errors(family, tmp_path):
    async def run() -> list[vf.Episode]:
        runner, url = await serve(fake_engine([]))
        try:
            crashing = load_environment(family, agent=relay_agent(tmp_path, "crash"))
            missing = tmp_path / "missing.json"
            missing.write_text(
                json.dumps(
                    {"kind": "persistent_command", "command": ["/no/agent", "{policy.model}"]}
                )
            )
            unstartable = load_environment(family, harness={"agent": str(missing)})
            client = eval_client(url)
            (crashed,) = await episodes(crashing, [scripted_task()], client)
            (failed,) = await episodes(unstartable, [scripted_task()], client)
            return [crashed, failed]
        finally:
            await runner.cleanup()

    crashed, failed = asyncio.run(run())
    trace = seat(crashed, "agent")
    assert crashed.ok and trace.reward == 0.0
    assert trace.info["overnight"]["failure"]["kind"] == "agent"
    assert not failed.ok and seat(failed, "agent").has_error


def failing_engine(status: int) -> web.Application:
    async def chat(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response({"error": {"message": f"status {status}"}}, status=status)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


def test_an_engine_fault_errors_the_rollout_and_a_refused_request_scores_zero(family, tmp_path):
    async def run(status: int) -> vf.Episode:
        runner, url = await serve(failing_engine(status))
        try:
            env = load_environment(family, agent=relay_agent(tmp_path))
            (episode,) = await episodes(env, [scripted_task()], eval_client(url))
            return episode
        finally:
            await runner.cleanup()

    # The engine failed: the episode says nothing about the policy, so a trainer drops it.
    outage = seat(asyncio.run(run(500)), "agent")
    assert outage.has_error and "overnight" not in outage.info
    # The engine refused the agent's own request: that is the agent's failure.
    refused = seat(asyncio.run(run(400)), "agent")
    assert not refused.has_error and refused.reward == 0.0
    assert refused.info["overnight"]["failure"]["kind"] == "agent"


def hanging_engine() -> web.Application:
    async def chat(request: web.Request) -> web.Response:
        await request.read()
        await asyncio.sleep(40)
        return web.json_response({"error": {"message": "too late"}}, status=500)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


def test_a_policy_call_the_engine_never_answered_errors_the_rollout(family, tmp_path):
    async def run(*mode: str) -> vf.Episode:
        runner, url = await serve(hanging_engine())
        agent = relay_agent(tmp_path, *mode)
        config = {
            "taskset": {"id": "overnight-verifiers", "family": str(family)},
            "agent": {
                "harness": {"id": "overnight-verifiers", "agent": str(agent)},
                "timeout": {"rollout": 15},
            },
            "operator": OPERATOR,
        }
        try:
            env = vf_load_environment(resolve_env_config(config))
            (episode,) = await episodes(env, [scripted_task()], eval_client(url))
            return episode
        finally:
            await runner.cleanup()

    # No engine fault is ever recorded, yet the episode says nothing about the policy, so it
    # errors instead of scoring the agent 0: the harness gave up on the call, or the agent's
    # deadline cut it.
    gave_up = seat(asyncio.run(run()), "agent")
    assert gave_up.has_error and "overnight" not in gave_up.info
    cut = seat(asyncio.run(run("patient")), "agent")
    assert cut.stop_condition == "agent_timeout" and cut.has_error


def slow_engine(seconds: float, answered: list[float]) -> web.Application:
    """The fake engine, answering each chat call `seconds` late; `answered` gets each answer's
    time once it is sent."""
    engine = fake_engine([])
    (route,) = [r for r in engine.router.routes() if r.resource.canonical == "/v1/chat/completions"]

    async def chat(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(seconds)
        response = await route.handler(request)
        answered.append(asyncio.get_running_loop().time())
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


def test_a_policy_call_the_harness_gave_up_on_errors_the_rollout_when_answered_late(
    family, tmp_path
):
    async def run() -> tuple[vf.Episode, list[float], float]:
        answered: list[float] = []
        # The agent's client gives up after 5 seconds and the agent fails 3 seconds later; the
        # engine answers at 5.5, while the episode is still running.
        runner, url = await serve(slow_engine(5.5, answered))
        try:
            env = load_environment(family, agent=relay_agent(tmp_path, "linger"))
            (episode,) = await episodes(env, [scripted_task()], eval_client(url))
            return episode, answered, asyncio.get_running_loop().time()
        finally:
            await runner.cleanup()

    episode, answered, ended = asyncio.run(run())
    trace = seat(episode, "agent")
    # The engine answered within the episode and verifiers recorded the call as answered, yet
    # the harness had already given up on it: the agent failure it caused says nothing about
    # the policy, so the rollout errors instead of scoring 0.
    assert answered and answered[0] < ended
    assert trace.calls and all(call.error is None for call in trace.calls)
    assert trace.has_error and "overnight" not in trace.info


# A process agent that stalls on its first observation, or reports to the operator on every one.
PROCESS_AGENT = """import json, os, pathlib, sys, time
pathlib.Path(os.environ["OVERNIGHT_PROVENANCE_PATH"]).write_text(json.dumps({
    "implementation": "process-agent",
    "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
}))
for count, line in enumerate(sys.stdin):
    if sys.argv[1] == "stall":
        time.sleep(300)
    action = {"kind": "report", "message": f"Update {count}."} if count < 5 else {"kind": "done"}
    print(json.dumps(action), flush=True)
"""


def process_agent(tmp_path: Path, mode: str) -> Path:
    script = tmp_path / "process_agent.py"
    script.write_text(PROCESS_AGENT)
    path = tmp_path / f"{mode}.json"
    command = [PYTHON, str(script), mode, "{policy.model}"]
    path.write_text(json.dumps({"kind": "persistent_command", "command": command}))
    return path


def test_a_policy_out_of_time_or_past_the_operator_budget_scores_zero(family, tmp_path):
    stalled = process_agent(tmp_path, "stall")
    config = resolve_env_config(
        {
            "taskset": {"id": "overnight-verifiers", "family": str(family)},
            "agent": {
                "harness": {"id": "overnight-verifiers", "agent": str(stalled)},
                "timeout": {"rollout": 5},
            },
            "operator": OPERATOR,
        }
    )

    async def run() -> list[vf.Episode]:
        runner, url = await serve(fake_engine([]))
        try:
            slow = vf_load_environment(config)
            pestering = load_environment(
                family,
                agent=process_agent(tmp_path, "pester"),
                harness={"operator_model": {"max_calls": 1}},
            )
            client = eval_client(url)
            (timed_out,) = await episodes(slow, [scripted_task()], client)
            (over_budget,) = await episodes(pestering, [next(iter(pestering.taskset))], client)
            return [timed_out, over_budget]
        finally:
            await runner.cleanup()

    timed_out, over_budget = (seat(episode, "agent") for episode in asyncio.run(run()))
    assert timed_out.stop_condition == "agent_timeout"
    assert not timed_out.has_error and timed_out.reward == 0.0
    failure = over_budget.info["overnight"]["failure"]
    assert failure["type"] == "OperatorBudgetExceeded" and failure["kind"] == "agent"
    assert not over_budget.has_error and over_budget.reward == 0.0


def test_the_operator_seat_must_name_its_model(family):
    with pytest.raises(ValueError, match="operator seat's model"):
        _load_environment(family)


def test_concurrent_rollouts_keep_their_own_calls(family, tmp_path):
    received: list[dict] = []

    async def run() -> list[vf.Episode]:
        runner, url = await serve(fake_engine(received))
        try:
            env = load_environment(family, agent=relay_agent(tmp_path))
            tasks = list(env.taskset) * 2
            return await episodes(env, tasks, eval_client(url))
        finally:
            await runner.cleanup()

    traces = [seat(episode, "agent") for episode in asyncio.run(run())]
    names = {trace.info["overnight"]["episode"] for trace in traces}
    assert len(names) == 4
    for trace in traces:
        assert trace.ok and trace.reward is not None and len(trace.calls) == 1
        # The trace's one user message names its own episode's folder and no other.
        own = trace.info["overnight"]["episode"]
        texts = json.dumps([node.model_dump(mode="json") for node in trace.nodes])
        assert own in texts and not any(other in texts for other in names - {own})


@pytest.mark.skipif(
    os.environ.get("OVERNIGHT_CONTAINER_TESTS") != "1",
    reason="needs OVERNIGHT_CONTAINER_TESTS=1, Docker and overnight-agent-base",
)
def test_a_contained_agent_reaches_its_own_interception_session(family, tmp_path):
    document = {
        "kind": "persistent_command",
        "command": [
            "python3",
            "-c",
            RELAY_AGENT,
            "{policy.base_url}",
            "{policy.api_key_env}",
            "{policy.model}",
        ],
        "container": {"image": "overnight-agent-base", "env": ["OVERNIGHT_POLICY_KEY"]},
    }
    agent = tmp_path / "contained.json"
    agent.write_text(json.dumps(document))

    async def run() -> vf.Episode:
        runner, url = await serve(fake_engine([]))
        try:
            env = load_environment(family, agent=agent)
            (episode,) = await episodes(env, [scripted_task()], eval_client(url))
            return episode
        finally:
            await runner.cleanup()

    trace = seat(asyncio.run(run()), "agent")
    assert trace.ok and len(trace.calls) == 1
