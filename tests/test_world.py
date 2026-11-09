import pytest

from overnight.domain.contracts import (
    Actor,
    CompoundCondition,
    Event,
    FaultSpec,
    Invocation,
    NegatedCondition,
    ResultExists,
    StateEquals,
    StateExists,
)
from overnight.domain.models import ServiceInstance, Transition
from overnight.services.operations import NoArguments, OperationSpec, Service
from overnight.services.state import State
from overnight.world.engine import World


class CounterParameters(NoArguments):
    amount: int


class CounterState(State):
    total: int


class CounterHandler(Service[CounterState]):
    def __init__(self, bad_target="actor"):
        self.bad_target = bad_target
        super().__init__(
            CounterState,
            [
                OperationSpec(CounterParameters, self.add),
                OperationSpec(NoArguments, self.get, read_only=True),
                OperationSpec(NoArguments, self.bad_emission),
            ],
        )

    def add(self, state, args, context):
        """Add an amount to the counter."""
        state.total += args.amount
        return {"total": state.total}

    def get(self, state, args, context):
        """Read the counter."""
        return {"total": state.total}

    def bad_emission(self, state, args, context):
        """Produce an invalid event after changing state."""
        state.total += 1
        bad = Event(
            id="bad",
            invocation=Invocation(
                id="bad-call",
                actor_id="missing" if self.bad_target == "actor" else "agent",
                service_id="missing" if self.bad_target == "service" else "counter",
                operation="add",
                args={"amount": 1},
            ),
        )
        return Transition(state, {}, (bad,))

    def validate_actors(self, state, actors):
        pass

    def visible(self, state, actor, context):
        return state.json_object()


@pytest.fixture
def make_world():
    def make(*, events=(), faults=(), actors=None, bad_target="actor"):
        return World(
            service_definitions={"counter": CounterHandler(bad_target)},
            start_time=10,
            horizon=100,
            seed=7,
            actors=actors
            or (
                Actor(
                    id="agent",
                    role="agent",
                    read_services=("counter",),
                    operations={"counter": ("add", "get", "bad_emission")},
                ),
                Actor(
                    id="person",
                    role="person",
                    operations={"counter": ("add", "get")},
                    persona={"secret": "private"},
                ),
            ),
            services=(ServiceInstance("counter", "counter", {"total": 0}),),
            events=events,
            faults=faults,
        )

    return make


def test_equal_time_order_dependencies_and_result_reference(make_world):
    first = Event(
        id="e1",
        invocation=Invocation(
            id="a", actor_id="agent", service_id="counter", operation="add", args={"amount": 2}
        ),
        at=12,
    )
    second = Event(
        id="e2",
        invocation=Invocation(
            id="b", actor_id="agent", service_id="counter", operation="add", args={"amount": 3}
        ),
        at=12,
    )
    dependent = Event(
        id="e3",
        invocation=Invocation(
            id="c",
            actor_id="agent",
            service_id="counter",
            operation="add",
            args={"amount": {"$ref": {"result": "b", "path": ["total"]}}},
        ),
        dependencies=[
            "b",
        ],
        delay=2,
    )
    world = make_world(events=(first, second, dependent))
    assert world.next_wake_time() == 12
    assert [result.id for result in world.advance(12)] == ["a", "b"]
    assert world.next_wake_time() == 14
    assert [result.id for result in world.advance(14)] == ["c"]
    assert world.snapshot_for_evaluator()["services"]["counter"]["state"]["total"] == 10
    assert [entry.invocation_id for entry in world.trace if entry.kind == "event"] == [
        "a",
        "b",
        "c",
    ]


def test_failed_dependency_skips_and_unmet_condition_stays_pending(make_world):
    failed = Event(
        id="fail",
        invocation=Invocation(
            id="a", actor_id="agent", service_id="counter", operation="add", args={"amount": "bad"}
        ),
        at=11,
    )
    dependent = Event(
        id="dependent",
        invocation=Invocation(
            id="b", actor_id="agent", service_id="counter", operation="add", args={"amount": 5}
        ),
        dependencies=[
            "a",
        ],
    )
    conditional = Event(
        id="conditional",
        invocation=Invocation(
            id="c", actor_id="agent", service_id="counter", operation="add", args={"amount": 1}
        ),
        condition={
            "op": "eq",
            "source": "state",
            "service_id": "counter",
            "path": ["total"],
            "value": 9,
        },
    )
    world = make_world(events=(failed, dependent, conditional))
    assert [(result.id, result.status) for result in world.advance(20)] == [
        ("a", "failed"),
        ("b", "skipped"),
    ]
    assert world.pending_events == ("conditional",)
    assert world.next_wake_time() is None
    assert world.snapshot_for_evaluator()["services"]["counter"]["state"]["total"] == 0


def test_actor_observation_and_result_references_are_private(make_world):
    world = make_world()
    private = world.invoke(
        Invocation(
            id="person-call",
            actor_id="person",
            service_id="counter",
            operation="add",
            args={"amount": 4},
        )
    )
    assert private.status == "succeeded"
    observation = world.observe("agent")
    assert "person-call" not in observation["results"]
    assert world.observe("person")["services"] == {}
    stolen = world.invoke(
        Invocation(
            id="steal",
            actor_id="agent",
            service_id="counter",
            operation="add",
            args={"amount": {"$ref": {"result": "person-call", "path": ["total"]}}},
        )
    )
    assert stolen.status == "denied"
    assert world.snapshot_for_evaluator()["services"]["counter"]["state"]["total"] == 4
    assert "secret" not in str(observation)


@pytest.mark.parametrize(
    ("bad_target", "error"), [("actor", "event actor"), ("service", "event service")]
)
def test_fault_and_invalid_emission_do_not_commit(make_world, bad_target, error):
    world = make_world(
        faults=(FaultSpec(id="outage", service_id="counter", operation="add", remaining=1),),
        bad_target=bad_target,
    )
    assert (
        world.invoke(
            Invocation(
                id="a", actor_id="agent", service_id="counter", operation="add", args={"amount": 2}
            )
        ).status
        == "failed"
    )
    assert (
        world.invoke(
            Invocation(
                id="b", actor_id="agent", service_id="counter", operation="add", args={"amount": 2}
            )
        ).status
        == "succeeded"
    )
    with pytest.raises(ValueError, match=error):
        world.invoke(
            Invocation(id="c", actor_id="agent", service_id="counter", operation="bad_emission")
        )
    assert world.snapshot_for_evaluator()["services"]["counter"]["state"]["total"] == 2
    assert world.pending_events == ()


def test_snapshot_is_mutable_export_but_recorded_facts_are_immutable(make_world):
    world = make_world()
    world.invoke(Invocation(id="a", actor_id="agent", service_id="counter", operation="get"))
    with pytest.raises(TypeError):
        world.trace[-1].payload["value"]["total"] = 99
    with pytest.raises(TypeError):
        world.results["a"].value["total"] = 99
    world.snapshot_for_evaluator()["services"]["counter"]["state"]["total"] = 99
    assert world.results["a"].value["total"] == 0
    assert world.trace[-1].payload["value"]["total"] == 0
    assert world.snapshot_for_evaluator()["services"]["counter"]["state"]["total"] == 0


def test_trace_distinguishes_reads_from_state_changes(make_world):
    world = make_world()
    world.invoke(Invocation(id="read", actor_id="agent", service_id="counter", operation="get"))
    world.invoke(
        Invocation(
            id="write", actor_id="agent", service_id="counter", operation="add", args={"amount": 2}
        )
    )
    changes = [entry.invocation_id for entry in world.trace if entry.kind == "state_change"]
    assert changes == ["write"]
    assert [entry.invocation_id for entry in world.trace if entry.kind == "result"] == [
        "read",
        "write",
    ]


def test_trace_records_effective_arguments_and_original_reference(make_world):
    world = make_world()
    world.invoke(
        Invocation(
            id="seed", actor_id="agent", service_id="counter", operation="add", args={"amount": 2}
        )
    )
    world.invoke(Invocation(id="read", actor_id="agent", service_id="counter", operation="get"))
    reference = {"$ref": {"result": "read", "path": ["total"]}}
    world.invoke(
        Invocation(
            id="linked",
            actor_id="agent",
            service_id="counter",
            operation="add",
            args={"amount": reference},
        )
    )
    dispatch = next(
        entry
        for entry in world.trace
        if entry.kind == "invocation" and entry.invocation_id == "linked"
    )
    assert dispatch.payload["args"] == {"amount": 2}
    assert dispatch.model_dump(mode="json")["payload"]["requested_args"] == {"amount": reference}
    assert world.results["linked"].status == "succeeded"


def test_condition_distinguishes_json_boolean_from_number(make_world):
    boolean_event = Event(
        id="bool-event",
        invocation=Invocation(
            id="bool-call",
            actor_id="agent",
            service_id="counter",
            operation="add",
            args={"amount": 4},
        ),
        condition={
            "op": "eq",
            "source": "state",
            "service_id": "counter",
            "path": ["total"],
            "value": True,
        },
    )
    world = make_world(events=(boolean_event,))
    world.invoke(
        Invocation(
            id="seed", actor_id="agent", service_id="counter", operation="add", args={"amount": 1}
        )
    )
    assert world.advance(20) == ()
    assert world.pending_events == ("bool-event",)

    number_event = Event(
        id="number-event",
        invocation=Invocation(
            id="number-call",
            actor_id="agent",
            service_id="counter",
            operation="add",
            args={"amount": 4},
        ),
        condition={
            "op": "eq",
            "source": "state",
            "service_id": "counter",
            "path": ["total"],
            "value": 1,
        },
    )
    number_world = make_world(events=(number_event,))
    number_world.invoke(
        Invocation(
            id="seed", actor_id="agent", service_id="counter", operation="add", args={"amount": 1}
        )
    )
    assert [result.id for result in number_world.advance(20)] == ["number-call"]


def test_invalid_dependency_graph_rejected_at_setup(make_world):
    missing = Event(
        id="a",
        invocation=Invocation(id="a", actor_id="agent", service_id="counter", operation="get"),
        dependencies=[
            "unknown",
        ],
    )
    with pytest.raises(ValueError, match="missing event dependency"):
        make_world(events=(missing,))
    a = Event(
        id="a",
        invocation=Invocation(id="a", actor_id="agent", service_id="counter", operation="get"),
        dependencies=[
            "b",
        ],
    )
    b = Event(
        id="b",
        invocation=Invocation(id="b", actor_id="agent", service_id="counter", operation="get"),
        dependencies=[
            "a",
        ],
    )
    with pytest.raises(ValueError, match="cyclic event dependency"):
        make_world(events=(a, b))


def test_queued_conditions_are_isolated_from_scenario_mutation(make_world):
    amount = {"amount": 4}
    conditions = [
        StateEquals(op="eq", source="state", service_id="counter", path=["total"], value=1),
        NegatedCondition(
            op="not",
            condition=ResultExists(
                op="exists",
                source="result",
                result_id="missing",
                path=[],
            ),
        ),
    ]
    event = Event(
        id="conditional",
        invocation=Invocation(
            id="conditional-call",
            actor_id="agent",
            service_id="counter",
            operation="add",
            args=amount,
        ),
        condition=CompoundCondition(op="all", conditions=conditions),
    )
    world = make_world(events=(event,))
    assert isinstance(event.condition, CompoundCondition)
    amount["amount"] = 99
    conditions.clear()
    with pytest.raises(AttributeError):
        event.condition.conditions.clear()
    with pytest.raises(TypeError):
        event.invocation.args["amount"] = 99
    assert world.advance(11) == ()
    world.invoke(
        Invocation(
            id="seed", actor_id="agent", service_id="counter", operation="add", args={"amount": 1}
        )
    )
    assert [result.id for result in world.advance(12)] == ["conditional-call"]
    assert world.results["conditional-call"].value == {"total": 5}


def test_direct_world_rejects_condition_on_unknown_service(make_world):
    event = Event(
        id="conditional",
        invocation=Invocation(id="call", actor_id="agent", service_id="counter", operation="get"),
        condition=StateExists(op="exists", source="state", service_id="private", path=[]),
    )
    with pytest.raises(ValueError, match="condition targets unknown service"):
        make_world(events=(event,))


def _add_event(event_id, call_id, **kwargs):
    return Event(
        id=event_id,
        invocation=Invocation(
            id=call_id, actor_id="agent", service_id="counter", operation="add", args={"amount": 1}
        ),
        **kwargs,
    )


def test_failed_dependency_skips_despite_unresolved_sibling(make_world):
    bad = Event(
        id="fail",
        invocation=Invocation(
            id="a", actor_id="agent", service_id="counter", operation="add", args={"amount": "bad"}
        ),
        at=11,
    )
    later = _add_event("later", "b", at=500)
    dependent = _add_event("dependent", "c", dependencies=["a", "b"])
    world = make_world(events=(bad, later, dependent))
    assert [(result.id, result.status) for result in world.advance(20)] == [
        ("a", "failed"),
        ("c", "skipped"),
    ]
    assert world.pending_events == ("later",)


def test_delay_without_dependencies_is_rejected(make_world):
    with pytest.raises(ValueError, match="delay requires dependencies"):
        make_world(events=(_add_event("e", "a", delay=5),))


def test_malformed_call_does_not_spend_fault_budget(make_world):
    world = make_world(
        faults=(FaultSpec(id="outage", service_id="counter", operation="add", remaining=1),)
    )
    malformed = Invocation(
        id="bad", actor_id="agent", service_id="counter", operation="add", args={"amount": "x"}
    )
    assert world.invoke(malformed).status == "failed"
    assert world.snapshot_for_evaluator()["faults_remaining"] == {"outage": 1}


def test_unknown_fault_operation_or_grant_is_rejected(make_world):
    with pytest.raises(ValueError, match="unknown operation"):
        make_world(faults=(FaultSpec(id="f", service_id="counter", operation="ad"),))
    typo = Actor(id="agent", role="agent", operations={"counter": ("ad",)})
    with pytest.raises(ValueError, match="unknown operation"):
        make_world(actors=(typo,))


def test_non_value_error_in_event_still_resolves_dependents(make_world, monkeypatch):
    first = _add_event("e1", "a", at=11)
    dependent = _add_event("e2", "b", dependencies=["a"])
    world = make_world(events=(first, dependent))

    def explode(*args, **kwargs):
        raise TypeError("boom")

    monkeypatch.setattr(world, "_apply_operation", explode)
    with pytest.raises(TypeError):
        world.advance(20)
    monkeypatch.undo()
    assert world.results["a"].status == "failed"
    assert [(result.id, result.status) for result in world.advance(20)] == [("b", "skipped")]


def test_validation_failure_evidence_omits_pydantic_documentation_urls(make_world):
    world = make_world()
    result = world.invoke(
        Invocation(
            id="bad-type",
            actor_id="agent",
            service_id="counter",
            operation="add",
            args={"amount": "one"},
        )
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.startswith("CounterParameters: amount:")
    assert "https://" not in result.error
    assert world.results["bad-type"].error == result.error


def _notifying_world(events=()):
    from overnight.services.catalog import BUILTIN_SERVICES

    writes = {"mail": ("send_message",), "chat": ("send_message",)}
    world = World(
        service_definitions=BUILTIN_SERVICES,
        start_time=0,
        horizon=10,
        actors=(
            Actor(id="agent", role="agent", read_services=("mail", "chat"), operations=writes),
            Actor(id="blind", role="agent"),
            Actor(id="vendor", role="person", operations=writes),
        ),
        services=(
            ServiceInstance("mail", "mail", {"messages": []}),
            ServiceInstance(
                "chat",
                "chat",
                {
                    "conversations": [
                        {"id": "support", "participants": ["agent", "vendor", "blind"]}
                    ]
                },
            ),
        ),
        events=events,
    )
    for actor_id in ("agent", "blind", "vendor"):
        world.open_inbox(actor_id, None)
    return world


def _call(id_, operation, args, actor_id="agent", service_id="system"):
    return Invocation(
        id=id_, actor_id=actor_id, service_id=service_id, operation=operation, args=args
    )


def _vendor_mail(at):
    args = {"to": ["agent"], "subject": "Refund", "body": "Eligible"}
    return Event(
        id=f"mail-at-{at}",
        at=at,
        invocation=_call(f"mail-at-{at}-call", "send_message", args, "vendor", "mail"),
    )


def _notification_trace(world):
    return [entry.payload for entry in world.trace if entry.kind == "notification"]


def test_another_actors_write_notifies_each_addressed_agent_once():
    world = _notifying_world()
    world.advance(3)
    own = {"to": ["agent", "vendor"], "subject": "Own", "body": "Mine"}
    world.invoke(_call("own-mail", "send_message", own, service_id="mail"))
    world.invoke(
        _call(
            "own-chat",
            "send_message",
            {"conversation_id": "support", "body": "Hi"},
            "agent",
            "chat",
        )
    )
    assert _notification_trace(world) == []
    mail = {"to": ["agent", "blind"], "cc": ["agent"], "subject": "Refund", "body": "Eligible"}
    world.invoke(_call("vendor-mail", "send_message", mail, "vendor", "mail"))
    chat = {"conversation_id": "support", "body": "Eligible"}
    world.invoke(_call("vendor-chat", "send_message", chat, "vendor", "chat"))
    assert [(item["recipient"], item["cause"]) for item in _notification_trace(world)] == [
        ("agent", "vendor-mail"),
        ("agent", "vendor-chat"),
    ]
    notes = world.undelivered("agent", None)
    assert [(note.kind, note.ref, note.time, note.service_id) for note in notes] == [
        ("mail_received", "mail-2", 3, "mail"),
        ("chat_message", "support", 3, "chat"),
    ]
    assert notes[0].summary == "New mail from vendor: Refund"
    assert world.undelivered("blind", None) == world.undelivered("vendor", None) == []


def test_a_reply_goes_to_the_chat_that_last_wrote_in_that_thread():
    world = _notifying_world()
    for chat_id in ("catering", "venue"):
        world.open_inbox("agent", chat_id)
    vendor = {"conversation_id": "support", "body": "Status update"}
    world.invoke(_call("before", "send_message", vendor, "vendor", "chat"))
    asked = {"conversation_id": "support", "body": "Can it be cancelled?"}
    for ask in ("ask", "ask-again"):
        world.invoke(
            _call(ask, "send_message", asked, "agent", "chat").model_copy(
                update={"chat_id": "catering"}
            )
        )
    world.invoke(_call("after", "send_message", vendor, "vendor", "chat"))
    causes = {
        chat_id: [note.id for note in world.undelivered("agent", chat_id)]
        for chat_id in ("catering", "venue")
    }
    before, ask, after = (
        next(item["id"] for item in _notification_trace(world) if item["cause"] == cause)
        for cause in ("before", "ask", "after")
    )
    assert causes == {"catering": [before, after], "venue": [before, ask]}


def test_a_chat_sees_and_references_only_its_own_results():
    world = _notifying_world()
    sent = {"conversation_id": "support", "body": "Private to catering"}
    world.invoke(
        _call("catering-send", "send_message", sent, "agent", "chat").model_copy(
            update={"chat_id": "catering"}
        )
    )
    assert list(world.observe("agent", "catering")["results"]) == ["catering-send"]
    assert world.observe("agent", "venue")["results"] == {}
    assert list(world.observe("agent")["results"]) == ["catering-send"]
    copied = {
        "conversation_id": "support",
        "body": {"$ref": {"result": "catering-send", "path": ["id"]}},
    }
    result = world.invoke(
        _call("venue-copy", "send_message", copied, "agent", "chat").model_copy(
            update={"chat_id": "venue"}
        )
    )
    assert (result.status, result.error) == ("denied", "result reference not permitted")


def test_a_wait_returns_on_the_first_notification_on_timeout_or_at_once():
    from overnight.domain.models import PendingWait

    world = _notifying_world(events=[_vendor_mail(4)])
    pending = world.invoke(_call("wait-mail", "wait_for_notification", {"timeout": 10}))
    assert isinstance(pending, PendingWait) and pending.until == 10
    assert world.next_wake_time() == 4
    world.advance(6)
    first = world.results["wait-mail"]
    assert (first.time, first.value["timed_out"]) == (4, False)
    assert [note["kind"] for note in first.value["notifications"]] == ["mail_received"]
    assert world.undelivered("agent", None) == []

    assert isinstance(
        world.invoke(_call("wait-quiet", "wait_for_notification", {"timeout": 2})), PendingWait
    )
    world.advance(9)
    quiet = world.results["wait-quiet"]
    assert (quiet.time, quiet.value["timed_out"], quiet.value["notifications"]) == (8, True, ())

    world.invoke(
        _call(
            "late-mail",
            "send_message",
            {"to": ["agent"], "subject": "Late", "body": "."},
            "vendor",
            "mail",
        )
    )
    ready = world.invoke(_call("wait-ready", "wait_for_notification", {"timeout": 5}))
    assert not isinstance(ready, PendingWait)
    assert (ready.time, [note["ref"] for note in ready.value["notifications"]]) == (9, ["mail-2"])


def test_reminders_and_waits_end_at_the_horizon():
    from overnight.domain.models import PendingWait

    world = _notifying_world()
    late = world.invoke(_call("late", "set_reminder", {"message": "Too late", "at": 11}))
    assert late.status == "failed"
    both = world.invoke(_call("both", "set_reminder", {"message": "x", "at": 5, "delay": 5}))
    assert both.status == "failed"
    pending = world.invoke(_call("wait", "wait_for_notification", {"timeout": 50}))
    assert isinstance(pending, PendingWait) and pending.until == 10
    world.advance(10)
    result = world.results["wait"]
    assert (result.time, result.value["timed_out"]) == (10, True)
    clock = world.invoke(_call("clock", "current_time", {}))
    assert clock.value == {"time": 10, "horizon": 10}


def test_a_reminder_fires_once_and_a_repeated_set_reuses_it():
    world = _notifying_world()
    world.advance(2)
    reminder = {"message": "Check the refund", "delay": 3}
    first = world.invoke(_call("remind", "set_reminder", reminder))
    again = world.invoke(_call("remind-again", "set_reminder", reminder))
    assert first.value == {"id": "reminder-1", "due": 5, "created": True}
    assert again.value == {"id": "reminder-1", "due": 5, "created": False}
    world.advance(4)
    assert world.undelivered("agent", None) == []
    world.advance(9)
    world.advance(10)
    notes = world.undelivered("agent", None)
    assert [(note.kind, note.ref, note.time, note.summary) for note in notes] == [
        ("reminder_due", "reminder-1", 5, "Check the refund")
    ]
    listed = world.invoke(_call("list", "list_reminders", {}))
    assert [row["status"] for row in listed.value["reminders"]] == ["fired"]
    assert world.invoke(_call("other-list", "list_reminders", {}, "blind")).value == {
        "reminders": ()
    }


def test_cancelling_a_reminder_twice_changes_it_once_and_nothing_fires():
    world = _notifying_world()
    world.invoke(_call("remind", "set_reminder", {"message": "Ping", "at": 3}))
    foreign = world.invoke(_call("steal", "cancel_reminder", {"id": "reminder-1"}, "blind"))
    first = world.invoke(_call("cancel", "cancel_reminder", {"id": "reminder-1"}))
    second = world.invoke(_call("cancel-again", "cancel_reminder", {"id": "reminder-1"}))
    assert foreign.status == "failed"
    assert first.value == {"id": "reminder-1", "status": "cancelled", "changed": True}
    assert (second.status, second.value["changed"]) == ("succeeded", False)
    world.advance(10)
    assert world.undelivered("agent", None) == []
    assert world.next_wake_time() is None
