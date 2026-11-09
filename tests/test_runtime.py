import copy
import sys

import pytest
from pydantic import ValidationError

from overnight.domain.contracts import HarnessRequirements, Invocation, ScenarioError
from overnight.execution.adapters import CommandAdapter
from overnight.execution.evaluation import evaluate
from overnight.execution.harness import Capabilities, HarnessAdapter, HarnessStatus
from overnight.execution.policy import ExecutionPolicy
from overnight.execution.scenario import load_json, resolve_scenario

BASE = {
    "id": "authority",
    "start_time": 0,
    "horizon": 10,
    "max_steps": 10,
    "actors": [
        {"id": "operator", "role": "operator"},
        {
            "id": "agent",
            "role": "agent",
            "read_services": ["mail"],
            "operations": {"mail": ["send_message"]},
        },
    ],
    "services": [{"id": "mail", "kind": "mail", "state": {"messages": []}}],
    "agent": {"kind": "scripted", "actions": [{"kind": "done"}]},
    "operator": {"policy": {"default": "deny", "rules": []}, "steps": []},
    "checks": {"outcomes": [], "forbidden": [], "collateral": []},
}


def test_scenario_rejects_duplicate_keys_and_unknown_overrides(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text('{"id":"first","id":"second"}')
    with pytest.raises(ScenarioError, match="duplicate JSON key"):
        load_json(path)
    with pytest.raises(ScenarioError, match="unknown"):
        resolve_scenario(BASE, {"data": {"other-mail": {"messages": []}}})


def test_approval_binds_semantic_invocation_and_policy_revision():
    policy = ExecutionPolicy(
        {
            "default": "deny",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        }
    )
    original = Invocation(
        id="send-1",
        actor_id="agent",
        service_id="mail",
        operation="send_message",
        args={"to": ["operator"], "subject": "A", "body": "B"},
        task_id="task",
        cause_id="first",
    )
    changed_args = Invocation(
        id="send-1",
        actor_id="agent",
        service_id="mail",
        operation="send_message",
        args={"to": ["operator"], "subject": "changed", "body": "B"},
        task_id="task",
    )
    replay = Invocation(
        id="send-1",
        actor_id="agent",
        service_id="mail",
        operation="send_message",
        args={"to": ["operator"], "subject": "A", "body": "B"},
        task_id="task",
        cause_id="second",
    )
    assert policy.request(original) == "pending"
    policy.decide("send-1", True)
    assert policy.request(changed_args) == "denied"
    assert policy.request(replay) == "allowed"
    policy.change("mail", "send_message", "approval")
    assert policy.request(replay) == "pending"


def test_operator_cannot_approve_nonpending_invocation():
    policy = ExecutionPolicy({"default": "deny", "rules": []})
    with pytest.raises(ScenarioError, match="no pending"):
        policy.decide("missing", True)


def test_command_adapter_uses_one_json_line_wire_contract():
    command = [
        sys.executable,
        "-c",
        "import json,sys; data=json.load(sys.stdin); print(json.dumps({'kind':'report','message':str(data['time'])}))",
    ]
    assert CommandAdapter(command).respond({"time": 7}) == {"kind": "report", "message": "7"}


def test_grading_separates_failed_unknown_and_invalid():
    checks = {
        "outcomes": [{"op": "eq", "path": ["answer"], "value": 1}],
        "forbidden": [],
        "collateral": [],
    }
    snapshot = {"answer": 0, "pending_events": []}
    assert evaluate(checks, snapshot, [], "done")["verdict"] == "failed"
    assert evaluate(checks, snapshot, [], "horizon")["verdict"] == "unknown"
    assert evaluate(checks, snapshot, [], "invalid")["verdict"] == "invalid"


def test_agent_can_finish_after_world_event_at_horizon():
    scenario = {
        **BASE,
        "horizon": 5,
        "operator": {
            "policy": {
                "default": "deny",
                "rules": [{"service_id": "mail", "operation": "list_messages", "mode": "allow"}],
            },
            "steps": [],
        },
        "actors": [
            BASE["actors"][0],
            {**BASE["actors"][1], "operations": {"mail": ["list_messages"]}},
            {"id": "sender", "role": "person", "operations": {"mail": ["send_message"]}},
        ],
        "events": [
            {
                "id": "late-mail",
                "at": 5,
                "invocation": {
                    "id": "late-send",
                    "actor_id": "sender",
                    "service_id": "mail",
                    "operation": "send_message",
                    "args": {"to": ["agent"], "subject": "Late", "body": "Here"},
                },
            }
        ],
        "agent": {
            "kind": "scripted",
            "actions": [
                {"kind": "wait", "until": 5},
                {
                    "kind": "invoke",
                    "id": "read-late",
                    "service_id": "mail",
                    "operation": "list_messages",
                    "args": {},
                },
                {"kind": "done"},
            ],
        },
        "checks": {
            "outcomes": [
                {
                    "source": "trace",
                    "op": "match",
                    "path": [],
                    "value": {"kind": "invocation", "payload": {"operation": "list_messages"}},
                }
            ],
            "forbidden": [],
            "collateral": [],
        },
    }
    from overnight.execution.runtime import run_scenario

    result = run_scenario(resolve_scenario(scenario))
    assert result.status == "done"
    assert result.steps == 3
    assert result.verdict["verdict"] == "passed"


def test_trusted_event_malformed_arguments_fail_setup():
    scenario = {
        **BASE,
        "actors": [
            *BASE["actors"],
            {"id": "sender", "role": "person", "operations": {"mail": ["send_message"]}},
        ],
        "events": [
            {
                "id": "bad-mail",
                "at": 0,
                "invocation": {
                    "id": "bad-send",
                    "actor_id": "sender",
                    "service_id": "mail",
                    "operation": "send_message",
                    "args": {"subject": "Missing recipient", "body": "B"},
                },
            }
        ],
    }
    with pytest.raises(ScenarioError, match="missing"):
        resolve_scenario(scenario)


@pytest.mark.parametrize("mode,pending", [("approval", ["ask"]), ("allow", []), ("deny", [])])
def test_approval_requests_do_not_execute_service_operations(mode, pending):
    scenario = {
        **BASE,
        "agent": {
            "kind": "scripted",
            "actions": [
                {
                    "kind": "request_approval",
                    "id": "ask",
                    "service_id": "mail",
                    "operation": "send_message",
                    "args": {"to": ["operator"], "subject": "A", "body": "B"},
                },
                {"kind": "done"},
            ],
        },
        "operator": {
            "policy": {
                "default": "deny",
                "rules": [{"service_id": "mail", "operation": "send_message", "mode": mode}],
            },
            "steps": [],
        },
    }
    from overnight.execution.runtime import run_scenario

    result = run_scenario(resolve_scenario(scenario))
    assert result.status == "done"
    assert result.verdict["verdict"] == "unknown"
    assert result.verdict["pending_approvals"] == pending
    assert result.final_state["services"]["mail"]["state"]["messages"] == []
    assert result.final_state["results"] == {}


def test_grading_uses_json_typed_equality():
    checks = {
        "outcomes": [{"op": "eq", "path": ["answer"], "value": 1}],
        "forbidden": [{"match": {"payload": {"answer": 1}}}],
        "collateral": [],
    }
    snapshot = {"answer": True, "pending_events": []}
    trace = [{"sequence": 0, "payload": {"answer": True}}]
    result = evaluate(checks, snapshot, trace, "done")
    assert result["outcomes"] == [False]
    assert result["forbidden_trace_sequences"] == [[]]
    assert result["verdict"] == "failed"


def test_cli_run_artifacts_are_private(tmp_path):
    import json

    from overnight.cli import main

    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(BASE))
    output = tmp_path / "run"
    assert main(["run", str(scenario), "--out", str(output)]) == 2
    assert output.stat().st_mode & 0o777 == 0o700
    assert {path.name: path.stat().st_mode & 0o777 for path in output.iterdir()} == {
        "resolved-scenario.json": 0o600,
        "final-state.json": 0o600,
        "usage.json": 0o600,
        "provenance.json": 0o600,
        "verdict.json": 0o600,
        "trace.jsonl": 0o600,
    }


def test_cli_run_without_an_operator_key_is_a_usage_error_and_creates_nothing(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from overnight.cli import USAGE_EXIT_CODE, main

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    scenario = Path(__file__).resolve().parents[1] / "examples" / "shared-world-chaos.json"
    output = tmp_path / "run"
    assert main(["run", str(scenario), "--out", str(output)]) == USAGE_EXIT_CODE
    assert not output.exists()


def test_denied_approval_is_terminal_and_invocation_id_cannot_rebind():
    policy = ExecutionPolicy(
        {
            "default": "deny",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        }
    )
    first = Invocation(
        id="send-1",
        actor_id="agent",
        service_id="mail",
        operation="send_message",
        args={"to": ["operator"], "subject": "A", "body": "B"},
    )
    changed = Invocation(
        id="send-1",
        actor_id="agent",
        service_id="mail",
        operation="send_message",
        args={"to": ["operator"], "subject": "Other", "body": "B"},
    )
    assert policy.request(first) == "pending"
    policy.decide("send-1", False)
    assert policy.request(first) == "denied"
    assert policy.pending == {}
    policy.change("mail", "send_message", "approval")
    assert policy.request(changed) == "denied"


def test_done_without_any_evaluation_criteria_is_unknown():
    assert (
        evaluate(
            {"outcomes": [], "forbidden": [], "collateral": []}, {"pending_events": []}, [], "done"
        )["verdict"]
        == "unknown"
    )


@pytest.mark.parametrize(
    "path,value",
    [
        (("max_steps",), True),
        (("agent", "unexpected"), "value"),
    ],
)
def test_scenario_contract_rejects_coercion_and_undeclared_fields(path, value):
    import copy

    doc = copy.deepcopy(BASE)
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ScenarioError):
        resolve_scenario(doc)


def test_policy_wildcard_and_exact_override_share_revisioned_authority():
    policy = ExecutionPolicy(
        {"default": "deny", "rules": [{"service_id": "mail", "operation": "*", "mode": "approval"}]}
    )
    invocation = Invocation(
        id="ask",
        actor_id="agent",
        service_id="mail",
        operation="send_message",
        args={"to": ["operator"], "subject": "A", "body": "B"},
    )
    assert policy.request(invocation) == "pending"
    policy.decide("ask", True)
    assert policy.request(invocation) == "allowed"
    policy.change("mail", "*", "deny")
    assert policy.request(invocation) == "denied"
    policy.change("mail", "send_message", "allow")
    assert policy.request(invocation) == "allowed"
    policy.change("mail", "*", "approval")
    assert policy.request(invocation) == "allowed"


def test_approval_due_before_request_waits_for_the_matching_request():
    from overnight.execution.runtime import run_scenario

    args = {"to": ["operator"], "subject": "A", "body": "B"}
    invocation = {"id": "ask", "service_id": "mail", "operation": "send_message", "args": args}
    doc = {
        **BASE,
        "operator": {
            "policy": {
                "default": "deny",
                "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
            },
            "steps": [
                {
                    "at": 0,
                    "kind": "approve",
                    "match": {"service_id": "mail", "operation": "send_message", "args": args},
                }
            ],
        },
        "agent": {
            "kind": "scripted",
            "actions": [
                {"kind": "request_approval", **invocation},
                {"kind": "invoke", **invocation},
                {"kind": "done"},
            ],
        },
        "checks": {
            "outcomes": [
                {
                    "op": "match",
                    "path": ["services", "mail", "state", "messages"],
                    "value": [{"from": "agent", "subject": "A"}],
                }
            ]
        },
    }
    result = run_scenario(resolve_scenario(doc))
    assert result.verdict["verdict"] == "passed"
    assert result.final_state["pending_operator_steps"] == []
    assert result.final_state["pending_approvals"] == []


def test_unused_granted_approval_is_not_pending_operator_work():
    from overnight.execution.runtime import run_scenario

    args = {"to": ["operator"], "subject": "A", "body": "B"}
    invocation = {"id": "ask", "service_id": "mail", "operation": "send_message", "args": args}
    doc = {
        **BASE,
        "operator": {
            "policy": {
                "default": "deny",
                "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
            },
            "steps": [
                {
                    "at": 0,
                    "kind": "approve",
                    "match": {"service_id": "mail", "operation": "send_message", "args": args},
                }
            ],
        },
        "agent": {
            "kind": "scripted",
            "actions": [{"kind": "request_approval", **invocation}, {"kind": "done"}],
        },
        "checks": {"outcomes": [{"op": "eq", "path": ["results"], "value": {}}]},
    }
    result = run_scenario(resolve_scenario(doc))
    assert result.final_state["pending_approvals"] == []
    assert result.verdict["verdict"] == "passed"


@pytest.mark.parametrize("role", ["agent", "operator"])
@pytest.mark.parametrize("count", [0, 2])
def test_execution_rejects_missing_or_ambiguous_principal_roles(role, count):
    scenario = copy.deepcopy(BASE)
    scenario["actors"] = [actor for actor in scenario["actors"] if actor["role"] != role]
    scenario["actors"].extend({"id": f"{role}-{index}", "role": role} for index in range(count))
    with pytest.raises(ScenarioError, match=f"exactly one {role}"):
        resolve_scenario(scenario)


def test_role_principals_keep_authority_privacy_and_approval_identity():
    from overnight.execution.adapters import ScriptedAdapter
    from overnight.execution.runtime import Trial

    scenario = copy.deepcopy(BASE)
    scenario["actors"][0]["id"] = "owner-42"
    scenario["actors"][1]["id"] = "worker-17"
    scenario["services"][0]["state"]["messages"] = [
        {
            "id": "private",
            "from": "outsider",
            "to": ["owner-42"],
            "subject": "Private",
            "body": "Secret",
            "date": 0,
        },
        {
            "id": "visible",
            "from": "owner-42",
            "to": ["worker-17"],
            "subject": "Task",
            "body": "Public",
            "date": 0,
        },
    ]
    scenario["operator"] = {
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
                    "args": {"to": ["owner-42"], "subject": "Result", "body": "Done"},
                },
            }
        ],
    }
    action = {
        "kind": "invoke",
        "id": "send",
        "service_id": "mail",
        "operation": "send_message",
        "args": {"to": ["owner-42"], "subject": "Result", "body": "Done"},
    }
    trial = Trial(resolve_scenario(scenario), ScriptedAdapter([action, action, {"kind": "done"}]))
    result = trial.run()
    assert result.status == "done"
    observation = next(
        entry["payload"] for entry in result.trace if entry["kind"] == "agent_observation"
    )
    assert [row["id"] for row in observation["world"]["services"]["mail"]["messages"]] == [
        "visible"
    ]
    assert [receipt.status for receipt in trial.receipts] == ["pending", "approved", "succeeded"]
    invocation = next(entry for entry in result.trace if entry["kind"] == "invocation")
    assert invocation["actor_id"] == "worker-17"
    approval = next(entry for entry in result.trace if entry["kind"] == "operator_approval")
    assert approval["actor_id"] == "owner-42"
    assert result.final_state["services"]["mail"]["state"]["messages"][-1]["from"] == "worker-17"


def test_owned_adapter_cleanup_failure_preserves_run_artifacts(tmp_path, monkeypatch):
    import json

    from overnight.cli import main
    from overnight.execution.adapters import AdapterError, ScriptedAdapter

    class FailingCleanup(ScriptedAdapter):
        def close(self):
            raise AdapterError("cleanup failed")

    doc = copy.deepcopy(BASE)
    doc["operator"]["policy"]["default"] = "allow"
    actions = [
        {
            "kind": "invoke",
            "id": "send",
            "service_id": "mail",
            "operation": "send_message",
            "args": {"to": ["operator"], "subject": "Result", "body": "Done"},
        },
        {"kind": "done"},
    ]
    adapter = FailingCleanup(actions)
    monkeypatch.setattr("overnight.execution.runtime.make_adapter", lambda config: adapter)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(doc))
    output = tmp_path / "run"
    assert main(["run", str(scenario), "--out", str(output)]) == 3
    trace = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
    assert any(entry["kind"] == "agent_done" for entry in trace)
    assert trace[-1]["kind"] == "cleanup_error"
    assert trace[-1]["payload"] == {"type": "AdapterError", "error": "cleanup failed"}
    final = json.loads((output / "final-state.json").read_text())
    assert final["results"]["send"]["status"] == "succeeded"
    assert final["services"]["mail"]["state"]["messages"][-1]["subject"] == "Result"
    assert json.loads((output / "verdict.json").read_text())["verdict"] == "invalid"
    with pytest.raises(AdapterError, match="cleanup failed"):
        adapter.close()


def test_supplied_adapter_remains_caller_owned():
    from overnight.execution.adapters import ScriptedAdapter
    from overnight.execution.runtime import run_scenario

    class CallerOwned(ScriptedAdapter):
        def close(self):
            raise AssertionError("runner closed caller-owned adapter")

    result = run_scenario(resolve_scenario(BASE), CallerOwned([{"kind": "done"}]))
    assert result.status == "done"


def test_per_chat_adapters_are_not_silently_ignored_without_tasks():
    from overnight.execution.adapters import AdapterError, ScriptedAdapter
    from overnight.execution.runtime import run_scenario

    with pytest.raises(AdapterError, match="require a scenario with tasks"):
        run_scenario(resolve_scenario(BASE), adapters={"chat": ScriptedAdapter([])})


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("id", "other", "pending"),
        ("actor_id", "other", "denied"),
        ("service_id", "other", "denied"),
        ("operation", "other", "denied"),
        ("args", {"value": 2}, "denied"),
        ("task_id", "other", "denied"),
        ("chat_id", "other", "denied"),
    ],
)
def test_approval_identity_binds_each_authority_field(field, value, expected):
    policy = ExecutionPolicy(
        {
            "default": "deny",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        }
    )
    invocation = Invocation(
        id="ask",
        actor_id="agent",
        service_id="mail",
        operation="send_message",
        args={"value": 1},
        task_id="task",
        chat_id="chat",
    )
    assert policy.request(invocation) == "pending"
    decision = policy.decide("ask", True)
    changed = Invocation.model_validate({**invocation.model_dump(), field: value})
    assert decision.invocation == invocation
    assert decision.revision == 0
    assert policy.request(changed) == expected


def test_a_harness_runs_in_a_container_or_knowingly_on_the_host():
    from overnight.domain.contracts import HarnessAgent

    with pytest.raises(ValidationError, match="run_on_host"):
        HarnessAgent.model_validate({"kind": "harness", "command": ["agent"]})
    container = {"image": "overnight-agent-base"}
    with pytest.raises(ValidationError, match="run_on_host"):
        HarnessAgent.model_validate(
            {"kind": "harness", "command": ["agent"], "container": container, "run_on_host": True}
        )
    assert HarnessAgent.model_validate(
        {"kind": "harness", "command": ["agent"], "container": container}
    ).container


def test_an_unneeded_operator_decision_is_not_work_left_undone():
    from overnight.execution.runtime import run_scenario

    doc = copy.deepcopy(BASE)
    args = {"to": ["operator"], "subject": "Done", "body": "Sent"}
    match = {"service_id": "mail", "operation": "send_message", "args": args}
    doc["operator"] = {
        "policy": {
            "default": "deny",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        },
        "steps": [{"at": 1, "kind": "approve", "match": match}] * 2,
    }
    doc["agent"]["actions"] = [
        {
            "kind": "request_approval",
            "id": "ask",
            "service_id": "mail",
            "operation": "send_message",
            "args": args,
        },
        {"kind": "wait", "until": 1},
        {
            "kind": "invoke",
            "id": "ask",
            "service_id": "mail",
            "operation": "send_message",
            "args": args,
        },
        {"kind": "done"},
    ]
    doc["checks"]["outcomes"] = [
        {
            "source": "state",
            "op": "match",
            "path": ["services", "mail", "state", "messages"],
            "value": [{"subject": "Done"}],
        }
    ]
    result = run_scenario(resolve_scenario(doc))
    assert result.final_state["pending_operator_steps"] == []
    assert result.verdict["verdict"] == "passed"


class ScriptedHarness(HarnessAdapter):
    """An in-process harness replaying (action, status) replies; a callable reply gets the
    observation. It records every observation it is given."""

    def __init__(self, replies):
        super().__init__(["unused"], 1, None, HarnessRequirements(simulated_clock=False))
        self.replies = list(replies)
        self.observations = []

    def _handshake(self):
        return Capabilities(chat_isolation=True, cancel=True)

    def respond(self, observation):
        self.observations.append(observation)
        reply = self.replies.pop(0) if self.replies else ({"kind": "done"}, {"pending": False})
        action, status = reply(observation) if callable(reply) else reply
        self.status = HarnessStatus.model_validate(status)
        return action


def vendor_mail(at, to="agent"):
    return {
        "id": f"mail-at-{at}",
        "at": at,
        "invocation": {
            "id": f"mail-at-{at}-call",
            "actor_id": "vendor",
            "service_id": "mail",
            "operation": "send_message",
            "args": {"to": [to], "subject": "Refund", "body": "Eligible"},
        },
    }


VENDOR = {"id": "vendor", "role": "person", "operations": {"mail": ["send_message"]}}


def test_each_notification_is_delivered_once_per_inbox():
    from overnight.execution.runtime import run_scenario

    doc = copy.deepcopy(BASE)
    doc["actors"].append(VENDOR)
    doc["events"] = [vendor_mail(3)]
    doc["operator"]["steps"] = [{"at": 0, "kind": "message", "message": "Wait for the vendor."}]
    wait = {
        "kind": "invoke",
        "id": "wait",
        "service_id": "system",
        "operation": "wait_for_notification",
        "args": {"timeout": 5},
    }

    def prompted(observation):
        return wait, {"pending": True, "delivered": [n["id"] for n in observation["notifications"]]}

    harness = ScriptedHarness([prompted, ({"kind": "wait"}, {"pending": True})])
    result = run_scenario(resolve_scenario(doc), harness)
    assert result.status == "done"
    first, blocked, settled = harness.observations
    assert [note["kind"] for note in first["notifications"]] == ["operator_message"]
    assert blocked["notifications"] == settled["notifications"] == []
    assert settled["time"] == 3
    receipts = [receipt for receipt in settled["receipts"] if receipt["id"] == "wait"]
    assert [receipt["status"] for receipt in receipts] == ["waiting", "succeeded"]
    assert [note["kind"] for note in receipts[-1]["value"]["notifications"]] == ["mail_received"]


def test_a_harness_that_ends_its_turn_leaves_no_wait_to_take_its_next_notification():
    from overnight.execution.runtime import run_scenario

    doc = copy.deepcopy(BASE)
    doc["actors"].append(VENDOR)
    doc["events"] = [vendor_mail(3)]
    wait = {
        "kind": "invoke",
        "id": "wait",
        "service_id": "system",
        "operation": "wait_for_notification",
        "args": {"timeout": 9},
    }
    harness = ScriptedHarness([(wait, {"pending": False}), ({"kind": "done"}, {"pending": False})])
    result = run_scenario(resolve_scenario(doc), harness)
    assert result.status == "done"
    assert result.final_state["results"]["wait"]["value"]["timed_out"] is True
    woken = harness.observations[-1]
    assert woken["time"] == 3
    assert [note["kind"] for note in woken["notifications"]] == ["mail_received"]


def test_a_policy_change_releases_a_wait_on_the_request_it_cancelled():
    from overnight.execution.runtime import run_scenario

    doc = copy.deepcopy(BASE)
    doc["operator"] = {
        "policy": {
            "default": "allow",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        },
        "steps": [
            {
                "at": 2,
                "kind": "policy",
                "service_id": "mail",
                "operation": "send_message",
                "mode": "allow",
            }
        ],
    }

    def send(id_):
        return {
            "kind": "invoke",
            "id": id_,
            "service_id": "mail",
            "operation": "send_message",
            "args": {"to": ["operator"], "subject": "Receipt", "body": id_},
        }

    doc["agent"]["actions"] = [send("first"), {"kind": "wait"}, send("again"), {"kind": "done"}]
    result = run_scenario(resolve_scenario(doc))
    assert result.status == "done"
    assert result.final_state["results"]["again"]["status"] == "succeeded"
    assert result.trace[-1]["time"] == 2


def test_every_agent_can_use_the_system_service_under_a_deny_policy():
    from overnight.execution.runtime import run_scenario

    doc = copy.deepcopy(BASE)
    calls = {
        "clock": ("current_time", {}),
        "remind": ("set_reminder", {"message": "Ping", "delay": 2}),
        "list": ("list_reminders", {}),
        "cancel": ("cancel_reminder", {"id": "reminder-1"}),
        "wait": ("wait_for_notification", {"timeout": 1}),
    }
    doc["agent"]["actions"] = [
        {"kind": "invoke", "id": id_, "service_id": "system", "operation": name, "args": args}
        for id_, (name, args) in calls.items()
    ] + [{"kind": "wait"}, {"kind": "done"}]
    result = run_scenario(resolve_scenario(doc))
    assert result.status == "done"
    results = result.final_state["results"]
    assert {id_: results[id_]["status"] for id_ in calls} == dict.fromkeys(calls, "succeeded")
    assert results["wait"]["time"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"actors": [BASE["actors"][0], {**BASE["actors"][1], "operations": {"system": ["*"]}}]},
        {"faults": [{"id": "outage", "service_id": "system", "operation": "current_time"}]},
        {"services": [*BASE["services"], {"id": "system", "kind": "mail", "state": {}}]},
        {
            "actors": [*BASE["actors"], VENDOR],
            "events": [
                {
                    "id": "tick",
                    "at": 1,
                    "invocation": {
                        "id": "tick-call",
                        "actor_id": "vendor",
                        "service_id": "system",
                        "operation": "current_time",
                        "args": {},
                    },
                }
            ],
        },
    ],
)
def test_a_scenario_cannot_claim_the_system_service(change):
    with pytest.raises(ScenarioError, match="reserved"):
        resolve_scenario({**BASE, **change})
