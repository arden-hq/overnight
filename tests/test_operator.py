import copy
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from test_runtime import BASE, ScriptedHarness

from overnight.adapters.operator_model import ModelTransport
from overnight.domain.contracts import OperatorModel, ScenarioError
from overnight.execution.adapters import ScriptedAdapter
from overnight.execution.operator import Completion, OperatorUnavailable, RecordedTransport
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import resolve_scenario
from overnight.execution.usage import ProcessUsage

ROOT = Path(__file__).resolve().parents[1]
CALL_USAGE = ProcessUsage(calls=1, input_tokens=100, output_tokens=10, cost_usd=0.001)


class FakeModel:
    source = "model"

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def complete(self, prompt, expects):
        self.prompts.append(prompt)
        text = "\n".join(message["content"] for message in prompt)
        return Completion(
            text=self.answer(text, expects),
            usage=CALL_USAGE,
            model="fake-model",
            settings={"model": "fake-model", "temperature": 0},
        )

    def finish(self):
        pass


def persona(*briefs, **model):
    return {
        "description": "Dana, the events lead. Short, practical messages.",
        "briefs": list(briefs),
        **({"model": model} if model else {}),
    }


def single_lane(answer_brief="Book the room for Sunday."):
    doc = copy.deepcopy(BASE)
    doc["operator"] = {
        "policy": {"default": "allow", "rules": []},
        "steps": [{"at": 0, "kind": "message", "message": "Book the meeting room."}],
        "persona": persona(
            {"wants": answer_brief, "knows": ["Sunday 8am suits me."], "prefers": []}
        ),
    }
    doc["checks"]["outcomes"] = [
        {
            "label": "SECRET-CHECK-LABEL",
            "op": "match",
            "path": ["services", "mail", "state", "messages"],
            "value": [{"subject": "Booked"}],
        }
    ]
    return doc


def operator_messages(result):
    return [
        (entry["time"], entry["chat_id"], entry["payload"]["id"], entry["payload"]["message"])
        for entry in result.trace
        if entry["kind"] == "operator_message"
    ]


def test_the_persona_answers_a_report_in_the_asking_chat():
    report = {"kind": "report", "message": "Book it for Sunday 8am?"}
    model = FakeModel(lambda text, expects: json.dumps({"message": "Yes, Sunday 8am is fine."}))
    harness = ScriptedHarness(
        [(report, {"pending": False}), ({"kind": "done"}, {"pending": False})]
    )
    result = run_scenario(resolve_scenario(single_lane()), harness, operator_transport=model)

    assert result.status == "done"
    assert operator_messages(result)[1:] == [(0, None, "answer-op-1", "Yes, Sunday 8am is fine.")]
    after = harness.observations[1]
    assert after["notifications"][-1]["summary"] == "Yes, Sunday 8am is fine."
    (prompt,) = model.prompts
    shown = json.dumps(prompt)
    assert "Book the room for Sunday." in shown and "Sunday 8am suits me." in shown
    assert "Book it for Sunday 8am?" in prompt[-1]["content"]
    assert "SECRET-CHECK-LABEL" not in shown and "results" not in shown
    assert result.operator_usage == CALL_USAGE
    assert result.provenance["operator"]["transport"] == "model"
    assert not any("Book the room for Sunday." in json.dumps(o) for o in harness.observations)

    silent = FakeModel(lambda text, expects: '{"message": null}')
    quiet = ScriptedHarness([(report, {"pending": False}), ({"kind": "done"}, {"pending": False})])
    result = run_scenario(resolve_scenario(single_lane()), quiet, operator_transport=silent)
    assert len(operator_messages(result)) == 1
    assert [note["kind"] for note in quiet.observations[1]["notifications"]] == ["operator_message"]


def two_chats():
    doc = copy.deepcopy(BASE)
    doc["actors"][1] = {
        "id": "agent",
        "role": "agent",
        "read_services": ["shop"],
        "operations": {"shop": ["cancel_order"]},
    }
    doc["services"] = [
        {
            "id": "shop",
            "kind": "shop",
            "state": {
                "catalog": [{"id": "meal", "title": "Meals", "price_cents": 2000, "available": 4}],
                "orders": [
                    {
                        "id": "original",
                        "item_id": "meal",
                        "buyer_id": "agent",
                        "quantity": 1,
                        "total_cents": 2000,
                        "status": "confirmed",
                        "placed_at": 0,
                    }
                ],
            },
        }
    ]
    doc["horizon"] = 20
    doc["max_steps"] = 40
    doc["tasks"] = [
        {"id": "catering", "chat_id": "catering-chat", "message": "Cancel order original."},
        {"id": "venue", "chat_id": "venue-chat", "message": "Move the handoff only."},
    ]
    doc["operator"] = {
        "policy": {
            "default": "allow",
            "rules": [{"service_id": "shop", "operation": "cancel_order", "mode": "approval"}],
        },
        "steps": [],
        "persona": persona(
            {"task_id": "catering", "wants": "Order original cancelled."},
            {
                "task_id": "venue",
                "wants": "Only the handoff moved.",
                "prefers": ["Deny any cancel from this chat; the catering chat handles it."],
            },
        ),
    }
    return doc


def cancel(id_):
    return {
        "kind": "invoke",
        "id": id_,
        "service_id": "shop",
        "operation": "cancel_order",
        "args": {"order_id": "original"},
    }


def by_brief(text, expects):
    if "Task catering (this chat)" in text:
        return '{"decision": "approve", "message": null}'
    return '{"decision": "deny", "message": "The catering chat is handling that."}'


def run_two_chats(doc, transport):
    adapters = {
        "venue-chat": ScriptedAdapter(
            [cancel("venue-1"), cancel("venue-1"), cancel("venue-2"), {"kind": "done"}]
        ),
        "catering-chat": ScriptedAdapter([cancel("cat-1"), cancel("cat-1"), {"kind": "done"}]),
    }
    return run_scenario(resolve_scenario(doc), adapters=adapters, operator_transport=transport)


def test_each_approval_answers_its_own_chat_and_a_denial_is_not_standing():
    result = run_two_chats(two_chats(), FakeModel(by_brief))

    requests = {
        entry["payload"]["id"]: entry["payload"]
        for entry in result.trace
        if entry["kind"] == "operator_request"
    }
    approvals = [
        (entry["invocation_id"], entry["chat_id"], entry["payload"]["decision"])
        for entry in result.trace
        if entry["kind"] == "operator_approval"
    ]
    assert sorted(approvals) == [
        ("cat-1", "catering-chat", "approve"),
        ("venue-1", "venue-chat", "deny"),
        ("venue-2", "venue-chat", "deny"),
    ]
    for entry in result.trace:
        if entry["kind"] == "operator_approval":
            asked = requests[entry["payload"]["request_id"]]
            assert asked["invocation_id"] == entry["invocation_id"]
    assert result.final_state["results"]["cat-1"]["status"] == "succeeded"
    assert "venue-1" not in result.final_state["results"]
    denials = [message for message in operator_messages(result) if message[2].startswith("answer")]
    assert {chat for _, chat, _, _ in denials} == {"venue-chat"} and len(denials) == 2


def test_a_retry_after_a_failure_is_asked_again_in_the_current_conversation():
    doc = two_chats()
    doc["faults"] = [
        {"id": "outage", "service_id": "shop", "operation": "cancel_order", "remaining": 1}
    ]

    def answer(text, expects):
        if expects == "reply":
            return '{"message": "Then do not cancel it."}'
        if "Then do not cancel it." in text:
            return '{"decision": "deny", "message": null}'
        return '{"decision": "approve", "message": null}'

    model = FakeModel(answer)
    adapters = {
        "venue-chat": ScriptedAdapter([{"kind": "done"}]),
        "catering-chat": ScriptedAdapter(
            [
                cancel("cat-1"),
                cancel("cat-1"),
                {"kind": "report", "message": "The vendor now says no refund. Still cancel?"},
                cancel("cat-2"),
                {"kind": "done"},
            ]
        ),
    }
    result = run_scenario(resolve_scenario(doc), adapters=adapters, operator_transport=model)

    assert result.final_state["results"]["cat-1"]["status"] == "failed"
    assert "cat-2" not in result.final_state["results"]
    decisions = [
        (entry["invocation_id"], entry["payload"]["answer"]["decision"])
        for entry in result.trace
        if entry["kind"] == "operator_answer" and entry["payload"]["answer"]["kind"] == "decision"
    ]
    assert decisions == [("cat-1", "approve"), ("cat-2", "deny")]
    assert "approved" in model.prompts[-1][-1]["content"]


MAIL_ARGS = {"to": ["operator"], "subject": "Done", "body": "Sent"}


def send(id_):
    return {
        "kind": "invoke",
        "id": id_,
        "service_id": "mail",
        "operation": "send_message",
        "args": MAIL_ARGS,
    }


def scripted_mail(steps, actions, faults=()):
    doc = copy.deepcopy(BASE)
    doc["faults"] = list(faults)
    rule = {"service_id": "mail", "operation": "send_message", "mode": "approval"}
    doc["operator"] = {"policy": {"default": "deny", "rules": [rule]}, "steps": steps}
    doc["agent"]["actions"] = [*actions, {"kind": "done"}]
    return run_scenario(resolve_scenario(doc))


APPROVE = {
    "kind": "approve",
    "match": {"service_id": "mail", "operation": "send_message", "args": MAIL_ARGS},
}
DENY = {**APPROVE, "kind": "deny"}
POLICY = {"kind": "policy", "service_id": "mail", "operation": "send_message", "mode": "approval"}


@pytest.mark.parametrize(
    "steps,actions,faults,sent",
    [
        (
            [{"at": 1, **APPROVE}, {"at": 1, **POLICY}],
            [send("s1"), {"kind": "wait", "until": 1}, send("s1"), {"kind": "wait", "until": 3}]
            + [send("s1")],
            [],
            False,
        ),
        (
            [{"at": 1, **POLICY}, {"at": 1, **APPROVE}],
            [send("s1"), {"kind": "wait", "until": 1}, send("s1"), {"kind": "wait", "until": 3}]
            + [send("s1")],
            [],
            True,
        ),
        (
            [{"at": 1, **APPROVE}, {"at": 2, **DENY}],
            [
                send("s1"),
                {"kind": "wait", "until": 1},
                send("s1"),
                {"kind": "report", "message": "The mail service failed."},
                send("s2"),
                {"kind": "wait", "until": 3},
                send("s2"),
            ],
            [{"id": "outage", "service_id": "mail", "operation": "send_message", "remaining": 1}],
            False,
        ),
    ],
)
def test_scripted_decisions_apply_in_source_order_to_one_attempt(steps, actions, faults, sent):
    result = scripted_mail(steps, actions, faults)

    assert (result.final_state["services"]["mail"]["state"]["messages"] != []) is sent
    assert result.final_state["pending_operator_steps"] == []
    asked = [
        entry["payload"]["kind"] for entry in result.trace if entry["kind"] == "operator_request"
    ]
    assert "report" not in asked


def without_evidence(trace):
    kept = []
    for entry in trace:
        if entry["kind"] == "operator_answer":
            entry = {**entry, "payload": {"answer": entry["payload"]["answer"]}}
        kept.append(entry)
    return kept


def test_recorded_answers_replay_a_run_exactly_without_a_model():
    recorded = run_two_chats(two_chats(), FakeModel(by_brief))
    replay = run_two_chats(two_chats(), RecordedTransport.from_entries(recorded.trace))

    assert replay.verdict["verdict"] == recorded.verdict["verdict"]
    assert without_evidence(replay.trace) == without_evidence(recorded.trace)
    assert replay.operator_usage.calls == 0
    sources = {
        entry["payload"]["evidence"]["source"]
        for entry in replay.trace
        if entry["kind"] == "operator_answer"
    }
    assert sources == {"recorded"}

    changed = two_chats()
    changed["operator"]["persona"]["briefs"][0]["wants"] = "Keep order original."
    diverged = run_two_chats(changed, RecordedTransport.from_entries(recorded.trace))
    assert diverged.verdict["verdict"] == "invalid"
    assert diverged.verdict["failure"]["kind"] == "infrastructure"
    assert diverged.verdict["failure"]["retryable"] is False


def broken(text, expects):
    raise ConnectionError("provider down")


@pytest.mark.parametrize(
    "answer,model,calls,retryable",
    [
        (broken, {}, 1, True),
        (lambda text, expects: "sure, go ahead", {}, 2, True),
        (lambda text, expects: '{"message": "ok"}', {"max_calls": 1}, 1, False),
    ],
)
def test_an_operator_failure_ends_the_run_without_an_answer(answer, model, calls, retryable):
    doc = single_lane()
    doc["operator"]["persona"]["model"] = model
    doc["agent"]["actions"] = [
        {"kind": "report", "message": "First update."},
        {"kind": "report", "message": "Second update."},
        {"kind": "done"},
    ]
    result = run_scenario(resolve_scenario(doc), operator_transport=FakeModel(answer))

    assert result.verdict["verdict"] == "invalid"
    assert result.verdict["failure"]["kind"] == "infrastructure"
    assert result.verdict["failure"]["retryable"] is retryable
    assert result.operator_usage.calls == calls
    failed = next(entry for entry in result.trace if entry["kind"] == "operator_failure")
    answered = [entry for entry in result.trace if entry["kind"] == "operator_answer"]
    assert failed["payload"]["request_id"] == f"op-{len(answered) + 1}"
    assert len(operator_messages(result)) == 1 + len(answered)


def test_a_persona_scenario_has_one_brief_per_task_and_no_timed_decisions():
    doc = two_chats()
    doc["operator"]["steps"] = [
        {
            "at": 1,
            "kind": "approve",
            "match": {
                "service_id": "shop",
                "operation": "cancel_order",
                "args": {"order_id": "original"},
            },
        }
    ]
    with pytest.raises(ScenarioError, match="timed"):
        resolve_scenario(doc)
    doc = two_chats()
    doc["operator"]["persona"]["briefs"][1]["task_id"] = "audit"
    with pytest.raises(ScenarioError, match="one brief for each task"):
        resolve_scenario(doc)
    with pytest.raises(ScenarioError, match="persona"):
        run_scenario(resolve_scenario(single_lane()))


class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        self._answer()

    def do_GET(self):
        self._answer()

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.seen.append((self.path, self.headers.get("Authorization"), body))
        status, headers, payload = self.server.reply(self.path)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        data = json.dumps(payload).encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@contextmanager
def provider(reply):
    server = HTTPServer(("127.0.0.1", 0), Provider)
    server.seen, server.reply = [], reply
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def completion(usage):
    return {"model": "served", "choices": [{"message": {"content": '{"message": null}'}}], **usage}


TOKENS = {"usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


def transport_for(base_url, **provider_options):
    model = OperatorModel.model_validate(
        {"provider": {"base_url": base_url, "api_key_env": "TEST_KEY", **provider_options}}
    )
    return ModelTransport(model, 7, environ={"TEST_KEY": "sk-test-secret"})


def test_the_model_transport_uses_the_shared_provider_and_never_resends_its_key():
    with provider(lambda path: (200, {}, completion(TOKENS))) as (server, base):
        answer = transport_for(f"{base}/v1", reasoning_effort="none").complete(
            [{"role": "user", "content": "hi"}], "reply"
        )
    ((path, auth, body),) = server.seen
    assert (path, auth) == ("/v1/chat/completions", "Bearer sk-test-secret")
    assert body["reasoning_effort"] == "none" and body["temperature"] == 0 and body["seed"] == 7
    assert answer.usage.cost_usd == pytest.approx((1000 * 0.2 + 100 * 0.75) / 1_000_000)

    with provider(lambda path: (200, {}, completion({}))) as (server, base):
        with pytest.raises(OperatorUnavailable, match="no token usage"):
            transport_for(base).complete([{"role": "user", "content": "hi"}], "reply")

    with provider(lambda path: (200, {}, completion(TOKENS))) as (elsewhere, other):
        moved = {"Location": f"{other}/v1/chat/completions"}
        with provider(lambda path: (302, moved, {})) as (server, base):
            with pytest.raises(OperatorUnavailable, match="redirected"):
                transport_for(base).complete([{"role": "user", "content": "hi"}], "reply")
    assert elsewhere.seen == []


def test_unmeasured_operator_usage_is_not_reported_as_a_spent_budget():
    class Unmetered(FakeModel):
        def complete(self, prompt, expects):
            return Completion(text='{"message": null}', usage=ProcessUsage(calls=1))

    doc = single_lane()
    doc["agent"]["actions"] = [
        {"kind": "report", "message": "One."},
        {"kind": "report", "message": "Two."},
        {"kind": "done"},
    ]
    result = run_scenario(resolve_scenario(doc), operator_transport=Unmetered(None))
    assert result.verdict["failure"]["type"] == "OperatorUnavailable"
    assert "unmeasured" in result.verdict["failure"]["message"]
    assert result.verdict["failure"]["retryable"] is True


def test_the_shared_world_replay_fixture_is_what_its_script_authors(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    from author_operator_answers import ANSWERS, authored_lines

    monkeypatch.chdir(ROOT)
    assert ANSWERS.read_text(encoding="utf-8") == "".join(authored_lines()), (
        "regenerate with: uv run scripts/author_operator_answers.py"
    )


def test_a_scripted_decision_matches_a_request_by_its_value():
    request = {**MAIL_ARGS, "cc": [], "subject": f" {MAIL_ARGS['subject']}\n"}
    action = {**send("s1"), "args": request}
    result = scripted_mail([{"at": 1, **APPROVE}], [action, {"kind": "wait", "until": 2}, action])

    assert result.final_state["services"]["mail"]["state"]["messages"] != []
