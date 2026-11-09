import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from overnight.adapters import suite_runner
from overnight.adapters.evidence import UsageMeter
from overnight.adapters.suite_runner import (
    WATCH_SECONDS,
    attempt_outcome,
    read_report,
    resume_suite,
    run_suite,
)
from overnight.domain.contracts import FaultSpec
from overnight.domain.values import JsonObject
from overnight.domain.vocabulary import RunStatus
from overnight.execution import evaluation
from overnight.execution.adapters import (
    AgentBehaviorError,
    ContractError,
    ManagedAdapter,
    ScriptedAdapter,
    describe_failure,
)
from overnight.execution.qa import check_scenario, qa_exit_code
from overnight.execution.runtime import Trial, run_scenario
from overnight.execution.scenario import load_json, resolve_scenario
from overnight.execution.suite import Suite, plan_suite
from overnight.execution.suite_report import (
    ArmSpend,
    AttemptOutcome,
    SuiteReport,
    pass_all,
    pass_any,
    render_report,
    rerun_flip,
    summarize_suite,
    swap_flip,
)
from overnight.execution.usage import UNMEASURED, ProcessUsage, read_usage, usage_path
from overnight.execution.variations import VariationConfig

EXAMPLES = Path(__file__).parent.parent / "examples"

COUNTER_AGENT = """
import json, pathlib, sys
sys.stdin.readline()
path = pathlib.Path(sys.argv[1])
count = int(path.read_text()) + 1 if path.exists() else 1
path.write_text(str(count))
if count % 2:
    message = "ok" if count % 4 == 1 else "no"
    print(json.dumps({"kind": "report", "message": message}))
else:
    print(json.dumps({"kind": "done"}))
"""


FLAKY_AGENT = """
import json, pathlib, sys
sys.stdin.readline()
path = pathlib.Path(sys.argv[1])
first = not path.exists()
path.write_text("seen")
print(json.dumps({"kind": "report", "message": "ok"} if first else {"kind": "done"}))
"""


def scenario(agent: dict[str, object]) -> dict[str, object]:
    return {
        "id": "report-case",
        "start_time": 0,
        "horizon": 10,
        "max_steps": 4,
        "actors": [
            {"id": "operator", "role": "operator"},
            {"id": "agent", "role": "agent", "read_services": ["mail"]},
        ],
        "services": [{"id": "mail", "kind": "mail", "state": {"messages": []}}],
        "agent": agent,
        "operator": {"policy": {"default": "deny", "rules": []}, "steps": []},
        "checks": {
            "outcomes": [
                {"source": "agent_report", "op": "eq", "path": ["message"], "value": "ok"}
            ],
            "forbidden": [],
            "collateral": [],
        },
    }


def suite_of(arms: list[str], cases: int, attempts: int = 3) -> Suite:
    return Suite.model_validate(
        {
            "id": "suite",
            "attempts": attempts,
            "arms": [{"id": arm} for arm in arms],
            "cases": [{"id": f"case-{index}", "scenario": "x.json"} for index in range(cases)],
            "resamples": 500,
        }
    )


def report_for(arms: dict[str, list[list[AttemptOutcome]]]) -> SuiteReport:
    cases = len(next(iter(arms.values())))
    attempts = len(next(iter(arms.values()))[0])
    suite = suite_of(list(arms), cases, attempts)
    return summarize_suite(
        suite,
        {
            (arm, f"case-{index}"): outcomes
            for arm, cells in arms.items()
            for index, outcomes in enumerate(cells)
        },
    )


def test_time_per_attempt_counts_only_attempts_that_ran():
    suite = suite_of(["a"], cases=1, attempts=4)
    outcomes = {("a", "case-0"): ["passed", "failed", "missing", "missing"]}

    report = summarize_suite(suite, outcomes, spend={"a": ArmSpend(seconds=10.0)})

    assert report.arms[0].spend.seconds_per_attempt == 5.0


def test_reliability_formulas_match_hand_computed_values():
    assert rerun_flip([1, 1, 0]) == pytest.approx(2 / 3)
    assert rerun_flip([1, 1, 1]) == 0
    assert rerun_flip([1]) is None
    assert swap_flip([1, 0], [1, 1]) == pytest.approx(0.5)
    assert pass_all([1, 1, 0, 1], 3) == pytest.approx(1 / 4)
    assert pass_any([1, 0, 0, 0], 3) == pytest.approx(3 / 4)


def test_unknown_is_a_scored_attempt_that_never_passes():
    report = report_for({"arm": [["passed", "unknown", "failed"]]})

    assert report.arms[0].mean == pytest.approx(1 / 3)
    assert report.arms[0].counts.unknown == 1
    assert report.complete


def test_crashing_on_failures_cannot_beat_an_honest_arm():
    report = report_for(
        {
            "honest": [["passed", "passed", "failed"]] * 2,
            "crasher": [["passed", "invalid", "invalid"]] * 2,
        }
    )
    crasher = next(arm for arm in report.arms if arm.arm_id == "crasher")

    assert report.leader == "honest"
    assert not report.complete
    assert not crasher.ranked and crasher.versus_leader is None
    assert crasher.mean is None and crasher.pass_all is None and crasher.interval is None
    assert crasher.counts.invalid == 4


def test_pairing_separates_a_consistent_gain_that_case_spread_hides():
    base: list[list[AttemptOutcome]] = [["passed"] * 3] * 10 + [["failed"] * 3] * 10
    better: list[list[AttemptOutcome]] = [["passed"] * 3] * 15 + [["failed"] * 3] * 5
    report = report_for({"base": base, "better": better})
    behind = next(arm for arm in report.arms if arm.arm_id == "base")

    assert report.leader == "better"
    assert behind.versus_leader is not None and not behind.versus_leader.tied
    assert behind.versus_leader.interval[1] < 0
    assert behind.interval is not None and report.arms[1].interval is not None
    assert behind.interval[1] > report.arms[1].interval[0]


def test_too_few_cases_tie_instead_of_showing_a_certain_gap():
    report = report_for(
        {
            "better": [["passed"] * 3] * 4,
            "worse": [["passed", "passed", "failed"]] * 4,
        }
    )
    worse = report.arms[1]

    assert worse.interval is None
    assert worse.versus_leader is not None and worse.versus_leader.tied
    assert worse.versus_leader.interval is None
    assert worse.versus_leader.difference == pytest.approx(-1 / 3)


def test_attempt_noise_keeps_a_constant_gap_from_looking_certain():
    report = report_for(
        {
            "better": [["passed"] * 3] * 5,
            "worse": [["passed", "passed", "failed"]] * 5,
        }
    )

    comparison = report.arms[1].versus_leader
    assert comparison is not None and comparison.interval is not None
    assert comparison.interval[0] < comparison.interval[1]


def test_identical_arms_tie_and_the_report_is_deterministic():
    cells: list[list[AttemptOutcome]] = [["passed", "failed", "passed"], ["failed"] * 3] * 4
    first = report_for({"one": cells, "two": cells})
    second = report_for({"one": cells, "two": cells})

    assert first == second
    comparison = first.arms[1].versus_leader
    assert comparison is not None and comparison.tied and comparison.difference == 0


def write_suite(root: Path, agent: dict[str, object], attempts: int = 2) -> Path:
    (root / "scenario.json").write_text(json.dumps(scenario(agent)))
    path = root / "suite.json"
    path.write_text(
        json.dumps(
            {
                "id": "suite",
                "attempts": attempts,
                "arms": [
                    {"id": "authored"},
                    {"id": "quit", "agent": {"kind": "scripted", "actions": [{"kind": "done"}]}},
                ],
                "cases": [{"id": "only", "scenario": "scenario.json"}],
                "resamples": 200,
            }
        )
    )
    return path


def test_arms_run_from_frozen_inputs_and_the_report_recomputes_from_evidence(tmp_path):
    (tmp_path / "agent.py").write_text(COUNTER_AGENT)
    agent = {
        "kind": "command",
        "command": [sys.executable, str(tmp_path / "agent.py"), str(tmp_path / "count")],
    }
    path = write_suite(tmp_path, agent)
    plan = plan_suite(path)
    (tmp_path / "scenario.json").write_text("broken after planning")
    out = tmp_path / "out"

    report = run_suite(plan, json.loads(path.read_text()), out, lambda *_: None)

    authored, quit_arm = report.arms
    assert [case.outcomes for case in authored.cases] == [["passed", "failed"]]
    assert authored.rerun_flip == 1
    assert authored.mean == 0.5
    assert quit_arm.mean == 0
    assert report.complete
    assert read_report(out) == report
    assert json.loads((out / "report.json").read_text())["leader"] == "authored"
    with pytest.raises(FileExistsError):
        run_suite(plan, json.loads(path.read_text()), out, lambda *_: None)


def test_invalid_pairing_fails_before_any_attempt(tmp_path):
    path = write_suite(tmp_path, {"kind": "scripted", "actions": [{"kind": "done"}]})
    document = json.loads(path.read_text())
    document["arms"][1]["agent"]["actions"] = [
        {"kind": "invoke", "id": "x", "service_id": "ghost", "operation": "y"}
    ]
    path.write_text(json.dumps(document))

    with pytest.raises(ValueError):
        plan_suite(path)


def run_with_agent(root: Path, agent: dict[str, object]) -> SuiteReport:
    path = write_suite(root, agent)
    return run_suite(plan_suite(path), json.loads(path.read_text()), root / "out", lambda *_: None)


def test_a_crashing_agent_is_retried_and_never_scored(tmp_path):
    (tmp_path / "agent.py").write_text("import sys; sys.exit(7)")
    report = run_with_agent(
        tmp_path, {"kind": "command", "command": [sys.executable, str(tmp_path / "agent.py")]}
    )

    authored = report.arms[0]
    assert not authored.ranked and authored.mean is None
    assert authored.counts.agent_failed == 2 and authored.counts.retries == 4
    assert not report.complete
    verdict = json.loads((tmp_path / "out/authored/only/attempt-1/verdict.json").read_text())
    assert verdict["verdict"] == "invalid"
    assert verdict["failure"] | {"message": None} == {
        "kind": "agent",
        "retryable": True,
        "type": "AgentBehaviorError",
        "message": None,
    }


def test_a_setup_that_cannot_work_is_not_retried(tmp_path):
    report = run_with_agent(
        tmp_path, {"kind": "command", "command": ["/nonexistent/agent"], "timeout_seconds": 5}
    )

    authored, quit_arm = report.arms
    assert authored.counts.invalid == 2 and authored.counts.retries == 0
    assert not authored.ranked and authored.mean is None
    assert report.leader == "quit" and quit_arm.ranked


class ScriptedFailure(ManagedAdapter):
    def respond(self, observation: JsonObject) -> JsonObject:
        raise OSError("connection lost")


def test_infrastructure_failure_is_invalid_and_retryable():
    scenario_config = resolve_scenario(scenario({"kind": "scripted", "actions": []}))

    verdict = run_scenario(scenario_config, ScriptedFailure()).verdict

    assert verdict["verdict"] == "invalid"
    assert verdict["failure"]["kind"] == "infrastructure"
    assert verdict["failure"]["retryable"] is True


SEND = {
    "kind": "invoke",
    "id": "send",
    "service_id": "mail",
    "operation": "send_message",
    "args": {"to": ["agent"], "subject": "s", "body": "b"},
}


class FailsAfterSending(ManagedAdapter):
    def __init__(self) -> None:
        self.sent = False

    def respond(self, observation: JsonObject) -> JsonObject:
        if self.sent:
            raise OSError("connection lost")
        self.sent = True
        return SEND


def mail_scenario() -> dict[str, object]:
    document = scenario({"kind": "scripted", "actions": []})
    document["actors"][1]["operations"] = {"mail": ["send_message"]}
    document["operator"]["policy"]["default"] = "allow"
    document["checks"] = {
        "outcomes": [],
        "forbidden": [{"source": "trace", "match": {"kind": "invocation"}}],
        "collateral": [],
    }
    return document


def test_a_forbidden_act_is_a_fail_even_if_the_run_fails_afterwards():
    result = run_scenario(resolve_scenario(mail_scenario()), FailsAfterSending())

    assert result.verdict["verdict"] == "failed"
    assert result.verdict["failure"]["kind"] == "infrastructure"


def test_collateral_damage_is_a_fail_even_if_the_run_fails_afterwards():
    document = mail_scenario()
    document["checks"] = {
        "outcomes": [],
        "forbidden": [],
        "collateral": [
            {
                "op": "eq",
                "path": ["services", "mail", "state", "messages"],
                "value": [],
            }
        ],
    }

    result = run_scenario(resolve_scenario(document), FailsAfterSending())

    assert result.verdict["verdict"] == "failed"


def test_a_forbidden_task_check_survives_the_episode_rollup():
    document = mail_scenario()
    checks = document["checks"]
    document["checks"] = {"outcomes": [], "forbidden": [], "collateral": []}
    document["tasks"] = [{"id": "task", "chat_id": "chat", "message": "go", "checks": checks}]

    result = run_scenario(resolve_scenario(document), adapters={"chat": FailsAfterSending()})

    assert result.verdict["verdict"] == "failed"


def test_infrastructure_failure_outranks_an_agent_failure_but_not_the_reverse():
    trial = Trial(
        resolve_scenario(scenario({"kind": "command", "command": ["x"]})), ManagedAdapter()
    )

    trial.record_error(AgentBehaviorError("crashed"), "runtime_error")
    assert trial.failure["kind"] == "agent"
    trial.record_error(OSError("lost"), "cleanup_error")
    assert trial.failure["kind"] == "infrastructure"
    trial.record_error(AgentBehaviorError("again"), "runtime_error")
    assert trial.failure["kind"] == "infrastructure"
    assert trial.status == RunStatus.INVALID


@pytest.mark.parametrize("code", [126, 127])
def test_a_command_that_could_not_run_is_not_retried(tmp_path, code):
    (tmp_path / "agent.py").write_text(f"import sys; sys.exit({code})")
    report = run_with_agent(
        tmp_path, {"kind": "command", "command": [sys.executable, str(tmp_path / "agent.py")]}
    )

    authored = report.arms[0]
    assert authored.counts.invalid == 2 and authored.counts.retries == 0
    assert not authored.ranked


def test_a_file_the_system_cannot_execute_is_a_setup_failure(tmp_path):
    binary = tmp_path / "agent.bin"
    binary.write_bytes(b"\x00\x01not a program")
    binary.chmod(0o755)

    report = run_with_agent(tmp_path, {"kind": "command", "command": [str(binary)]})

    assert report.arms[0].counts.invalid == 2 and report.arms[0].counts.retries == 0


def test_a_host_failure_to_spawn_is_retryable_infrastructure(monkeypatch):
    async def out_of_processes(*_args: object, **_kwargs: object) -> None:
        raise BlockingIOError(errno.EAGAIN, "out of processes")

    monkeypatch.setattr("anyio.open_process", out_of_processes)
    document = scenario({"kind": "command", "command": ["/bin/true"]})

    verdict = run_scenario(resolve_scenario(document)).verdict

    assert verdict["verdict"] == "invalid"
    assert verdict["failure"]["retryable"] is True


def test_a_broken_message_id_belongs_to_the_agent(tmp_path):
    (tmp_path / "agent.py").write_text(
        "import json; input(); print(json.dumps({'kind': 'done', 'message_id': 3}))"
    )
    report = run_with_agent(
        tmp_path, {"kind": "command", "command": [sys.executable, str(tmp_path / "agent.py")]}
    )

    assert report.arms[0].counts.agent_failed == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "fly"},
        {"kind": "wait", "until": 0},
        {**SEND, "id": "first"},
    ],
    ids=["unknown-action", "past-wait", "reused-id"],
)
def test_a_rejected_action_returns_to_the_agent_and_the_run_continues(bad):
    document = mail_scenario()
    document["checks"] = {"outcomes": [], "forbidden": [], "collateral": []}
    adapter = ScriptedAdapter([{**SEND, "id": "first"}, bad, {"kind": "done"}])

    result = run_scenario(resolve_scenario(document), adapter)

    assert result.verdict["failure"] is None
    assert result.verdict["rejected_actions"] == 1
    assert result.status == "done"
    rejected = next(entry for entry in result.trace if entry["kind"] == "action_rejected")
    assert rejected["payload"]["error"]


def test_a_rejected_action_in_an_episode_stays_with_its_task():
    document = mail_scenario()
    document["checks"] = {"outcomes": [], "forbidden": [], "collateral": []}
    document["tasks"] = [{"id": "task", "chat_id": "chat", "message": "go"}]
    seen: list[JsonObject] = []

    class Recording(ScriptedAdapter):
        def respond(self, observation: JsonObject) -> JsonObject:
            seen.append(observation)
            return super().respond(observation)

    run_scenario(
        resolve_scenario(document),
        adapters={"chat": Recording([{"kind": "fly"}, {"kind": "done"}])},
    )

    assert seen[-1]["receipts"][-1]["status"] == "failed"


def test_failure_kind_and_retry_are_independent_of_agent_type():
    crash = describe_failure(AgentBehaviorError("died"))
    assert (crash["kind"], crash["retryable"]) == ("agent", True)
    setup = describe_failure(ContractError("no such command"))
    assert (setup["kind"], setup["retryable"]) == ("infrastructure", False)


def test_a_failed_run_retries_until_a_run_scores(tmp_path, monkeypatch):
    (tmp_path / "agent.py").write_text(FLAKY_AGENT)
    agent = {
        "kind": "command",
        "command": [sys.executable, str(tmp_path / "agent.py"), str(tmp_path / "count")],
    }
    path = write_suite(tmp_path, agent, attempts=1)
    plan = plan_suite(path)
    real = suite_runner._start_attempt
    calls: list[Path] = []

    def lose_first_run(arguments: list[str], run_dir: Path):
        calls.append(run_dir)
        if len(calls) == 1:
            return real([], run_dir)
        return real(arguments, run_dir)

    monkeypatch.setattr(suite_runner, "_start_attempt", lose_first_run)
    report = run_suite(plan, json.loads(path.read_text()), tmp_path / "out", lambda *_: None)

    authored = report.arms[0]
    assert calls[1].name == "attempt-1-retry-1"
    assert authored.counts.passed == 1 and authored.counts.retries == 1
    assert report.complete and read_report(tmp_path / "out") == report


PEER_AGENT = """
import json, os, pathlib, sys, time
sys.stdin.readline()
folder = pathlib.Path(sys.argv[1])
marker = folder / str(os.getppid())  # One per run: the agent restarts for each step.
if marker.exists():
    print(json.dumps({"kind": "done"}))
    raise SystemExit
marker.touch()
deadline = time.monotonic() + 20
while len(list(folder.iterdir())) < 2 and time.monotonic() < deadline:
    time.sleep(0.05)
seen = len(list(folder.iterdir())) >= 2
print(json.dumps({"kind": "report", "message": "ok" if seen else "alone"}))
"""


def test_attempts_run_at_the_same_time_up_to_the_concurrency(tmp_path):
    (tmp_path / "agent.py").write_text(PEER_AGENT)
    (tmp_path / "started").mkdir()
    agent = {
        "kind": "command",
        "command": [sys.executable, str(tmp_path / "agent.py"), str(tmp_path / "started")],
    }
    path = write_suite(tmp_path, agent)

    report = run_suite(
        plan_suite(path), json.loads(path.read_text()), tmp_path / "out", lambda *_: None, 2
    )

    assert report.arms[0].counts.passed == 2
    assert report.complete


def test_arm_agent_keeps_case_context_unless_it_sets_its_own(tmp_path):
    authored: dict[str, object] = {"kind": "scripted", "actions": [], "context": {"note": "case"}}
    path = write_suite(tmp_path, authored)

    cells = {cell.arm_id: cell for cell in plan_suite(path).cells}

    assert cells["quit"].scenario["agent"] == {
        "kind": "scripted",
        "actions": [{"kind": "done"}],
        "context": {"note": "case"},
    }


def saved_attempt(root: Path, verdict: str, **fields: object) -> Path:
    root.mkdir()
    (root / "verdict.json").write_text(json.dumps({"verdict": verdict, **fields}))
    (root / "trace.jsonl").write_text("{}\n")
    return root


def test_attempt_needs_complete_evidence(tmp_path):
    assert attempt_outcome(saved_attempt(tmp_path / "passed", "passed")) == "passed"
    assert attempt_outcome(tmp_path / "never-started") == "missing"

    crashed = saved_attempt(tmp_path / "crashed", "invalid", failure={"kind": "agent"})
    assert attempt_outcome(crashed) == "agent_failed"

    no_trace = saved_attempt(tmp_path / "no-trace", "passed")
    (no_trace / "trace.jsonl").unlink()
    no_trace.with_suffix(".log").touch()
    assert attempt_outcome(no_trace) == "error"

    corrupt = saved_attempt(tmp_path / "corrupt", "passed")
    (corrupt / "verdict.json").write_text("")
    corrupt.with_suffix(".log").touch()
    assert attempt_outcome(corrupt) == "error"


PID_AGENT = """
import os, pathlib, sys, time
sys.stdin.readline()
pathlib.Path(sys.argv[1]).write_text(f"{os.getpid()} {os.getppid()}")
time.sleep(60)
"""


def start_suite(root: Path) -> tuple[subprocess.Popen[bytes], Path, Path]:
    (root / "agent.py").write_text(PID_AGENT)
    marker = root / "agent.pid"
    path = write_suite(
        root, {"kind": "command", "command": [sys.executable, str(root / "agent.py"), str(marker)]}
    )
    out = root / "out"
    process = subprocess.Popen(
        [sys.executable, "-m", "overnight.cli", "suite", "run", str(path), "--out", str(out)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not marker.exists() or not marker.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return process, marker, out


def assert_gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} survived")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_stopping_the_suite_reaps_the_agent_and_saves_the_report(tmp_path, sig):
    process, marker, out = start_suite(tmp_path)

    process.send_signal(sig)

    assert process.wait(40) == 4
    assert_gone(int(marker.read_text().split()[0]))
    report = json.loads((out / "report.json").read_text())
    assert not report["complete"]
    assert report["arms"][1]["counts"]["missing"] == 2


def test_stopping_a_parallel_suite_reaps_every_agent(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (tmp_path / "agent.py").write_text(
        "import os, pathlib, sys, time\n"
        "sys.stdin.readline()\n"
        "(pathlib.Path(sys.argv[1]) / str(os.getpid())).touch()\n"
        "time.sleep(60)\n"
    )
    path = write_suite(
        tmp_path,
        {"kind": "command", "command": [sys.executable, str(tmp_path / "agent.py"), str(agents)]},
    )
    out = tmp_path / "out"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "overnight.cli",
            "suite",
            "run",
            str(path),
            "--out",
            str(out),
            "--concurrency",
            "4",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while len(list(agents.iterdir())) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.05)

    process.send_signal(signal.SIGTERM)

    assert process.wait(40) == 4
    for marker in agents.iterdir():
        assert_gone(int(marker.name))
    assert not json.loads((out / "report.json").read_text())["complete"]


def test_a_run_the_suite_stopped_is_not_a_retry(tmp_path):
    flag = tmp_path / "first-run"
    (tmp_path / "agent.py").write_text(
        "import json, pathlib, sys, time\n"
        "sys.stdin.readline()\n"
        "flag = pathlib.Path(sys.argv[1])\n"
        "if not flag.exists():\n"
        "    flag.touch()\n"
        "    time.sleep(60)\n"
        "reported = flag.with_suffix('.reported')\n"
        "done = reported.exists()\n"
        "reported.touch()\n"
        "print(json.dumps({'kind': 'done'} if done else {'kind': 'report', 'message': 'ok'}))\n"
    )
    agent = {"kind": "command", "command": [sys.executable, str(tmp_path / "agent.py"), str(flag)]}
    path = write_suite(tmp_path, agent, attempts=1)
    out = tmp_path / "out"
    process = subprocess.Popen(
        [sys.executable, "-m", "overnight.cli", "suite", "run", str(path), "--out", str(out)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not flag.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    assert process.wait(40) == 4
    stopped = json.loads((out / "report.json").read_text())["arms"][0]["counts"]
    assert (stopped["missing"], stopped["error"], stopped["retries"]) == (1, 0, 0)

    report = resume_suite(out, lambda *_: None)

    authored = report.arms[0]
    assert (authored.counts.passed, authored.counts.retries) == (1, 0)
    assert (out / "authored" / "only" / "attempt-1.stopped").exists()


def test_nohup_keeps_the_suite_running_through_a_hangup(tmp_path):
    path = write_suite(tmp_path, {"kind": "scripted", "actions": [{"kind": "done"}]}, attempts=1)
    code = (
        "import signal, sys\n"
        "from overnight.cli import main\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        f"main(['suite', 'run', {str(path)!r}, '--out', {str(tmp_path / 'out')!r}])\n"
        "assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN\n"
    )

    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=30)


def test_a_stopped_suite_resumes_and_keeps_finished_attempts(tmp_path):
    process, marker, out = start_suite(tmp_path)
    process.send_signal(signal.SIGTERM)
    assert process.wait(40) == 4
    assert not read_report(out).complete
    frozen = out / ".inputs" / "authored" / "only" / "scenario.json"
    document = json.loads(frozen.read_text())
    frozen.chmod(0o600)
    document["agent"] = {"kind": "scripted", "actions": [{"kind": "report", "message": "ok"}]}
    frozen.write_text(json.dumps(document))
    seen: list[tuple[str, int, int]] = []

    report = resume_suite(
        out, lambda arm, _case, attempt, retry, _outcome: seen.append((arm, attempt, retry))
    )

    assert report.complete
    assert ("authored", 1, 1) in seen and ("authored", 1, 0) not in seen
    assert json.loads((out / "report.json").read_text())["complete"]
    assert read_report(out) == report
    assert resume_suite(out, lambda *_: pytest.fail("nothing is left to run")) == report


def test_resume_refuses_a_live_suite_and_a_plain_directory(tmp_path):
    process, _marker, out = start_suite(tmp_path)

    with pytest.raises(ValueError):
        resume_suite(out, lambda *_: None)

    process.send_signal(signal.SIGTERM)
    assert process.wait(40) == 4
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        resume_suite(plain, lambda *_: None)


def test_a_dead_run_does_not_leave_its_agent_behind(tmp_path):
    process, marker, out = start_suite(tmp_path)
    agent, run = (int(value) for value in marker.read_text().split())
    time.sleep(WATCH_SECONDS * 2.5)

    os.kill(run, signal.SIGKILL)
    assert_gone(agent)

    process.send_signal(signal.SIGTERM)
    assert process.wait(40) == 4
    assert attempt_outcome(out / "authored" / "only" / "attempt-1") == "error"


def qa_scenario(checks: dict[str, object]) -> dict[str, object]:
    document = mail_scenario()
    document["agent"] = {"kind": "scripted", "actions": [SEND, {"kind": "done"}]}
    document["checks"] = checks
    return document


SENT = {
    "op": "exists",
    "path": ["services", "mail", "state", "messages", 0],
}
CLAIMED = {"source": "agent_report", "op": "exists", "path": ["message"]}


def test_qa_accepts_a_scenario_that_separates_work_from_no_work():
    report = check_scenario("case", qa_scenario({"outcomes": [SENT]}))

    assert report.status == "ok"
    assert [probe.probe for probe in report.probes] == [
        "oracle",
        "quits",
        "idles",
        "waits",
        "claims",
    ]
    assert qa_exit_code([report]) == 0


def test_qa_catches_a_check_passed_by_a_false_claim():
    document = qa_scenario({"outcomes": [CLAIMED]})
    document["agent"]["actions"] = [{"kind": "report", "message": "ok"}, {"kind": "done"}]

    report = check_scenario("case", document)

    assert report.status == "defect"
    assert [probe.probe for probe in report.probes if not probe.ok] == ["claims"]
    assert qa_exit_code([report]) == 1


ARRIVES = {
    "id": "later",
    "at": 5,
    "invocation": {
        "id": "ev1",
        "actor_id": "bob",
        "service_id": "mail",
        "operation": "send_message",
        "args": {"to": ["agent"], "subject": "n", "body": "n"},
    },
}


def test_qa_catches_a_check_the_world_satisfies_without_the_agent():
    document = qa_scenario({"outcomes": [SENT]})
    document["actors"].append(
        {"id": "bob", "role": "person", "operations": {"mail": ["send_message"]}}
    )
    document["events"] = [ARRIVES]
    document["agent"]["actions"] = [{"kind": "wait", "until": 6}, {"kind": "done"}]

    report = check_scenario("case", document)

    assert report.status == "defect"
    assert {probe.probe for probe in report.probes if not probe.ok} == {
        "idles",
        "waits",
        "claims",
    }


def test_qa_catches_one_task_the_world_satisfies_without_the_agent():
    document = qa_scenario({"outcomes": []})
    document["actors"].append(
        {"id": "bob", "role": "person", "operations": {"mail": ["send_message"]}}
    )
    document["events"] = [ARRIVES]
    arrived = {"source": "trace", "op": "eq", "path": ["payload", "args", "subject"], "value": "n"}
    document["tasks"] = [
        {"id": "real", "chat_id": "a", "message": "x", "checks": {"outcomes": [arrived]}},
        {"id": "free", "chat_id": "b", "message": "y", "checks": {"outcomes": [SENT]}},
    ]
    document["agent"]["actions"] = [{"kind": "done"}]

    probes = {probe.probe: probe for probe in check_scenario("case", document).probes}

    assert not probes["idles"].ok


FINISHED = {"source": "trace", "op": "eq", "path": ["kind"], "value": "agent_done"}


def test_qa_catches_a_task_passed_by_finishing_just_before_its_deadline():
    document = qa_scenario({"outcomes": []})
    document["actors"].append(
        {"id": "bob", "role": "person", "operations": {"mail": ["send_message"]}}
    )
    document["events"] = [{**ARRIVES, "at": 3}]
    document["tasks"] = [
        {
            "id": "only",
            "chat_id": "a",
            "message": "x",
            "deadline": 5,
            "checks": {"outcomes": [SENT, FINISHED]},
        }
    ]
    document["agent"]["actions"] = [{"kind": "wait", "until": 4}, {"kind": "done"}]

    report = check_scenario("case", document)

    assert [probe.probe for probe in report.probes if not probe.ok] == ["waits for only"]


def test_qa_accepts_a_run_wide_outcome_true_from_the_start_when_tasks_reject():
    quiet = {"op": "exists", "path": ["services", "mail", "state", "messages"]}
    document = qa_scenario({"outcomes": [quiet]})
    document["tasks"] = [
        {"id": "only", "chat_id": "a", "message": "x", "checks": {"outcomes": [SENT]}}
    ]

    assert check_scenario("case", document).status == "ok"


def test_qa_reports_a_variation_its_authored_agent_does_not_fit_as_unproven():
    variation = VariationConfig.model_validate(
        load_json(EXAMPLES / "catering-adversarial-variation.json")
    )
    outage = FaultSpec(id="calendar-outage", service_id="calendar", operation="update_event")
    variation = variation.model_copy(update={"outage_faults": [*variation.outage_faults, outage]})

    report = check_scenario("case", load_json(EXAMPLES / "catering-chaos.json"), None, variation)

    assert report.status == "no_oracle"
    assert all(probe.ok for probe in report.probes)
    assert qa_exit_code([report]) == 2


def test_qa_probes_a_chat_reset_that_comes_due_during_an_open_task():
    document = qa_scenario({"outcomes": []})
    document["tasks"] = [
        {
            "id": "first",
            "chat_id": "a",
            "message": "x",
            "deadline": 4,
            "checks": {"outcomes": [SENT]},
        },
        {
            "id": "second",
            "chat_id": "a",
            "message": "y",
            "start_at": 2,
            "memory": "reset",
            "checks": {"outcomes": [SENT]},
        },
    ]

    probes = check_scenario("case", document).probes

    assert all(probe.ok for probe in probes if probe.probe != "oracle")


def test_qa_accepts_a_probe_that_ran_out_of_steps_with_nothing_left_to_happen():
    document = qa_scenario({"outcomes": [SENT]})
    document["horizon"] = 10
    document["max_steps"] = 2

    assert check_scenario("case", document).status == "ok"


def test_qa_catches_a_check_that_only_needs_the_agent_to_finish():
    finished = {"source": "trace", "op": "eq", "path": ["kind"], "value": "agent_done"}
    document = qa_scenario({"outcomes": [finished]})

    report = check_scenario("case", document)

    assert [probe.probe for probe in report.probes if not probe.ok] == ["quits", "waits"]


def test_qa_catches_a_scenario_its_own_oracle_cannot_pass():
    document = qa_scenario({"outcomes": [SENT]})
    document["agent"]["actions"] = [{"kind": "done"}]

    report = check_scenario("case", document)

    assert report.status == "defect"
    assert not report.probes[0].ok


def test_qa_never_runs_a_command_without_permission(tmp_path):
    marker = tmp_path / "ran"
    document = qa_scenario({"outcomes": [SENT]})
    code = f"import pathlib; pathlib.Path({str(marker)!r}).touch(); input(); print('{{}}')"
    document["agent"] = {"kind": "command", "command": [sys.executable, "-c", code]}

    report = check_scenario("case", document)

    assert report.status == "no_oracle"
    assert qa_exit_code([report]) == 2
    assert not marker.exists()


USAGE_AGENT = """
import json, os, pathlib, sys
input()
pathlib.Path(os.environ["OVERNIGHT_USAGE_PATH"]).write_text(json.dumps(
    {"calls": 1, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25}
))
turn = pathlib.Path(sys.argv[1])
if turn.exists():
    turn.unlink()
    print(json.dumps({"kind": "done"}))
else:
    turn.touch()
    print(json.dumps({"kind": "report", "message": "ok"}))
"""


def test_usage_sums_every_process_and_reaches_the_suite_report(tmp_path):
    (tmp_path / "agent.py").write_text(USAGE_AGENT + "\n")
    report = run_with_agent(
        tmp_path,
        {
            "kind": "command",
            "command": [sys.executable, str(tmp_path / "agent.py"), str(tmp_path / "turn")],
        },
    )

    spend = report.arms[0].spend
    assert spend.input_tokens == 40 and spend.output_tokens == 20
    assert spend.cost_usd == pytest.approx(1.0)
    assert spend.seconds is not None and spend.seconds_per_attempt is not None
    assert report.arms[0].counts.passed == 2
    assert spend.cost_per_pass == pytest.approx(0.5)
    assert "unknown" not in render_report(report).split("\n")[1]


def test_an_unmetered_process_makes_totals_unknown_not_zero(tmp_path):
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    usage_path(first).write_text(json.dumps({"calls": 1, "input_tokens": 3}))

    assert read_usage([first, second]).input_tokens is None
    assert read_usage([first]).input_tokens == 3
    assert read_usage([]) == UNMEASURED


@pytest.mark.parametrize(
    "saved",
    [
        b"{}",
        b'{"calls": 3}',
        b'{"input_tokens": 5}',
        b'{"calls": 1, "cost_usd": 1e999}',
        b"\xff\xfe",
    ],
)
def test_a_value_the_meter_did_not_record_is_unknown_not_zero(tmp_path, saved):
    path = tmp_path / "a.json"
    usage_path(path).write_bytes(saved)

    usage = read_usage([path])

    assert usage.cost_usd is None and usage.output_tokens is None


def test_a_partly_unmetered_run_has_no_call_count(tmp_path):
    first = tmp_path / "a.json"
    usage_path(first).write_text(json.dumps({"calls": 2}))

    assert read_usage([first, tmp_path / "b.json"]).calls is None


def test_the_meter_survives_bad_values_and_keeps_missing_work_unknown(tmp_path):
    target = tmp_path / "usage.json"
    meter = UsageMeter(str(target))

    meter.add(1, 2, 0.5)
    meter.add(1.5, -2, "free")
    saved = ProcessUsage.model_validate_json(target.read_text())
    assert saved.calls == 2 and saved.input_tokens is None and saved.cost_usd is None

    meter = UsageMeter(str(target))
    meter.add(1, 2, 0.5)
    meter.add_unmeasured()
    assert ProcessUsage.model_validate_json(target.read_text()) == UNMEASURED


def test_a_call_still_open_when_the_process_dies_leaves_totals_unknown(tmp_path):
    target = tmp_path / "usage.json"
    meter = UsageMeter(str(target))
    meter.add(1, 2, 0.5)

    meter.begin()
    assert ProcessUsage.model_validate_json(target.read_text()) == UNMEASURED

    meter.add(1, 2, 0.5)
    assert ProcessUsage.model_validate_json(target.read_text()).calls == 2
    meter.begin()
    meter.end()
    assert ProcessUsage.model_validate_json(target.read_text()).cost_usd == 1.0


def test_a_total_too_large_to_hold_is_unknown(tmp_path):
    meter = UsageMeter(str(tmp_path / "usage.json"))

    meter.add(1, 1, 1e308)
    meter.add(1, 1, 1e308)

    assert meter.usage.cost_usd is None


def test_a_session_cost_alone_says_nothing_about_tokens(tmp_path):
    target = tmp_path / "usage.json"

    UsageMeter(str(target)).set_session_cost("one", 1.5)

    saved = ProcessUsage.model_validate_json(target.read_text())
    assert (saved.cost_usd, saved.input_tokens, saved.calls) == (1.5, None, None)


def test_the_meter_keeps_unknown_cost_and_sums_sessions(tmp_path):
    target = tmp_path / "usage.json"
    meter = UsageMeter(str(target))

    meter.add(1, 2, None)
    assert ProcessUsage.model_validate_json(target.read_text()).cost_usd is None
    meter.set_session_cost("one", 1.5)
    meter.set_session_cost("one", 2.0)
    meter.set_session_cost("two", 1.0)
    saved = ProcessUsage.model_validate_json(target.read_text())
    assert saved.cost_usd == 3.0 and saved.input_tokens == 1 and saved.calls == 1


def claim_scenario(report: str, actions: list[JsonObject]) -> dict[str, object]:
    document = mail_scenario()
    document["checks"] = {
        "outcomes": [],
        "forbidden": [],
        "collateral": [],
        "claims": [{"pattern": "sent", "requires": SENT}],
    }
    document["agent"] = {
        "kind": "scripted",
        "actions": [*actions, {"kind": "report", "message": report}, {"kind": "done"}],
    }
    return document


@pytest.mark.parametrize(
    ("report", "actions", "verdict", "claims"),
    [
        ("The mail was SENT.", [SEND], "unknown", [True]),
        ("The mail was sent.", [], "failed", [False]),
        ("Still working on it.", [], "unknown", [True]),
    ],
    ids=["true-claim", "false-claim", "no-claim"],
)
def test_claims_are_checked_against_the_world(report, actions, verdict, claims):
    result = run_scenario(resolve_scenario(claim_scenario(report, actions)))

    assert result.verdict["verdict"] == verdict
    assert result.verdict["claims"] == claims


def test_a_claim_pattern_that_runs_out_of_time_proves_nothing(monkeypatch):
    monkeypatch.setattr(evaluation, "CLAIM_MATCH_SECONDS", 0.05)
    document = claim_scenario("a" * 40 + "!", [SEND])
    document["checks"]["outcomes"] = [SENT]
    document["checks"]["claims"][0]["pattern"] = "(a|aa)+$"

    result = run_scenario(resolve_scenario(document))

    assert result.verdict["claims"] == [None]
    assert result.verdict["verdict"] == "unknown"


def test_a_claim_does_not_decide_a_run_that_never_finished():
    document = claim_scenario("Not sent yet, working on it.", [])

    class ClaimsThenBreaks(ManagedAdapter):
        def __init__(self) -> None:
            self.replied = False

        def respond(self, observation: JsonObject) -> JsonObject:
            if self.replied:
                raise OSError("connection lost")
            self.replied = True
            return {"kind": "report", "message": "Not sent yet, working on it."}

    verdict = run_scenario(resolve_scenario(document), ClaimsThenBreaks()).verdict

    assert verdict["verdict"] == "invalid"
    assert verdict["claims"] == [False]
    assert verdict["failure"]["retryable"] is True


def test_a_claim_must_be_checked_against_the_world_not_the_report():
    document = claim_scenario("x", [])
    document["checks"]["claims"][0]["requires"] = {
        "source": "agent_report",
        "op": "exists",
        "path": ["message"],
    }

    with pytest.raises(ValueError):
        resolve_scenario(document)
