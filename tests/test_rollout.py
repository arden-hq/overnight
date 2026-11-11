import json
import os
import subprocess
import sys
from pathlib import Path

from anyio.from_thread import start_blocking_portal
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_operator import single_lane
from test_provider_relay import MODEL, engine

from overnight.adapters.http import serve_http
from overnight.adapters.rollout import RolloutRequest, RolloutWorker
from overnight.execution.scenario import load_json

ROOT = Path(__file__).resolve().parents[1]

RELAY_AGENT = """import json, os, pathlib, sys
import anyio, httpx
from overnight.adapters.http import serve_http
from overnight.adapters.provider import CompatibleProvider
from overnight.adapters.provider_relay import ProviderRelay, RequestBudget, run_capture

base_url, key_env, model = sys.argv[1:4]
pathlib.Path(os.environ["OVERNIGHT_PROVENANCE_PATH"]).write_text(json.dumps({
    "implementation": "relay-agent",
    "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
}))


async def ask():
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
                json={"model": model, "messages": [{"role": "user", "content": "go"}],
                      "stream": True},
                headers={"Authorization": f"Bearer {relay.token}"},
            )
            answer.raise_for_status()


for count, line in enumerate(sys.stdin):
    if count == 0:
        anyio.run(ask)
        action = {"kind": "report", "message": "Asked the model."}
    else:
        action = {"kind": "done"}
    print(json.dumps(action), flush=True)
"""


def operator_endpoint(models: list[str]) -> Starlette:

    async def completions(request: Request) -> JSONResponse:
        models.append(json.loads(await request.body())["model"])
        return JSONResponse(
            {
                "model": "local/operator-served",
                "choices": [{"message": {"content": '{"message": null}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        )

    return Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])


def test_a_scripted_case_scores_the_same_on_every_rollout(tmp_path):
    worker = RolloutWorker(tmp_path / "rollouts")
    request = RolloutRequest(scenario=load_json(ROOT / "examples/catering-chaos.json"))
    first = worker.rollout(request)
    second = worker.rollout(request.model_copy(update={"id": "again"}))

    assert first.score == second.score == 1.0
    assert first.verdict == second.verdict == "passed"
    assert first.captures == second.captures == []
    assert not list((tmp_path / "rollouts").iterdir())


def test_each_rollout_returns_only_its_own_captured_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_KEY", "engine-key")
    monkeypatch.setenv("OPERATOR_KEY", "operator-key")
    received: list[dict] = []
    operator_models: list[str] = []
    agent = tmp_path / "relay_agent.py"
    agent.write_text(RELAY_AGENT)
    with start_blocking_portal() as portal:
        with (
            portal.wrap_async_context_manager(serve_http(engine(received))) as engine_url,
            portal.wrap_async_context_manager(
                serve_http(operator_endpoint(operator_models))
            ) as operator_url,
        ):
            request = RolloutRequest.model_validate(
                {
                    "scenario": single_lane(),
                    "agent": {
                        "kind": "persistent_command",
                        "command": [
                            sys.executable,
                            str(agent),
                            "{policy.base_url}",
                            "{policy.api_key_env}",
                            "{policy.model}",
                        ],
                    },
                    "policy": {
                        "provider": {"base_url": f"{engine_url}/v1", "api_key_env": "ENGINE_KEY"},
                        "model": MODEL,
                    },
                    "operator_model": {
                        "provider": {
                            "base_url": f"{operator_url}/v1",
                            "api_key_env": "OPERATOR_KEY",
                        },
                        "model": "local/operator",
                    },
                }
            )
            worker = RolloutWorker(tmp_path / "rollouts", keep=True)
            rollouts = [
                worker.rollout(request.model_copy(update={"id": name}))
                for name in ("first", "second")
            ]

    assert len(received) == 2 and all(body["return_token_ids"] for body in received)
    for number, rollout in enumerate(rollouts, start=1):
        assert rollout.score is not None
        (capture,) = rollout.captures
        (call,) = capture.calls
        assert call.generation_id == f"gen-{number}"
        assert call.prompt_token_ids == [1, 2, 3, 4] and call.completion_token_ids == [7, 8, 9]
        assert (tmp_path / "rollouts" / rollout.id / "agent" / capture.source).is_file()
        operator = rollout.provenance["operator"]
        assert operator["configured_model"] == "local/operator"
        assert operator["model"] == "local/operator-served"
        assert operator["settings"]["base_url"] == f"{operator_url}/v1"
    assert operator_models and set(operator_models) == {"local/operator"}


TAMPERING_AGENT = """import json, os, pathlib, sys

mode = sys.argv[1]
provenance = pathlib.Path(os.environ["OVERNIGHT_PROVENANCE_PATH"])
provenance.write_text(json.dumps({
    "implementation": "tampering-agent",
    "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
}))
forged = {"request": 1, "generation_id": "forged", "prompt_token_ids": [1],
          "completion_token_ids": [2], "logprobs": "not_returned", "completion_logprobs": None,
          "finish_reason": "stop"}
if mode == "forge":
    (provenance.parent / "planted.capture.jsonl").write_text(json.dumps(forged) + "\\n")
if mode == "malformed":
    pathlib.Path(os.environ["OVERNIGHT_CAPTURE_PATH"]).write_text("not a captured call\\n")
for line in sys.stdin:
    print("not json" if mode == "crash" else json.dumps({"kind": "done"}), flush=True)
"""


def test_rollouts_ignore_planted_captures_and_tell_agent_from_infrastructure_failures(tmp_path):
    agent = tmp_path / "tampering_agent.py"
    agent.write_text(TAMPERING_AGENT)
    worker = RolloutWorker(tmp_path / "rollouts")
    scenario = load_json(ROOT / "examples/catering-chaos.json")

    def rollout(mode: str):
        command = [sys.executable, str(agent), mode]
        return worker.rollout(
            RolloutRequest(
                id=mode,
                scenario=scenario,
                agent={"kind": "persistent_command", "command": command},
            )
        )

    forged = rollout("forge")
    assert forged.captures == [] and forged.failure is None and forged.score is not None

    malformed = rollout("malformed")
    assert malformed.captures == []
    assert malformed.failure is not None and malformed.failure.kind == "infrastructure"

    crashed = rollout("crash")
    assert crashed.verdict == "invalid" and crashed.score is None
    assert crashed.failure is not None and crashed.failure.kind == "agent"


PEEKING_AGENT = """import json, os, subprocess, sys
report, names = sys.argv[1], sys.argv[2:]
pids, pid = [], os.getppid()
while pid > 1:
    pids.append(pid)
    parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
    pid = int(parent.stdout.strip() or 1)
seen = {name: name in os.environ for name in names}
for pid in pids:
    shown = subprocess.run(["ps", "eww", "-p", str(pid)], capture_output=True, text=True).stdout
    seen = {name: seen[name] or f"{name}=" in shown for name in names}
with open(report, "w") as file:
    json.dump(seen, file)
for line in sys.stdin:
    print(json.dumps({"kind": "done"}), flush=True)
"""


def test_a_clean_worker_keeps_trainer_secrets_from_its_agents_and_their_ancestors(tmp_path):
    agent = tmp_path / "peeking_agent.py"
    agent.write_text(PEEKING_AGENT)
    report = tmp_path / "seen.json"
    names = ["TRAINER_SECRET", "EPISODE_KEY"]
    request = RolloutRequest(
        scenario=load_json(ROOT / "examples/catering-chaos.json"),
        agent={
            "kind": "persistent_command",
            "command": [sys.executable, str(agent), str(report), *names],
        },
    )
    command = [sys.executable, "-m", "overnight.adapters.rollout", "--root", str(tmp_path / "r")]
    command += ["--clean-env", "--pass-env", "EPISODE_KEY"]
    environ = os.environ | {"TRAINER_SECRET": "trainer", "EPISODE_KEY": "episode"}
    done = subprocess.run(
        command,
        input=request.model_dump_json() + "\n",
        capture_output=True,
        text=True,
        env=environ,
        timeout=120,
        check=True,
    )
    assert json.loads(done.stdout)["failure"] is None
    assert json.loads(report.read_text()) == {"TRAINER_SECRET": False, "EPISODE_KEY": True}


UNLAUNCHABLE_FACTORY = """from overnight.adapters.config import Factory
from overnight.execution.harness import Capabilities


async def prepare(context):
    raise RuntimeError("no native runtime here")


adapter = Factory(capabilities=Capabilities(), prepare=prepare)
"""
NO_REQUIREMENTS = {"simulated_clock": False, "chat_isolation": False, "cancel": False}


def test_one_host_agent_document_keeps_each_episodes_evidence_in_that_episode(
    tmp_path, monkeypatch
):
    (tmp_path / "unlaunchable.py").write_text(UNLAUNCHABLE_FACTORY)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"factory": "unlaunchable:adapter", "evidence": "native"})
    agent = {
        "kind": "harness",
        "run_on_host": True,
        "required": NO_REQUIREMENTS,
        "command": [sys.executable, "-m", "overnight.adapters.acp", "--config-json", config],
    }
    worker = RolloutWorker(tmp_path / "rollouts", keep=True)
    scenario = load_json(ROOT / "examples/catering-chaos.json")
    for name in ("first", "second"):
        worker.rollout(RolloutRequest(id=name, scenario=scenario, agent=agent))
        native = tmp_path / "rollouts" / name / "agent" / "connector-generation-0" / "native"
        assert (native / "native.jsonl").is_file()
    assert not (tmp_path / "native").exists()


def test_a_harness_that_ends_before_it_is_ready_is_infrastructure(tmp_path):
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import json, sys\n"
        "mode = sys.argv[1]\n"
        "for line in sys.stdin:\n"
        "    if mode == 'start' or json.loads(line)['kind'] != 'hello':\n"
        "        sys.exit(1)\n"
        "    print(json.dumps({'kind': 'ready', 'version': 1, 'capabilities': {}}), flush=True)\n"
    )
    worker = RolloutWorker(tmp_path / "rollouts")
    scenario = load_json(ROOT / "examples/catering-chaos.json")

    def failure(mode: str):
        agent = {
            "kind": "harness",
            "run_on_host": True,
            "required": NO_REQUIREMENTS,
            "command": [sys.executable, str(harness), mode],
        }
        rollout = worker.rollout(RolloutRequest(id=mode, scenario=scenario, agent=agent))
        assert rollout.verdict == "invalid" and rollout.failure is not None
        return rollout.failure.kind

    assert failure("start") == "infrastructure"
    assert failure("turn") == "agent"


TURN_ENDING_AGENT = RELAY_AGENT.replace("            answer.raise_for_status()\n", "").replace(
    '"content": "go"', '"content": sys.argv[4]'
)


def test_a_policy_call_the_engine_did_not_answer_drops_the_episode(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_KEY", "engine-key")
    agent = tmp_path / "turn_ending_agent.py"
    agent.write_text(TURN_ENDING_AGENT)
    worker = RolloutWorker(tmp_path / "rollouts")
    scenario = load_json(ROOT / "examples/catering-chaos.json")
    with start_blocking_portal() as portal:
        for token_ids, said in ((False, "go"), (True, "overloaded")):
            with portal.wrap_async_context_manager(serve_http(engine([], token_ids))) as url:
                command = [sys.executable, str(agent), "{policy.base_url}"]
                command += ["{policy.api_key_env}", "{policy.model}", said]
                rollout = worker.rollout(
                    RolloutRequest.model_validate(
                        {
                            "scenario": scenario,
                            "agent": {"kind": "persistent_command", "command": command},
                            "policy": {
                                "provider": {"base_url": f"{url}/v1", "api_key_env": "ENGINE_KEY"},
                                "model": MODEL,
                            },
                        }
                    )
                )
            assert rollout.captures == []
            assert rollout.failure is not None and rollout.failure.kind == "infrastructure"
            assert rollout.failure.type == "PolicyCallUnanswered"
