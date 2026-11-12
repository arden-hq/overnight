import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from click.testing import CliRunner
from pydantic import JsonValue, ValidationError
from starlette.applications import Starlette

from overnight.adapters import workbench as workbench_module
from overnight.adapters.workbench import WorkbenchConfig, create_app, scenario_bytes
from overnight.cli import cli
from overnight.domain.contracts import Checks
from overnight.domain.values import JsonObject
from overnight.execution.adapters import ScriptedAdapter
from overnight.execution.run_view import RunArtifacts, build_run_detail
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import load_json, resolve_scenario

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class Client:
    def __init__(self, app: Starlette) -> None:
        self.app = app

    def request(self, method: str, path: str, **options: Any) -> httpx.Response:
        async def fetch() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.request(method, path, **options)

        return anyio.run(fetch)

    def get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: JsonValue) -> httpx.Response:
        return self.request("POST", path, json=body)


def _entry(line_sequence: int, kind: str, **fields: JsonValue) -> JsonObject:
    return {"sequence": line_sequence, "time": 0, "kind": kind, "payload": {}, **fields}


def test_checks_join_verdict_by_layer_index_with_labels_and_evidence():
    checks: JsonObject = {
        "outcomes": [
            {
                "label": "Order cancelled",
                "op": "eq",
                "path": ["services", "shop", "state"],
                "value": 1,
            },
            {"op": "eq", "path": ["results", "call", "status"], "value": "succeeded"},
            {
                "source": "trace",
                "op": "before",
                "first": {"kind": "invocation"},
                "then": {"kind": "result"},
            },
        ],
        "collateral": [{"op": "unchanged", "path": ["services", "mail", "state"]}],
        "forbidden": [{"label": "Never buys", "match": {"kind": "invocation"}}],
    }
    trace: list[tuple[int, JsonValue]] = [
        (1, _entry(0, "invocation", invocation_id="call")),
        (2, _entry(1, "state_change", invocation_id="call", payload={"service_id": "shop"})),
        (3, _entry(2, "result", invocation_id="call", payload={"status": "succeeded"})),
        (4, _entry(3, "state_change", payload={"service_id": "shop"})),
        (5, _entry(4, "observation", invocation_id="call")),
    ]
    verdict: JsonObject = {
        "verdict": "failed",
        "outcomes": [True, False, True],
        "collateral": [True],
        "forbidden_trace_sequences": [[0]],
    }
    detail = build_run_detail(
        "run", RunArtifacts(scenario={"id": "s", "checks": checks}, trace=trace, verdict=verdict)
    )

    rows = [(c.layer, c.index, c.text, c.passed, c.evidence_lines) for c in detail.checks]
    assert rows == [
        ("outcome", 0, "Order cancelled", True, [4]),
        ("outcome", 1, 'results.call.status = "succeeded"', False, [1, 2, 3]),
        ("outcome", 2, "trace: kind=invocation before kind=result", True, [1, 3]),
        ("collateral", 0, "services.mail.state unchanged", True, []),
        ("forbidden", 0, "Never buys", False, [1]),
    ]
    assert detail.checks[0].predicate == "services.shop.state = 1"
    lanes = {item.line: (item.lane, item.row, item.view) for item in detail.timeline}
    assert lanes[2] == ("services", "shop", False)
    assert lanes[3] == ("tools", "call", False)
    assert lanes[5] == ("views", "views", True)


def test_check_label_is_strict_display_only():
    with pytest.raises(ValidationError):
        Checks.model_validate({"outcomes": [{"label": " ", "op": "exists", "path": ["x"]}]})
    labelled = Checks.model_validate(
        {"forbidden": [{"label": "No buys", "match": {"kind": "invocation"}}]}
    )
    assert labelled.forbidden[0].match == {"kind": "invocation"}


def test_historical_scenario_projects_leniently():
    old: JsonObject = {
        "id": "old",
        "agent": {"kind": "retired-kind"},
        "checks": {"outcomes": [{"op": "eq", "path": ["services", "a", "state"], "value": 2}]},
    }
    detail = build_run_detail(
        "old-run",
        RunArtifacts(
            scenario=old,
            trace=[(1, _entry(0, "legacy_kind", payload="not-an-object")), (3, ["odd"])],
            verdict={"outcomes": ["yes"]},
        ),
    )
    assert detail.schema_current is False and detail.schema_error
    assert [(c.text, c.passed) for c in detail.checks] == [("services.a.state = 2", None)]
    assert [(item.line, item.lane, item.kind) for item in detail.timeline] == [
        (1, "system", "legacy_kind"),
        (3, "system", "unknown"),
    ]


@pytest.fixture
def workbench(tmp_path: Path) -> tuple[Client, Path]:
    runs = tmp_path / "runs"
    run = runs / "group" / "catering"
    run.parent.mkdir(parents=True)
    result = CliRunner().invoke(
        cli, ["run", str(EXAMPLES / "catering-chaos.json"), "--out", str(run)]
    )
    assert result.exit_code == 0, result.output
    (run / "arden.db").write_text("private", encoding="utf-8")
    outside = tmp_path / "outside"
    shutil.copytree(run, outside)
    (runs / "linked").symlink_to(outside, target_is_directory=True)
    (run / "memory.json").symlink_to(outside / "verdict.json")

    scenarios = tmp_path / "scenarios"
    (scenarios / "fixtures").mkdir(parents=True)
    shutil.copy(EXAMPLES / "catering-chaos.json", scenarios / "catering-chaos.json")
    shutil.copy(EXAMPLES / "catering-overrides.json", scenarios / "catering-overrides.json")
    shutil.copy(EXAMPLES / "shared-world-chaos.json", scenarios / "fixtures" / "hidden.json")
    app = create_app(WorkbenchConfig(runs_roots=[runs], scenarios_roots=[scenarios]))
    return Client(app), run


def test_api_lists_and_projects_saved_evidence(workbench: tuple[Client, Path]):
    client, _ = workbench
    scenarios = client.get("/api/scenarios").json()["scenarios"]
    assert [(s["id"], s["valid"]) for s in scenarios] == [("catering-chaos", True)]
    assert scenarios[0]["last_run"]["id"] == "0~group~catering"
    detail = client.get("/api/scenarios/catering-chaos").json()
    assert {op["name"] for op in detail["services"][3]["operations"]} >= {"cancel_order"}

    runs = client.get("/api/runs", params={"scenario": "catering-chaos"}).json()["runs"]
    assert [run["id"] for run in runs] == ["0~group~catering"]
    run = client.get("/api/runs/0~group~catering").json()
    assert run["verdict"] == "passed"
    assert run["checks"][0] == {
        **run["checks"][0],
        "label": "Catering order cancelled",
        "passed": True,
    }
    trace = client.get("/api/runs/0~group~catering/trace", params={"kind": "fault"}).json()
    assert trace["matched"] == 1
    line = trace["records"][0]["line"]
    entry = client.get(f"/api/runs/0~group~catering/entries/{line}").json()
    assert entry["entry"]["kind"] == "fault"


def test_last_run_ignores_newer_override_runs_and_marks_them(workbench: tuple[Client, Path]):
    client, run = workbench
    variant = run.parent / "variant"
    result = CliRunner().invoke(
        cli,
        [
            "run",
            str(EXAMPLES / "catering-chaos.json"),
            "--overrides",
            str(EXAMPLES / "catering-overrides.json"),
            "--out",
            str(variant),
        ],
    )
    assert result.exit_code in {0, 1, 2}, result.output
    runs = {r["id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert (
        runs["0~group~variant"]["has_overrides"] and not runs["0~group~catering"]["has_overrides"]
    )
    scenarios = client.get("/api/scenarios").json()["scenarios"]
    assert scenarios[0]["last_run"]["id"] == "0~group~catering"


def test_api_serves_only_allowlisted_artifacts_and_refuses_traversal(
    workbench: tuple[Client, Path],
):
    client, run = workbench
    secret = run.parent / "secret.json"
    secret.write_text('{"private": true}', encoding="utf-8")
    (run / "variation.json").symlink_to(secret)
    base = "/api/runs/0~group~catering"
    assert client.get(f"{base}/artifacts/variation.json").status_code == 404
    assert client.get(f"{base}/artifacts/verdict.json").json()["verdict"] == "passed"
    for name in ("arden.db", "memory.json"):
        response = client.get(f"{base}/artifacts/{name}")
        assert response.status_code == 404
        assert response.json()["code"] == "not_served"
        assert "private" not in response.text
    for path in (
        "/api/runs/0~..~outside",
        "/api/runs/%2e%2e",
        "/api/runs/0~linked",
        f"{base}/artifacts/..%2Farden.db",
        "/api/scenarios/fixtures~hidden",
    ):
        response = client.get(path)
        assert response.status_code in {400, 404}, path
        assert set(response.json()) == {"code", "message", "details"}


def test_save_checks_etag_and_contract_and_writes_losslessly(tmp_path: Path):
    original = (EXAMPLES / "catering-chaos.json").read_bytes()
    assert scenario_bytes(json.loads(original), json.loads(original)) == original
    root = tmp_path / "scenarios"
    root.mkdir()
    path = root / "catering-chaos.json"
    path.write_bytes(original)
    client = Client(create_app(WorkbenchConfig(runs_roots=[], scenarios_roots=[root])))
    base = "/api/scenarios/catering-chaos"
    etag = client.get(base).json()["summary"]["etag"]

    def put(document: JsonValue, tag: str) -> httpx.Response:
        return client.request("PUT", base, json={"scenario": document}, headers={"If-Match": tag})

    edited = json.loads(original)
    edited["checks"]["outcomes"][0]["label"] = "Order cancelled (edited)"
    stale = put(edited, "0" * 64)
    assert stale.status_code == 409 and stale.json()["details"] == {"etag": etag}
    broken = json.loads(original)
    broken["events"][0]["invocation"]["operation"] = "no_such_operation"
    for response in (put(broken, etag), client.post(f"{base}/validate", {"scenario": broken})):
        assert response.status_code == 422 and "no_such_operation" in response.json()["message"]
    assert path.read_bytes() == original
    assert client.post(f"{base}/validate", {"scenario": edited}).json()["valid"] is True

    saved = put(edited, etag)
    assert saved.status_code == 200, saved.text
    assert json.loads(path.read_bytes()) == edited
    assert saved.json()["checks"]["checks"][0]["label"] == "Order cancelled (edited)"
    reverted = json.loads(original)
    reverted["faults"][0]["probability"] = 1
    reverted = dict(reversed(list(reverted.items())))
    assert put(reverted, saved.json()["summary"]["etag"]).status_code == 200
    assert path.read_bytes() == original
    assert not [item for item in root.iterdir() if item.name != "catering-chaos.json"]


def _wait_for(client: Client, operation: str, state: str) -> JsonObject:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        view = client.get(f"/api/operations/{operation}").json()
        if view["state"] == state:
            return view
        time.sleep(0.1)
    raise AssertionError(f"operation never reached {state}: {view}")


def _command_scenario(scenarios: Path, scenario_id: str, command: list[str]) -> None:
    document = json.loads((EXAMPLES / "catering-chaos.json").read_text(encoding="utf-8"))
    document["id"] = scenario_id
    document["agent"] = {"kind": "command", "command": command, "timeout_seconds": 120}
    (scenarios / f"{scenario_id}.json").write_text(json.dumps(document), encoding="utf-8")


def test_only_scripted_runs_start_without_allow_live(tmp_path: Path):
    scenarios, runs = tmp_path / "scenarios", tmp_path / "runs"
    scenarios.mkdir()
    for name in ("catering-chaos", "catering-harness"):
        shutil.copy(EXAMPLES / f"{name}.json", scenarios / f"{name}.json")
    _command_scenario(scenarios, "mail-command", ["python3", "-c", "pass"])
    client = Client(create_app(WorkbenchConfig(runs_roots=[runs], scenarios_roots=[scenarios])))
    for key, kind in (("catering-harness", "harness"), ("mail-command", "command")):
        refused = client.post("/api/runs", {"scenario": key})
        assert refused.status_code == 403 and refused.json()["code"] == "live_refused", key
        assert refused.json()["details"]["agent_kind"] == kind
    assert not runs.exists() and client.get("/api/status").json()["operation"] is None

    started = client.post("/api/runs", {"scenario": "catering-chaos"})
    assert started.status_code == 202, started.text
    frozen = Path(started.json()["command"][2])
    assert frozen != scenarios / "catering-chaos.json"
    assert frozen.read_bytes() == (scenarios / "catering-chaos.json").read_bytes()
    done = _wait_for(client, started.json()["id"], "finished")
    assert done["exit_code"] == 0
    deadline = time.monotonic() + 10
    while client.get(f"/api/operations/{done['id']}").json()["ended"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert not frozen.parent.exists()


def test_runs_start_one_at_a_time_and_stop_as_incomplete(tmp_path: Path):
    scenarios, runs = tmp_path / "scenarios", tmp_path / "runs"
    scenarios.mkdir()
    shutil.copy(EXAMPLES / "catering-chaos.json", scenarios / "catering-chaos.json")
    _command_scenario(
        scenarios, "slow-command", [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    config = WorkbenchConfig(runs_roots=[runs], scenarios_roots=[scenarios], allow_live=True)
    client = Client(create_app(config))

    cross_site = client.request(
        "POST",
        "/api/runs",
        content='{"scenario": "catering-chaos"}',
        headers={"content-type": "text/plain"},
    )
    assert cross_site.status_code == 403 and client.get("/api/status").json()["operation"] is None
    escape = client.post("/api/runs", {"scenario": "catering-chaos", "variation": "0~..~x"})
    assert escape.status_code in {400, 404}

    started = client.post("/api/runs", {"scenario": "slow-command"})
    assert started.status_code == 202, started.text
    operation = started.json()["id"]
    assert client.post("/api/runs", {"scenario": "catering-chaos"}).status_code == 409
    assert client.get("/api/status").json()["operation"]["id"] == operation
    deadline = time.monotonic() + 30
    while not client.get(f"/api/operations/{operation}").json()["run_id"]:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    stopped = client.post(f"/api/operations/{operation}/stop", {}).json()
    assert stopped["stop_requested"] is True
    view = _wait_for(client, operation, "stopped")
    listed = {run["id"]: run for run in client.get("/api/runs").json()["runs"]}
    assert listed[view["run_id"]]["complete"] is False
    assert client.get(f"/api/runs/{view['run_id']}").json()["verdict"] is None

    finished = client.post("/api/runs", {"scenario": "catering-chaos"}).json()
    done = _wait_for(client, finished["id"], "finished")
    assert done["exit_code"] == 0 and client.get("/api/status").json()["operation"] is None
    assert client.get(f"/api/runs/{done['run_id']}").json()["verdict"] == "passed"


def _alive_group(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_stop_kills_an_agent_that_ignores_sigterm_after_the_run_stalls(tmp_path: Path):
    scenarios, runs, pidfile = tmp_path / "scenarios", tmp_path / "runs", tmp_path / "agent.pid"
    scenarios.mkdir()
    _command_scenario(
        scenarios, "stubborn", ["sh", "-c", f"trap '' TERM; echo $$ > {pidfile}; sleep 60"]
    )
    app = create_app(
        WorkbenchConfig(runs_roots=[runs], scenarios_roots=[scenarios], allow_live=True)
    )
    app.state.workbench.operations.stop_grace = 0.5
    client = Client(app)
    operation = client.post("/api/runs", {"scenario": "stubborn"}).json()["id"]
    deadline = time.monotonic() + 30
    while not pidfile.exists() or not pidfile.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    agent = int(pidfile.read_text())
    run = app.state.workbench.operations.get(operation).process.pid
    assert os.getpgid(agent) == agent != run

    try:
        client.post(f"/api/operations/{operation}/stop", {})
        _wait_for(client, operation, "stopped")
        deadline = time.monotonic() + 10
        while _alive_group(agent) or _alive_group(run):
            assert time.monotonic() < deadline, "the agent session outlived the stopped run"
            time.sleep(0.05)
    finally:
        for group in (agent, run):
            if _alive_group(group):
                os.killpg(group, signal.SIGKILL)


def test_serve_sigterm_terminates_the_run_in_progress(tmp_path: Path):
    scenarios, runs, pidfile = tmp_path / "scenarios", tmp_path / "runs", tmp_path / "pid"
    scenarios.mkdir()
    slow = json.loads((EXAMPLES / "catering-chaos.json").read_text(encoding="utf-8"))
    code = f"import os, sys; open({str(pidfile)!r}, 'w').write(str(os.getpid())); sys.stdin.read()"
    slow["agent"] = {
        "kind": "command",
        "command": [sys.executable, "-c", code],
        "timeout_seconds": 120,
    }
    (scenarios / "slow.json").write_text(json.dumps(slow), encoding="utf-8")
    serve = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "overnight.cli",
            "serve",
            "--port",
            "0",
            "--runs-root",
            str(runs),
            "--scenarios-root",
            str(scenarios),
            "--allow-live",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert serve.stdout is not None
        url = json.loads(serve.stdout.readline())["url"]
        started = httpx.post(f"{url}/api/runs", json={"scenario": "slow"}, headers={"origin": url})
        assert started.status_code == 202, started.text
        deadline = time.monotonic() + 30
        while not pidfile.exists() or not pidfile.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.1)
        agent = int(pidfile.read_text())
        serve.terminate()
        serve.wait(timeout=30)
        while True:
            try:
                os.kill(agent, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "agent command outlived serve"
            time.sleep(0.1)
    finally:
        if serve.poll() is None:
            serve.kill()
            serve.wait()


def test_compare_aligns_checks_and_finds_first_divergence(workbench: tuple[Client, Path]):
    client, run = workbench
    twin = run.parent / "twin"
    shutil.copytree(run, twin, symlinks=True)
    same = client.get("/api/compare", params={"a": "0~group~catering", "b": "0~group~twin"})
    assert same.json()["divergence"] is None
    assert not [check for check in same.json()["checks"] if check["differs"]]

    rows = [json.loads(line) for line in (twin / "trace.jsonl").read_text().splitlines()]
    target = next(i for i, row in enumerate(rows) if row["kind"] == "result")
    rows[target]["payload"]["status"] = "failed"
    (twin / "trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    verdict = json.loads((twin / "verdict.json").read_text())
    verdict["outcomes"][0] = False
    (twin / "verdict.json").write_text(json.dumps(verdict))

    diff = client.get("/api/compare", params={"a": "0~group~catering", "b": "0~group~twin"}).json()
    divergence = diff["divergence"]
    assert divergence["fields"] == ["result"]
    assert divergence["a"]["line"] == divergence["b"]["line"] == target + 1
    assert [check["index"] for check in diff["checks"] if check["differs"]] == [0]


def test_compare_marks_checks_whose_definitions_changed(workbench: tuple[Client, Path]):
    client, run = workbench
    twin = run.parent / "twin"
    shutil.copytree(run, twin, symlinks=True)
    scenario = json.loads((twin / "resolved-scenario.json").read_text())
    scenario["checks"]["outcomes"][0]["label"] = "Relabelled only"
    scenario["checks"]["collateral"][0]["path"] = ["services", "elsewhere"]
    (twin / "resolved-scenario.json").write_text(json.dumps(scenario))

    rows = client.get("/api/compare", params={"a": "0~group~catering", "b": "0~group~twin"})
    changed = [(c["layer"], c["index"]) for c in rows.json()["checks"] if c["definition_changed"]]
    assert changed == [("collateral", 0)]


def test_local_guard_refuses_other_hosts_and_cross_origin_writes(tmp_path: Path):
    client = Client(create_app(WorkbenchConfig(runs_roots=[], scenarios_roots=[tmp_path])))
    for host in ("evil.example", "evil.example:4174", "localhost.evil.example", "[::2]:4174"):
        response = client.request("GET", "/api/status", headers={"host": host})
        assert response.status_code == 403 and response.json()["code"] == "forbidden", host
    for host in ("localhost:4174", "127.0.0.1", "[::1]:4174", "[::1]"):
        assert client.request("GET", "/api/status", headers={"host": host}).status_code == 200

    def write(origin: str) -> httpx.Response:
        return client.request("POST", "/api/runs", json={}, headers={"origin": origin})

    for origin in ("http://evil.example", "http://localhost.evil.example:5173", "null"):
        assert write(origin).status_code == 403, origin
    for origin in ("http://localhost:5173", "http://[::1]:5173", "http://127.0.0.1:4174"):
        assert write(origin).status_code == 400, origin


def test_body_limit_checks_declared_length_and_streamed_bytes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(workbench_module, "MAX_BODY_BYTES", 64)
    client = Client(create_app(WorkbenchConfig(runs_roots=[], scenarios_roots=[tmp_path])))
    declared = client.request(
        "POST",
        "/api/runs",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": "65"},
    )
    assert declared.status_code == 413

    async def chunks():
        for _ in range(10):
            yield b" " * 16

    streamed = client.request(
        "POST", "/api/runs", content=chunks(), headers={"content-type": "application/json"}
    )
    assert streamed.status_code == 413


def test_run_detail_reports_why_a_run_failed() -> None:
    artifacts = RunArtifacts(
        scenario={"id": "x", "agent": {"kind": "command"}, "checks": {}},
        verdict={
            "verdict": "invalid",
            "run_status": "invalid",
            "rejected_actions": 2,
            "failure": {"kind": "agent", "retryable": True, "type": "T", "message": "died"},
        },
    )

    detail = build_run_detail("run", artifacts)

    assert detail.failure is not None and detail.failure.kind == "agent"
    assert detail.failure.message == "died"
    assert detail.rejected_actions == 2


def test_a_run_in_progress_is_readable_and_its_trace_ends_as_one_write_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runs = tmp_path / "runs"
    runs.mkdir()
    client = Client(create_app(WorkbenchConfig(runs_roots=[runs], scenarios_roots=[])))
    seen: list[tuple[int, object, list[str]]] = []
    respond = ScriptedAdapter.respond

    def respond_after_reading_the_run(self: ScriptedAdapter, observation: JsonObject):
        run = client.get("/api/runs/0~live").json()
        seen.append((len(run["timeline"]), run["verdict"], run["issues"]))
        return respond(self, observation)

    monkeypatch.setattr(ScriptedAdapter, "respond", respond_after_reading_the_run)
    scenario = EXAMPLES / "catering-chaos.json"
    result = CliRunner().invoke(cli, ["run", str(scenario), "--out", str(runs / "live")])
    assert result.exit_code == 0, result.output

    lengths = [length for length, _, _ in seen]
    assert lengths == sorted(lengths) and len(set(lengths)) > 2
    assert all(verdict is None and not issues for _, verdict, issues in seen)
    finished = client.get("/api/runs/0~live").json()
    assert finished["verdict"] == "passed" and len(finished["timeline"]) > lengths[-1]

    trace = runs / "live" / "trace.jsonl"
    monkeypatch.undo()
    written_at_once = "".join(
        json.dumps(entry, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        for entry in run_scenario(resolve_scenario(load_json(scenario))).trace
    )
    assert trace.read_bytes() == written_at_once.encode("utf-8")
    assert trace.stat().st_mode & 0o777 == 0o600

    with trace.open("ab") as output:
        output.write(
            '{"sequence": 999, "kind": "invocation", "payload": {"note": "caf\xc3'.encode("latin-1")
        )
    torn = client.get("/api/runs/0~live").json()
    assert torn["issues"] == [] and len(torn["timeline"]) == len(finished["timeline"])
    assert client.get("/api/runs/0~live/trace", params={"kind": "fault"}).json()["matched"] == 1


def test_a_run_whose_trace_cannot_be_written_leaves_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def full_disk(self: object, entry: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("overnight.cli._TraceFile._write", full_disk)
    out = tmp_path / "run"
    result = CliRunner().invoke(
        cli, ["run", str(EXAMPLES / "catering-chaos.json"), "--out", str(out)]
    )
    assert result.exit_code != 0
    assert not (out / "verdict.json").exists()
