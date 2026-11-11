import copy
import json
import sys

import pytest
from test_runtime import BASE

from overnight import cli
from overnight.execution.adapters import (
    AdapterError,
    CommandAdapter,
    PersistentCommandAdapter,
    ScriptedAdapter,
)
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import resolve_scenario


def test_persistent_protocol_keeps_process_state_and_closes(tmp_path):
    child = tmp_path / "agent.py"
    child.write_text(
        "import json,sys\nfor n,line in enumerate(sys.stdin):\n data=json.loads(line)\n print(json.dumps({'kind':'report','message':str(n)+':'+str(data['time'])}),flush=True)\n"
    )
    adapter = PersistentCommandAdapter([sys.executable, str(child)])
    try:
        assert adapter.respond({"time": 4}) == {"kind": "report", "message": "0:4"}
        assert adapter.respond({"time": 9}) == {"kind": "report", "message": "1:9"}
        process = adapter.process
    finally:
        adapter.close()
    assert process.returncode == 0


@pytest.mark.parametrize("reply", ["not-json", "[]"])
def test_persistent_protocol_rejects_malformed_actions(reply):
    adapter = PersistentCommandAdapter(
        [sys.executable, "-c", f"input(); print({reply!r},flush=True)"]
    )
    try:
        with pytest.raises(AdapterError):
            adapter.respond({"time": 0})
    finally:
        adapter.close()


def test_reactive_approval_matches_exact_arguments_and_reports_decision():
    doc = copy.deepcopy(BASE)
    args = {"to": ["operator"], "subject": "Approved", "body": "B"}
    doc["operator"] = {
        "policy": {
            "default": "deny",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        },
        "steps": [
            {
                "at": 0,
                "kind": "approve",
                "match": {
                    "service_id": "mail",
                    "operation": "send_message",
                    "args": args,
                },
            }
        ],
    }
    doc["checks"]["outcomes"] = [
        {
            "op": "eq",
            "path": ["services", "mail", "state", "messages", 0, "subject"],
            "value": "Approved",
        }
    ]

    class CapturingAdapter(ScriptedAdapter):
        def respond(self, observation):
            self.observation = observation
            return super().respond(observation)

    adapter = CapturingAdapter(
        [
            {
                "kind": "request_approval",
                "id": "wrong",
                "service_id": "mail",
                "operation": "send_message",
                "args": {**args, "subject": "Other"},
            },
            {
                "kind": "request_approval",
                "id": "chosen-by-agent",
                "service_id": "mail",
                "operation": "send_message",
                "args": args,
            },
            {
                "kind": "invoke",
                "id": "chosen-by-agent",
                "service_id": "mail",
                "operation": "send_message",
                "args": args,
            },
            {"kind": "done"},
        ]
    )
    result = run_scenario(resolve_scenario(doc), adapter)
    assert result.final_state["pending_approvals"] == ["wrong"]
    assert result.final_state["results"]["chosen-by-agent"]["status"] == "succeeded"
    assert [
        r["invocation_id"] for r in adapter.observation["receipts"] if r["status"] == "approved"
    ] == ["chosen-by-agent"]
    assert result.verdict["verdict"] == "unknown"


def test_persistent_timeout_covers_blocked_request_write():
    adapter = PersistentCommandAdapter(
        [
            sys.executable,
            "-c",
            "import time,sys,json; time.sleep(0.3); sys.stdin.readline(); print(json.dumps({'kind':'done'}),flush=True)",
        ],
        timeout_seconds=0.05,
    )
    try:
        with pytest.raises(AdapterError, match="timed out"):
            adapter.respond({"payload": "x" * 2_000_000})
    finally:
        adapter.close()


def test_same_tick_matching_approvals_select_after_each_prior_decision():
    from overnight.domain.contracts import ApprovalRequest, OperatorConfig
    from overnight.execution.operator import ScriptedOperator

    target = {
        "service_id": "mail",
        "operation": "send_message",
        "args": {"to": ["operator"], "subject": "A", "body": "B"},
    }
    config = OperatorConfig.model_validate(
        {
            "policy": {
                "default": "deny",
                "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
            },
            "steps": [
                {"at": 0, "kind": "approve", "match": target},
                {"at": 0, "kind": "approve", "match": target},
            ],
        }
    )
    open_requests = [
        ApprovalRequest(
            kind="approval",
            id=f"op-{number}",
            time=0,
            invocation_id=invocation_id,
            policy_revision=0,
            **target,
        )
        for number, invocation_id in enumerate(["first", "second"], start=1)
    ]
    as_written = lambda _service, _operation, args: args  # noqa: E731
    answered = ScriptedOperator(config.steps, as_written).due(0, open_requests)
    assert [item.answer.request_id for item in answered] == ["op-1", "op-2"]


@pytest.mark.parametrize("adapter_type", [PersistentCommandAdapter, CommandAdapter])
def test_command_modes_share_bounded_response_and_reap_on_protocol_failure(adapter_type):
    adapter = adapter_type(
        [sys.executable, "-c", "import sys; input(); print('x' * 1_048_577,flush=True)"]
    )
    with pytest.raises(AdapterError):
        adapter.respond({"time": 0})
    adapter.close()
    if isinstance(adapter, PersistentCommandAdapter):
        assert adapter.process is None
        with pytest.raises(AdapterError, match="closed"):
            adapter.respond({"time": 1})


@pytest.mark.parametrize("suffix", ["print('{}',flush=True)", "raise SystemExit(7)"])
def test_one_shot_requires_one_line_and_successful_exit(suffix):
    adapter = CommandAdapter(
        [sys.executable, "-c", 'input(); print(\'{"kind":"done"}\',flush=True); ' + suffix]
    )
    with pytest.raises(AdapterError):
        adapter.respond({"time": 0})


def test_exited_leader_permission_race_preserves_protocol_failure(monkeypatch):
    import os

    signal_group = os.killpg
    denied = False

    def deny_first_signal(pid, sig):
        nonlocal denied
        if not denied:
            denied = True
            raise PermissionError("exited leader awaiting reap")
        signal_group(pid, sig)

    monkeypatch.setattr(os, "killpg", deny_first_signal)
    adapter = PersistentCommandAdapter(
        [sys.executable, "-c", "input(); print('{}',flush=True); print('{}',flush=True)"]
    )
    with pytest.raises(AdapterError, match="exactly one JSON line"):
        adapter.respond_once({"time": 0})
    assert adapter.process is None


def test_persistent_group_permission_failure_propagates_and_closes(monkeypatch):
    import os

    def deny_signal(pid, sig):
        raise PermissionError("group access denied")

    monkeypatch.setattr(os, "killpg", deny_signal)
    adapter = PersistentCommandAdapter(
        [sys.executable, "-c", "input(); print('{}',flush=True); print('{}',flush=True)"]
    )
    with pytest.raises(PermissionError, match="group access denied"):
        adapter.respond_once({"time": 0})
    assert adapter.process is None


def test_live_leader_permission_failure_is_bounded_and_closes(monkeypatch):
    import os

    import anyio

    from overnight.execution import adapters

    class LiveProcess:
        pid = 123
        returncode = None
        stdin = None
        closed = False

        async def wait(self):
            await anyio.Event().wait()

        async def aclose(self):
            self.closed = True

    signals = []

    def deny_signal(pid, sig):
        signals.append(sig)
        raise PermissionError("live group access denied")

    monkeypatch.setattr(os, "killpg", deny_signal)
    monkeypatch.setattr(adapters, "TERMINATE_WAIT_SECONDS", 0)
    adapter = PersistentCommandAdapter(["unused"])
    process = LiveProcess()
    adapter.process = process
    adapter._failed = True
    with pytest.raises(PermissionError, match="live group access denied"):
        anyio.run(adapter._close_process)
    assert len(signals) == 1
    assert process.closed
    assert adapter.process is None


def test_correlated_reply_after_clean_exit_fails_and_closes_the_adapter():
    adapter = PersistentCommandAdapter(["sh", "-c", "read line; exit 0"])
    with pytest.raises(AdapterError, match="ended before response"):
        adapter._request_response({"time": 0}, require_exit=False, response_id="missing")
    assert adapter._failed and adapter.process is None
    with pytest.raises(AdapterError, match="closed"):
        adapter.respond({"time": 1})


def test_cli_separates_runtime_failures_from_invalid_scenarios(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(BASE))
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(BASE | {"max_steps": "many"}))

    def broken_transport(resolved):
        raise AdapterError("persistent agent exchange timed out")

    monkeypatch.setattr(cli, "run_scenario", broken_transport)
    assert cli.main(["run", str(invalid), "--out", str(tmp_path / "a")]) == 3
    assert cli.main(["run", str(scenario), "--out", str(tmp_path / "b")]) == 4
