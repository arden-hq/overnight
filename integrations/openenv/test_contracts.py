import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from openenv.core.harness import TrainableSession, TrainingTrace
from overnight_openenv import OvernightSessionFactory

REPOSITORY = Path(__file__).resolve().parents[2]
PYTHON = os.environ.get("OVERNIGHT_PYTHON") or str(REPOSITORY / ".venv/bin/python")
OPERATOR_KEY = "OPERATOR_TEST_KEY"
ENGINE_KEY = "ENGINE_TEST_API_KEY"
SECRETS = (OPERATOR_KEY, ENGINE_KEY, "HF_TOKEN")
REPLY = '{"message": null}'
SAMPLED = [11, 12, 13]

RELAY_AGENT = """import json, os, pathlib, sys, time
import anyio, httpx
from overnight.adapters.http import serve_http
from overnight.adapters.provider import CompatibleProvider
from overnight.adapters.provider_relay import ProviderRelay, RequestBudget, run_capture

base_url, key_env, model, mode = sys.argv[1:5]
provenance = pathlib.Path(os.environ["OVERNIGHT_PROVENANCE_PATH"])
leaked = [name for name in SECRETS if name in os.environ]
provenance.write_text(json.dumps({
    "implementation": "relay-agent",
    "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
}))


async def ask(tag, check=True):
    provider = CompatibleProvider(base_url=base_url, api_key_env=key_env)
    budget = RequestBudget(input_usd_per_million=0.2, output_usd_per_million=0.75)
    async with httpx.AsyncClient() as client:
        relay = ProviderRelay(
            budget, provider, model, os.environ[key_env], client, lambda record: None,
            capture=run_capture(),
        )
        async with serve_http(relay.app) as url:
            answer = await client.post(
                url + "/v1/chat/completions",
                json={"model": model, "stream": True, "messages": [
                    {"role": "user", "content": f"{tag}: {provenance} leaked={leaked}"}]},
                headers={"Authorization": f"Bearer {relay.token}"},
                timeout=60,
            )
            if check:
                answer.raise_for_status()


for count, line in enumerate(sys.stdin):
    if count == 0:
        if mode != "silent-crash":
            anyio.run(ask, "agent")
        if mode == "twice":
            # A second call whose failure the harness shrugs off.
            anyio.run(ask, "again", False)
        if mode == "stall":
            time.sleep(300)
        if mode.endswith("crash"):
            print("not json", flush=True)
            continue
        action = {"kind": "report", "message": "Asked the model."}
    else:
        action = {"kind": "done"}
    print(json.dumps(action), flush=True)
""".replace("SECRETS", repr(SECRETS))


class Engine:
    def __init__(self, tokens: bool = True) -> None:
        self.tokens = tokens
        self.fail_marker = ""
        self.slow_marker = ""
        self.slow_s = 0.0
        self.bad_marker = ""
        self.received: list[dict[str, Any]] = []
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def answer(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self.answer(200, {"object": "list", "data": [{"id": "policy/v7"}]})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                engine.received.append(body)
                text = json.dumps(body["messages"])
                if engine.fail_marker and engine.fail_marker in text:
                    self.answer(500, {"error": {"message": "engine down"}})
                    return
                if engine.slow_marker and engine.slow_marker in text:
                    time.sleep(engine.slow_s)
                completion = engine.completion(body)
                if engine.bad_marker and engine.bad_marker in text and has_logprobs(completion):
                    for entry in completion["choices"][0]["logprobs"]["content"]:
                        entry["logprob"] = 0.5
                self.answer(200, completion)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def completion(self, body: dict[str, Any]) -> dict[str, Any]:
        choice: dict[str, Any] = {
            "index": 0,
            "message": {"role": "assistant", "content": REPLY},
            "finish_reason": "stop",
        }
        response = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [choice],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        }
        if self.tokens:
            response["prompt_token_ids"] = [
                100 + sum(map(ord, json.dumps(m))) % 1000 for m in body["messages"]
            ]
            choice["token_ids"] = SAMPLED
            choice["logprobs"] = {
                "content": [
                    {"token": f"token_id:{i}", "logprob": -0.5, "top_logprobs": []} for i in SAMPLED
                ]
            }
        return response

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def engines(monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("OVERNIGHT_PYTHON", PYTHON)
    policy, operator = Engine(), Engine(tokens=False)
    yield policy, operator
    policy.close()
    operator.close()


@pytest.fixture(scope="module")
def family(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("families") / "order-resolution"
    command = [str(Path(PYTHON).parent / "overnight"), "generate", "order-resolution"]
    subprocess.run([*command, "--count", "2", "--seed", "100", "--out", str(folder)], check=True)
    return folder


def agent(tmp_path: Path, mode: str = "normal") -> dict[str, Any]:
    script = tmp_path / "relay_agent.py"
    script.write_text(RELAY_AGENT)
    tokens = ["{policy.base_url}", "{policy.api_key_env}", "{policy.model}"]
    return {"kind": "persistent_command", "command": [PYTHON, str(script), *tokens, mode]}


def factory(family: Path, engines, document: dict[str, Any], **options) -> OvernightSessionFactory:
    policy, operator = engines
    operator_model = {
        "provider": {"base_url": f"{operator.url}/v1", "api_key_env": OPERATOR_KEY},
        "model": "operator/fixed",
    }
    return OvernightSessionFactory(
        family,
        llm_url=policy.url,
        model="policy/v7",
        agent=document,
        operator_model=operator_model,
        python=PYTHON,
        engine_api_key_env=ENGINE_KEY,
        sampling={"temperature": 0.7},
        **options,
    )


def has_logprobs(completion: dict[str, Any]) -> bool:
    return "logprobs" in completion["choices"][0]


class CaptureContractError(ValueError):
    """TRL's error that stops training."""


@dataclass
class Outcome:
    exit_code: int
    timed_out: bool
    reward: float | None
    trace: TrainingTrace
    result: dict[str, Any]


def trl_rollout(sessions: OvernightSessionFactory, prompt) -> Outcome | None:
    try:
        session = sessions.create(prompt, seed=0, episode_id=uuid4().hex)
    except Exception:
        return None
    try:
        if not isinstance(session, TrainableSession):
            raise CaptureContractError("loop-owning training requires fetch_training_trace()")
        exit_code = 0
        timed_out = False
        try:
            exit_code = session.wait_for_completion()
        except TimeoutError:
            timed_out = True
        try:
            trace = session.fetch_training_trace()
        except (ValueError, TypeError, KeyError) as error:
            if exit_code != 0:
                return None
            raise CaptureContractError(str(error)) from error
        reward = session.verify([]).env_reward
        return Outcome(exit_code, timed_out, reward, trace, session.result)
    except CaptureContractError:
        raise
    except Exception:
        return None
    finally:
        session.close()


def prompts(sessions: OvernightSessionFactory) -> list:
    return [row["prompt"] for row in sessions.prompt_rows()]


def texts(trace: TrainingTrace) -> str:
    return json.dumps([turn.request for turn in trace.turns])


def test_cases_load_only_apart_from_held_out_with_a_pinned_operator(family, engines, tmp_path):
    sessions = factory(family, engines, agent(tmp_path))
    assert [row["case_id"] for row in sessions.prompt_rows()] == [
        "order-resolution-0100",
        "order-resolution-0101",
    ]
    with pytest.raises(KeyError):
        sessions.create([{"role": "user", "content": "catering-chaos"}])
    with pytest.raises(ValueError, match="split check failed"):
        factory(family, engines, agent(tmp_path), held_out=[family]).cases()
    with pytest.raises(ValueError, match="fixed operator model"):
        OvernightSessionFactory(
            family, llm_url="x", model="m", agent=agent(tmp_path), operator_model={}
        )


def test_policy_calls_are_captured_with_tokens_and_the_score_is_the_reward(
    family, engines, tmp_path
):
    policy, operator = engines
    sessions = factory(family, engines, agent(tmp_path))
    try:
        outcome = trl_rollout(sessions, prompts(sessions)[0])
    finally:
        sessions.close()
    assert outcome is not None and outcome.exit_code == 0
    assert outcome.reward == outcome.result["score"] is not None
    assert outcome.result["policy_model"] == "policy/v7"
    (turn,) = outcome.trace.turns
    assert turn.completion_token_ids == SAMPLED and len(turn.per_token_logps) == 3
    assert turn.loss_mask == [0] * len(turn.prompt_token_ids) + [1, 1, 1]
    assert outcome.result["episode"] in texts(outcome.trace)
    assert "leaked=[]" in texts(outcome.trace)
    assert operator.received
    agent_calls = [body for body in policy.received if "agent: " in json.dumps(body)]
    assert len(agent_calls) == 1
    assert agent_calls[0]["temperature"] == 0.7 and agent_calls[0]["top_p"] == 1.0
    assert agent_calls[0]["return_token_ids"] is True


def test_an_agent_failure_scores_zero_and_an_unusable_episode_is_dropped(family, engines, tmp_path):
    crashed = factory(family, engines, agent(tmp_path, "crash"))
    silent = factory(family, engines, agent(tmp_path, "silent-crash"))
    unstartable = factory(
        family,
        engines,
        {
            "kind": "persistent_command",
            "command": ["/no/agent", "{policy.base_url}", "{policy.api_key_env}"],
        },
    )
    try:
        failed = trl_rollout(crashed, prompts(crashed)[0])
        assert trl_rollout(silent, prompts(silent)[0]) is None
        assert trl_rollout(unstartable, prompts(unstartable)[0]) is None
    finally:
        for sessions in (crashed, silent, unstartable):
            sessions.close()
    assert failed is not None and failed.exit_code == 1 and failed.reward == 0.0
    assert failed.result["failure"]["kind"] == "agent" and len(failed.trace.turns) == 1


def test_an_engine_fault_drops_the_episode_even_when_the_agent_recovers(family, engines, tmp_path):
    policy, _ = engines
    policy.fail_marker = "again: "
    sessions = factory(family, engines, agent(tmp_path, "twice"))
    try:
        assert trl_rollout(sessions, prompts(sessions)[0]) is None
    finally:
        sessions.close()
    assert any("again: " in json.dumps(body) for body in policy.received)


def test_an_agent_scores_zero_for_time_only_when_every_policy_call_was_answered(
    family, engines, tmp_path
):
    policy, _ = engines
    stalled = factory(family, engines, agent(tmp_path, "stall"), agent_timeout_s=10)
    policy.slow_marker, policy.slow_s = "again: ", 5.5
    waiting = factory(family, engines, agent(tmp_path, "twice"), agent_timeout_s=10)
    try:
        assert trl_rollout(waiting, prompts(waiting)[0]) is None
        policy.slow_marker = ""
        outcome = trl_rollout(stalled, prompts(stalled)[0])
    finally:
        stalled.close()
        waiting.close()
    assert outcome is not None and outcome.timed_out and outcome.reward == 0.0
    assert outcome.result["timed_out"] and len(outcome.trace.turns) == 1


def test_a_graded_episode_with_an_untrainable_capture_is_unscorable(family, engines, tmp_path):
    policy, _ = engines
    policy.bad_marker = "agent: "
    sessions = factory(family, engines, agent(tmp_path))
    try:
        assert trl_rollout(sessions, prompts(sessions)[0]) is None
    finally:
        sessions.close()


def test_concurrent_episodes_keep_their_own_calls(family, engines, tmp_path):
    sessions = factory(family, engines, agent(tmp_path))
    try:
        with ThreadPoolExecutor(4) as pool:
            outcomes = list(
                pool.map(lambda prompt: trl_rollout(sessions, prompt), prompts(sessions) * 2)
            )
    finally:
        sessions.close()
    episodes = {outcome.result["episode"] for outcome in outcomes if outcome is not None}
    assert len(episodes) == 4
    for outcome in outcomes:
        assert outcome is not None and len(outcome.trace.turns) == 1
        own = outcome.result["episode"]
        assert own in texts(outcome.trace)
        assert not any(other in texts(outcome.trace) for other in episodes - {own})
