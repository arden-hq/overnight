import copy
import sys

import pytest

from overnight.domain.contracts import JsonObject, ScenarioError
from overnight.domain.values import at_path, json_object
from overnight.execution.adapters import AdapterError, ScriptedAdapter
from overnight.execution.runtime import RunResult, run_scenario
from overnight.execution.scenario import resolve_scenario

BASE: JsonObject = json_object(
    {
        "id": "shared-episode",
        "start_time": 0,
        "horizon": 20,
        "max_steps": 20,
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
        "agent": {"kind": "scripted", "actions": []},
        "operator": {"policy": {"default": "allow", "rules": []}, "steps": []},
        "checks": {"outcomes": [], "forbidden": [], "collateral": []},
    }
)


def outcome(result: RunResult, index: int = 0) -> JsonObject:
    assert result.task_outcomes is not None
    return result.task_outcomes[index]


class RecordingAdapter(ScriptedAdapter):
    def __init__(self, actions: list[JsonObject]) -> None:
        super().__init__(actions)
        self.observations: list[JsonObject] = []
        self.closed = False

    def respond(self, observation: JsonObject) -> JsonObject:
        self.observations.append(observation)
        return super().respond(observation)

    def close(self) -> None:
        self.closed = True


def task(id_: str, chat_id: str, message: str, **extra: object) -> JsonObject:
    return json_object({"id": id_, "chat_id": chat_id, "message": message, **extra})


def test_tasks_interleave_in_one_world_with_private_chat_history_and_completion_snapshots():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [
        task(
            "first",
            "chat-a",
            "First task",
            checks={
                "outcomes": [
                    {
                        "op": "eq",
                        "path": ["services", "mail", "state", "messages"],
                        "value": [],
                    }
                ]
            },
        ),
        task("second", "chat-b", "Second task"),
    ]
    first = RecordingAdapter([{"kind": "done"}])
    second = RecordingAdapter(
        [
            {
                "kind": "invoke",
                "id": "send-second",
                "service_id": "mail",
                "operation": "send_message",
                "args": {"to": ["operator"], "subject": "Shared", "body": "Done"},
            },
            {"kind": "done"},
        ]
    )

    result = run_scenario(resolve_scenario(doc), adapters={"chat-a": first, "chat-b": second})

    assert result.status == "done"
    assert (
        at_path(result.final_state, ["services", "mail", "state", "messages", 0, "subject"])
        == "Shared"
    )
    assert at_path(outcome(result), ["verdict", "verdict"]) == "passed"
    assert at_path(first.observations[0], ["messages", 0, "message"]) == "First task"
    assert at_path(second.observations[0], ["messages", 0, "message"]) == "Second task"
    observed_task = at_path(first.observations[0], ["task"])
    assert isinstance(observed_task, dict) and "checks" not in observed_task
    assert not first.closed and not second.closed


def test_wait_blocks_only_its_task_and_preserves_absolute_wake():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [task("later", "chat-a", "Wait"), task("ready", "chat-b", "Act")]
    later = RecordingAdapter([{"kind": "wait", "until": 10}, {"kind": "done"}])
    ready = RecordingAdapter([{"kind": "report", "message": "now"}, {"kind": "done"}])

    result = run_scenario(resolve_scenario(doc), adapters={"chat-a": later, "chat-b": ready})

    report = next(entry for entry in result.trace if entry["kind"] == "agent_report")
    later_done = next(
        entry
        for entry in result.trace
        if entry["kind"] == "agent_done" and entry["task_id"] == "later"
    )
    assert report["time"] == 0
    assert later_done["time"] == 10


def test_invocation_ids_cannot_cross_task_identity():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [task("one", "chat-a", "One"), task("two", "chat-b", "Two")]
    invoke = json_object(
        {
            "kind": "invoke",
            "id": "shared-id",
            "service_id": "mail",
            "operation": "send_message",
            "args": {"to": ["operator"], "subject": "X", "body": "Y"},
        }
    )
    result = run_scenario(
        resolve_scenario(doc),
        adapters={
            "chat-a": ScriptedAdapter([invoke, json_object({"kind": "done"})]),
            "chat-b": ScriptedAdapter([invoke, json_object({"kind": "done"})]),
        },
    )
    assert result.verdict["rejected_actions"] == 1
    sent = at_path(result.final_state, ["services", "mail", "state", "messages"])
    assert isinstance(sent, list) and len(sent) == 1


SEND: JsonObject = json_object(
    {
        "kind": "invoke",
        "service_id": "mail",
        "operation": "send_message",
        "args": {"to": ["x@example.com"], "subject": "private", "body": "b"},
    }
)


def two_tasks(a: list[JsonObject], b: list[JsonObject]) -> RecordingAdapter:
    document = copy.deepcopy(BASE)
    document["tasks"] = [task("a", "chat-a", "A"), task("b", "chat-b", "B")]
    first = RecordingAdapter(a)
    run_scenario(
        resolve_scenario(document), adapters={"chat-a": first, "chat-b": ScriptedAdapter(b)}
    )
    return first


def test_a_malformed_reply_cannot_reach_another_tasks_receipts():
    first = two_tasks(
        [
            {"kind": "wait", "until": 1},
            {"kind": "invoke", "id": "b-secret"},
            {"kind": "done"},
        ],
        [{**SEND, "id": "b-secret"}, {"kind": "done"}],
    )

    receipts = first.observations[-1]["receipts"]
    assert [receipt["status"] for receipt in receipts] == ["failed"]
    assert receipts[0]["invocation_id"] != "b-secret"


def test_a_rejection_receipt_cannot_take_an_id_another_task_chose():
    first = two_tasks(
        [{"kind": "wait", "until": 1}, {"kind": "fly"}, {"kind": "done"}],
        [{**SEND, "id": "rejected-1"}, {"kind": "done"}],
    )

    assert [receipt["status"] for receipt in first.observations[-1]["receipts"]] == ["failed"]


def test_a_false_claim_fails_a_finished_episode_task():
    document = copy.deepcopy(BASE)
    checks = {
        "outcomes": [],
        "forbidden": [],
        "collateral": [],
        "claims": [
            {
                "pattern": "sent",
                "requires": {"op": "exists", "path": ["services", "mail", "state", "messages", 0]},
            }
        ],
    }
    document["tasks"] = [task("a", "chat", "A", checks=checks)]
    actions = [{"kind": "report", "message": "It was sent."}, {"kind": "done"}]

    result = run_scenario(resolve_scenario(document), adapters={"chat": ScriptedAdapter(actions)})

    assert result.verdict["verdict"] == "failed"


def test_deadline_retires_a_held_approval_and_grades_the_task():
    doc = copy.deepcopy(BASE)
    operator = doc["operator"]
    assert isinstance(operator, dict)
    operator["policy"] = {
        "default": "deny",
        "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
    }
    args = {"to": ["operator"], "subject": "X", "body": "Y"}
    match = {"service_id": "mail", "operation": "send_message", "args": args}
    operator["steps"] = [{"at": 6, "kind": "approve", "match": match}]
    doc["tasks"] = [
        task(
            "ask",
            "chat-a",
            "Ask",
            deadline=5,
            checks={
                "outcomes": [
                    {
                        "source": "state",
                        "op": "eq",
                        "path": ["services", "mail", "state", "messages"],
                        "value": [],
                    }
                ]
            },
        ),
        task("later", "chat-b", "Later"),
    ]
    adapter = ScriptedAdapter(
        [
            {
                "kind": "request_approval",
                "id": "approval",
                "service_id": "mail",
                "operation": "send_message",
                "args": args,
            },
            {"kind": "wait", "until": 5},
        ]
    )
    later = ScriptedAdapter([{"kind": "wait", "until": 7}, {"kind": "done"}])
    result = run_scenario(resolve_scenario(doc), adapters={"chat-a": adapter, "chat-b": later})
    task_result = outcome(result)
    assert task_result["status"] == "deadline"
    assert task_result["pending_approvals"] == ["approval"]
    assert at_path(task_result, ["verdict", "verdict"]) == "passed"
    assert result.status == "done"
    assert result.final_state["pending_operator_steps"] == []
    assert not any(entry["kind"] == "operator_approval" for entry in result.trace)


def test_a_task_left_undone_at_its_deadline_lets_the_episode_pass():
    doc = copy.deepcopy(BASE)
    doc["operator"] = {
        "policy": {
            "default": "allow",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        },
        "steps": [],
    }
    unchanged = {"source": "state", "op": "unchanged", "path": ["services", "mail", "state"]}
    doc["checks"] = {"outcomes": [], "forbidden": [], "collateral": [unchanged]}
    doc["tasks"] = [
        task("held", "chat-a", "Send, then wait", deadline=2),
        task("next", "chat-b", "Report", checks={"collateral": [unchanged]}),
    ]
    args = {"to": ["operator"], "subject": "X", "body": "Y"}
    held = ScriptedAdapter(
        [
            {
                "kind": "request_approval",
                "id": "held",
                "service_id": "mail",
                "operation": "send_message",
                "args": args,
            },
            {"kind": "wait", "until": 2},
        ]
    )
    later = ScriptedAdapter([{"kind": "wait", "until": 3}, {"kind": "done"}])
    result = run_scenario(resolve_scenario(doc), adapters={"chat-a": held, "chat-b": later})
    assert [item["status"] for item in result.task_outcomes or []] == ["deadline", "done"]
    assert result.verdict["verdict"] == "passed"


def test_a_reset_task_waits_until_the_chat_is_free():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [
        task("active", "same", "First"),
        task("reset", "same", "Second", memory="reset"),
    ]
    adapter = ScriptedAdapter([{"kind": "wait", "until": 3}, {"kind": "done"}])
    result = run_scenario(resolve_scenario(doc), adapters={"same": adapter})
    assert [item["status"] for item in result.task_outcomes or []] == ["done", "done"]
    kinds = [entry["kind"] for entry in result.trace]
    second = [i for i, kind in enumerate(kinds) if kind == "operator_message"][1]
    assert kinds.index("agent_done") < second


def test_task_times_are_absolute_and_bounded():
    doc = copy.deepcopy(BASE)
    doc["start_time"] = 10
    doc["tasks"] = [task("late", "chat", "Late", start_at=12, deadline=11)]
    with pytest.raises(ScenarioError, match="deadline precedes start_at"):
        resolve_scenario(doc)


def test_owned_episode_closes_every_created_adapter_after_cleanup_failure(monkeypatch):
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [task("one", "chat-a", "One"), task("two", "chat-b", "Two")]
    created = []

    class Owned(ScriptedAdapter):
        def __init__(self, fail):
            super().__init__([{"kind": "done"}])
            self.fail = fail
            self.closed = False

        def close(self):
            self.closed = True
            if self.fail:
                raise RuntimeError("close failed")

    def make_owned(config):
        adapter = Owned(not created)
        created.append(adapter)
        return adapter

    monkeypatch.setattr("overnight.execution.episode.make_adapter", make_owned)
    result = run_scenario(resolve_scenario(doc))
    assert result.status == "invalid"
    assert [adapter.closed for adapter in created] == [True, True]


def test_unchecked_completed_task_is_neutral_to_passing_episode_verdict():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [
        task(
            "checked",
            "chat-a",
            "Checked",
            checks={"outcomes": [{"op": "exists", "path": ["services", "mail"]}]},
        ),
        task("unchecked", "chat-b", "Unchecked"),
    ]
    result = run_scenario(
        resolve_scenario(doc),
        adapters={
            "chat-a": ScriptedAdapter([{"kind": "done"}]),
            "chat-b": ScriptedAdapter([{"kind": "done"}]),
        },
    )
    assert result.verdict["verdict"] == "passed"


def test_multiple_chats_reject_one_aliased_adapter():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [task("one", "chat-a", "One"), task("two", "chat-b", "Two")]
    with pytest.raises(AdapterError, match="require per-chat adapters"):
        run_scenario(resolve_scenario(doc), ScriptedAdapter([{"kind": "done"}]))


def test_episode_event_task_and_chat_must_match():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [task("one", "chat-a", "One")]
    actors = doc["actors"]
    assert isinstance(actors, list)
    actors.append({"id": "system", "role": "system", "operations": {"mail": ["send_message"]}})
    doc["events"] = [
        {
            "id": "event",
            "at": 1,
            "invocation": {
                "id": "event-call",
                "actor_id": "system",
                "service_id": "mail",
                "operation": "send_message",
                "args": {"to": ["agent"], "subject": "X", "body": "Y"},
                "task_id": "one",
                "chat_id": "wrong",
            },
        }
    ]
    with pytest.raises(ScenarioError, match="event task and chat do not match"):
        resolve_scenario(doc)
    events = doc["events"]
    assert isinstance(events, list) and isinstance(events[0], dict)
    invocation = events[0]["invocation"]
    assert isinstance(invocation, dict)
    del invocation["task_id"]
    del invocation["chat_id"]
    assert resolve_scenario(doc).config.events[0].invocation.task_id is None


def test_horizon_is_the_task_terminal_status():
    doc = copy.deepcopy(BASE)
    doc["horizon"] = 3
    doc["tasks"] = [task("waiting", "chat", "Wait")]
    result = run_scenario(
        resolve_scenario(doc),
        adapters={"chat": ScriptedAdapter([{"kind": "wait", "until": 3}])},
    )
    assert result.status == "horizon"
    assert outcome(result)["status"] == "horizon"


def test_final_task_done_on_the_last_permitted_step_completes_the_episode():
    doc = copy.deepcopy(BASE)
    doc["max_steps"] = 1
    exists = {"outcomes": [{"op": "exists", "path": ["services", "mail"]}]}
    doc["checks"] = exists
    doc["tasks"] = [task("one", "chat", "One", checks=exists)]
    result = run_scenario(
        resolve_scenario(doc), adapters={"chat": ScriptedAdapter([{"kind": "done"}])}
    )
    assert result.status == "done"
    assert result.verdict["verdict"] == "passed"


def test_passing_task_does_not_hide_future_operator_work():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [
        task(
            "checked",
            "chat",
            "Work",
            checks={"outcomes": [{"op": "exists", "path": ["services", "mail"]}]},
        )
    ]
    operator = doc["operator"]
    assert isinstance(operator, dict)
    operator["steps"] = [
        {
            "kind": "message",
            "at": 5,
            "message": "Follow up",
            "task_id": "checked",
            "chat_id": "chat",
        }
    ]
    result = run_scenario(
        resolve_scenario(doc),
        adapters={"chat": ScriptedAdapter([json_object({"kind": "done"})])},
    )
    assert result.verdict["verdict"] == "unknown"
    assert result.verdict["pending_operator_steps"] == ["message"]


def test_passing_task_does_not_hide_future_global_event():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [
        task(
            "checked",
            "chat",
            "Work",
            checks={"outcomes": [{"op": "exists", "path": ["services", "mail"]}]},
        )
    ]
    actors = doc["actors"]
    assert isinstance(actors, list)
    actors.append({"id": "system", "role": "system", "operations": {"mail": ["send_message"]}})
    doc["events"] = [
        {
            "id": "global-event",
            "at": 5,
            "invocation": {
                "id": "global-call",
                "actor_id": "system",
                "service_id": "mail",
                "operation": "send_message",
                "args": {"to": ["agent"], "subject": "Global", "body": "Later"},
            },
        }
    ]
    result = run_scenario(
        resolve_scenario(doc),
        adapters={"chat": ScriptedAdapter([json_object({"kind": "done"})])},
    )
    assert result.verdict["verdict"] == "unknown"
    assert result.verdict["pending_events"] == ["global-event"]


def test_owned_persistent_adapter_reset_starts_fresh_process_history(tmp_path):
    agent = tmp_path / "agent.py"
    agent.write_text(
        """import json, os, pathlib, sys
path = os.environ.get("OVERNIGHT_PROVENANCE_PATH")
if path:
    pathlib.Path(path).write_text(json.dumps({
        "implementation": "reset-test",
        "code": {"git_commit": None, "sha256": "a" * 64, "version": None}
    }))
count = 0
for line in sys.stdin:
    observation = json.loads(line)
    task = observation["task"]["id"]
    if task == "first":
        action = {"kind": "done"}
    elif count == 0:
        action = {"kind": "report", "message": str(count)}
    else:
        action = {"kind": "done"}
    print(json.dumps(action), flush=True)
    count += 1
"""
    )
    doc = copy.deepcopy(BASE)
    doc["agent"] = {"kind": "persistent_command", "command": [sys.executable, str(agent)]}
    doc["tasks"] = [
        task("first", "chat", "First"),
        task(
            "second",
            "chat",
            "Second",
            start_at=1,
            memory="reset",
            checks={
                "outcomes": [
                    {"source": "agent_report", "op": "eq", "path": ["message"], "value": "0"}
                ]
            },
        ),
    ]
    result = run_scenario(resolve_scenario(doc))
    assert result.status == "done"
    assert result.verdict["verdict"] == "passed"
    assert at_path(outcome(result, 1), ["verdict", "verdict"]) == "passed"
    assert result.provenance["connector_status"] == "available"
    assert at_path(result.provenance, ["connector", "implementation"]) == "reset-test"


def test_same_chat_tasks_queue_without_crossing_pending_approval_or_messages():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [
        task("first", "shared", "First"),
        task("second", "shared", "Second", start_at=1),
    ]
    doc["operator"] = {
        "policy": {
            "default": "allow",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        },
        "steps": [
            {
                "kind": "approve",
                "at": 2,
                "match": {
                    "service_id": "mail",
                    "operation": "send_message",
                    "args": {"to": ["operator"], "subject": "First", "body": "Done"},
                },
            }
        ],
    }
    operation = {
        "id": "first-send",
        "service_id": "mail",
        "operation": "send_message",
        "args": {"to": ["operator"], "subject": "First", "body": "Done"},
    }
    adapter = RecordingAdapter(
        [
            json_object(operation | {"kind": "request_approval"}),
            json_object({"kind": "wait", "until": 2}),
            json_object(operation | {"kind": "invoke"}),
            json_object({"kind": "done"}),
            json_object({"kind": "report", "message": "Second ready"}),
            json_object({"kind": "done"}),
        ]
    )
    result = run_scenario(resolve_scenario(doc), adapters={"shared": adapter})
    assert result.status == "done"
    assert adapter.observations[2]["task"]["id"] == "first"
    assert adapter.observations[2]["messages"] == [
        {"id": "task-first", "time": 0, "message": "First", "task_id": "first", "chat_id": "shared"}
    ]
    assert adapter.observations[4]["task"]["id"] == "second"
    assert adapter.observations[4]["receipts"] == []
    assert adapter.observations[4]["messages"][1] == {
        "id": "task-second",
        "time": 2,
        "message": "Second",
        "task_id": "second",
        "chat_id": "shared",
    }


def test_deadline_retires_pending_native_work_before_reusing_its_chat():
    from overnight.execution.harness import Capabilities, HarnessAdapter, HarnessStatus

    class PendingHarness(HarnessAdapter):
        def __init__(self):
            from overnight.domain.contracts import HarnessRequirements

            super().__init__(["unused"], 1, None, HarnessRequirements(simulated_clock=False))
            self.cancelled = False
            self.observations = []

        def _handshake(self):
            return Capabilities(chat_isolation=True, cancel=True)

        def respond(self, observation):
            self.observations.append(observation)
            if observation["task"]["id"] == "successor":
                assert self.cancelled, "expired native call still owns this conversation"
                self.status = HarnessStatus(pending=False)
                return {"kind": "done"}
            self.status = HarnessStatus(pending=True)
            if not observation["receipts"]:
                return {
                    "kind": "request_approval",
                    "id": "expired-call",
                    "service_id": "mail",
                    "operation": "send_message",
                    "args": {"to": ["operator"], "subject": "Pending", "body": "Hold"},
                }
            return {"kind": "wait", "until": 3}

        def cancel(self):
            self.cancelled = True
            self.status = HarnessStatus(pending=False)

    doc = copy.deepcopy(BASE)
    doc["tasks"] = [
        task("expired", "shared", "First", deadline=2),
        task("successor", "shared", "Second", start_at=1),
    ]
    doc["operator"] = {
        "policy": {
            "default": "allow",
            "rules": [{"service_id": "mail", "operation": "send_message", "mode": "approval"}],
        },
        "steps": [],
    }
    adapter = PendingHarness()
    result = run_scenario(resolve_scenario(doc), adapters={"shared": adapter})
    assert adapter.cancelled
    assert outcome(result)["pending_approvals"] == ["expired-call"]
    assert outcome(result, 1)["status"] == "done"
    assert adapter.observations[-1]["receipts"] == []


def test_a_chat_that_never_got_a_turn_does_not_invalidate_connector_evidence(tmp_path):
    agent = tmp_path / "agent.py"
    agent.write_text(
        """import json, os, pathlib, sys
capabilities = {"simulated_clock": True, "chat_isolation": True, "cancel": True}
for line in sys.stdin:
    request = json.loads(line)
    if request["kind"] == "hello":
        reply = {"kind": "ready", "version": 1, "capabilities": capabilities}
    elif request["kind"] == "observe":
        path = pathlib.Path(os.environ["OVERNIGHT_PROVENANCE_PATH"])
        if not path.exists():
            path.write_text(json.dumps({
                "implementation": "late-chat-test",
                "code": {"git_commit": None, "sha256": "a" * 64, "version": None},
            }))
        reply = {"kind": "action", "action": {"kind": "report", "message": "busy"},
                 "status": {"pending": False}}
    else:
        reply = {"kind": request["kind"], "scope": request["scope"]}
    print(json.dumps(reply), flush=True)
    if request["kind"] == "close":
        break
"""
    )
    doc = copy.deepcopy(BASE)
    doc["max_steps"] = 3
    doc["agent"] = {
        "kind": "harness",
        "command": [sys.executable, str(agent)],
        "run_on_host": True,
        "timeout_seconds": 10,
    }
    doc["tasks"] = [task("first", "chat-a", "First"), task("later", "chat-b", "Later", start_at=5)]
    result = run_scenario(resolve_scenario(doc))
    assert result.status == "max_steps"
    assert result.provenance["connector_status"] == "available"


def test_a_world_notification_reaches_each_open_chat_once():
    from test_runtime import VENDOR, vendor_mail

    doc = copy.deepcopy(BASE)
    doc["actors"].append(VENDOR)
    doc["events"] = [vendor_mail(2)]
    doc["tasks"] = [task("first", "chat-a", "One"), task("second", "chat-b", "Two")]
    adapters = {
        chat: RecordingAdapter(
            [{"kind": "wait", "until": 3}, {"kind": "wait", "until": 4}, {"kind": "done"}]
        )
        for chat in ("chat-a", "chat-b")
    }
    result = run_scenario(resolve_scenario(doc), adapters=adapters)
    assert result.status == "done"
    seen = {
        chat: [[note["kind"] for note in item["notifications"]] for item in adapter.observations]
        for chat, adapter in adapters.items()
    }
    assert seen == dict.fromkeys(adapters, [["operator_message"], ["mail_received"], []])
    (mail_a,) = adapters["chat-a"].observations[1]["notifications"]
    (mail_b,) = adapters["chat-b"].observations[1]["notifications"]
    assert mail_a == mail_b


def test_an_order_cancelled_in_one_chat_notifies_the_agents_other_chat_only():
    doc = copy.deepcopy(BASE)
    doc["actors"][1]["read_services"] = ["mail", "shop"]
    doc["actors"][1]["operations"] = {"shop": ["cancel_order"]}
    shop = {
        "catalog": [{"id": "lunch", "title": "Lunch", "price_cents": 900}],
        "orders": [
            {
                "id": "original",
                "item_id": "lunch",
                "buyer_id": "agent",
                "quantity": 1,
                "total_cents": 900,
                "status": "confirmed",
                "placed_at": 0,
            }
        ],
    }
    doc["services"].append({"id": "shop", "kind": "shop", "state": shop})
    doc["tasks"] = [task("cancel", "chat-a", "Cancel"), task("audit", "chat-b", "Audit")]
    cancel = {
        "kind": "invoke",
        "id": "cancel",
        "service_id": "shop",
        "operation": "cancel_order",
        "args": {"order_id": "original"},
    }
    adapters = {
        "chat-a": RecordingAdapter([{"kind": "wait", "until": 2}, cancel, {"kind": "done"}]),
        "chat-b": RecordingAdapter([{"kind": "wait", "until": 9}, {"kind": "done"}]),
    }
    result = run_scenario(resolve_scenario(doc), adapters=adapters)
    assert result.status == "done"
    seen = {
        chat: [
            (note["kind"], note["ref"], note["summary"], note["chat_id"])
            for item in adapter.observations[1:]
            for note in item["notifications"]
        ]
        for chat, adapter in adapters.items()
    }
    assert seen == {
        "chat-a": [],
        "chat-b": [("order_changed", "original", "Order original cancelled", "chat-b")],
    }


def test_an_idle_task_completes_at_its_deadline_or_when_its_chats_successor_is_due():
    from test_runtime import VENDOR, ScriptedHarness, vendor_mail

    doc = copy.deepcopy(BASE)
    doc["actors"].append(VENDOR)
    doc["events"] = [vendor_mail(6)]
    doc["tasks"] = [
        task("first", "shared", "First"),
        task("next", "shared", "Next", start_at=3),
        task("timed", "other", "Timed", deadline=4),
    ]
    shared, other = ScriptedHarness([]), ScriptedHarness([])
    result = run_scenario(resolve_scenario(doc), adapters={"shared": shared, "other": other})
    assert result.status == "done"
    ended = {
        item["task_id"]: (item["status"], item["completed_at"]) for item in result.task_outcomes
    }
    assert ended == {"first": ("done", 3), "next": ("done", 6), "timed": ("done", 4)}
    assert [item["time"] for item in shared.observations] == [0, 3, 6]


def test_a_finished_task_leaves_no_wait_or_native_wake_in_its_chat():
    from test_runtime import ScriptedHarness

    from overnight.execution.harness import HarnessStatus

    doc = copy.deepcopy(BASE)
    doc["tasks"] = [
        task("first", "shared", "First", deadline=4),
        task("next", "shared", "Next", start_at=5),
    ]
    wait = {
        "kind": "invoke",
        "id": "wait",
        "service_id": "system",
        "operation": "wait_for_notification",
        "args": {"timeout": 15},
    }
    commands = RecordingAdapter([wait, {"kind": "done"}, {"kind": "done"}])
    result = run_scenario(resolve_scenario(doc), adapters={"shared": commands})
    assert result.final_state["results"]["wait"]["value"]["timed_out"] is True
    assert [note["summary"] for note in commands.observations[-1]["notifications"]] == ["Next"]

    class Scheduler(ScriptedHarness):
        cancels = 0

        def cancel(self):
            self.cancels += 1
            self.status = HarnessStatus(pending=False)

    harness = Scheduler([({"kind": "done"}, {"pending": False, "next_wake": 8})])
    result = run_scenario(resolve_scenario(doc), adapters={"shared": harness})
    assert result.status == "done"
    assert harness.cancels == 1
    assert [(item["time"], item["task"]["id"]) for item in harness.observations] == [
        (0, "first"),
        (5, "next"),
    ]


def test_a_message_for_a_task_that_has_not_started_reaches_it_when_it_starts():
    doc = copy.deepcopy(BASE)
    doc["tasks"] = [task("later", "chat", "Start", start_at=2)]
    early = {"at": 0, "kind": "message", "message": "Early note", "task_id": "later"}
    doc["operator"]["steps"] = [early | {"chat_id": "chat"}]
    adapter = RecordingAdapter([{"kind": "done"}])
    run_scenario(resolve_scenario(doc), adapters={"chat": adapter})
    (first,) = adapter.observations
    assert first["time"] == 2
    assert [note["summary"] for note in first["notifications"]] == ["Start", "Early note"]
