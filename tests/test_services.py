from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from overnight.domain.contracts import Actor, Invocation
from overnight.domain.models import Context
from overnight.services.calendar import CalendarUpdate
from overnight.services.catalog import BUILTIN_SERVICES
from overnight.services.state import State

ACTORS = {
    actor.id: actor
    for actor in (
        Actor(id="agent", role="agent"),
        Actor(id="person", role="person"),
        Actor(id="other", role="person"),
    )
}


def context(actor_id: str) -> Context:
    return Context(100, ACTORS[actor_id], ACTORS, "service")


def invocation(actor_id: str, operation: str, args: dict) -> Invocation:
    return Invocation(
        id="call", actor_id=actor_id, service_id="service", operation=operation, args=args
    )


def bind(kind: str, state: dict | State, actors: dict = ACTORS):
    source = state.json_object() if isinstance(state, State) else state
    return BUILTIN_SERVICES[kind].bind(source, actors)


def prepare(kind: str, state: dict, call: Invocation, ctx: Context):
    return bind(kind, state).prepare(call, ctx)


def observe(kind: str, state: dict, actor: Actor, ctx: Context):
    return bind(kind, state).observe(actor, ctx)


def test_mail_bulk_delete_is_atomic_and_recipient_scoped() -> None:
    state = {
        "messages": [
            {
                "id": "a",
                "from": "person",
                "to": ["agent", "other"],
                "subject": "First",
                "body": "A",
                "date": 1,
            },
            {
                "id": "b",
                "from": "person",
                "to": ["agent"],
                "subject": "Second",
                "body": "B",
                "date": 2,
            },
        ]
    }
    baseline = deepcopy(state)
    session = bind("mail", state)
    before = session.snapshot()
    with pytest.raises(ValueError, match="Unknown inbox mail"):
        session.prepare(
            invocation("agent", "delete_messages", {"ids": ["a", "missing"]}),
            context("agent"),
        )
    assert session.snapshot() == before

    changed = session.prepare(
        invocation("agent", "delete_messages", {"ids": ["a"]}), context("agent")
    )
    assert len(session.observe(ACTORS["agent"], context("agent"))["messages"]) == 2
    session.commit(changed)
    assert session.observe(ACTORS["agent"], context("agent"))["messages"] == [
        baseline["messages"][1]
    ]
    assert session.observe(ACTORS["other"], context("other"))["messages"] == [
        baseline["messages"][0]
    ]
    assert state == baseline


def test_unsent_mail_is_visible_only_to_its_author() -> None:
    state = {
        "messages": [
            {
                "id": "draft",
                "from": "person",
                "to": ["agent"],
                "subject": "Not sent",
                "body": "private",
                "date": 1,
                "draft": True,
            }
        ]
    }
    assert observe("mail", state, ACTORS["agent"], context("agent"))["messages"] == []
    assert (
        observe("mail", state, ACTORS["person"], context("person"))["messages"][0]["id"] == "draft"
    )


def test_chat_private_conversation_and_sender_identity() -> None:
    state = {
        "conversations": [
            {
                "id": "private",
                "participants": ["person", "other"],
                "messages": [{"id": "m1", "from": "person", "body": "secret", "date": 1}],
            },
            {"id": "shared", "participants": ["agent", "person"], "messages": []},
        ]
    }
    assert [
        row["id"]
        for row in observe("chat", state, ACTORS["agent"], context("agent"))["conversations"]
    ] == ["shared"]
    with pytest.raises(ValueError, match="Unknown conversation"):
        prepare(
            "chat",
            state,
            invocation("agent", "read_messages", {"conversation_id": "private"}),
            context("agent"),
        )
    with pytest.raises(ValueError):
        prepare(
            "chat",
            state,
            invocation(
                "agent",
                "send_message",
                {"conversation_id": "shared", "body": "hello", "from": "other"},
            ),
            context("agent"),
        )
    sent = prepare(
        "chat",
        state,
        invocation("agent", "send_message", {"conversation_id": "shared", "body": "hello"}),
        context("agent"),
    )
    assert sent.new_state.json_object()["conversations"][1]["messages"][0]["from"] == "agent"
    assert state["conversations"][1]["messages"] == []


def test_calendar_attendee_can_read_but_cannot_change_owner_event() -> None:
    state = {
        "events": [
            {
                "id": "e1",
                "owner_id": "person",
                "title": "Visit",
                "start": "2026-10-22T12:00:00Z",
                "end": "2026-10-22T13:00:00Z",
                "attendees": ["agent"],
            }
        ]
    }
    assert observe("calendar", state, ACTORS["agent"], context("agent"))["events"][0]["id"] == "e1"
    with pytest.raises(ValueError, match="Unknown owned event"):
        prepare(
            "calendar", state, invocation("agent", "delete_event", {"id": "e1"}), context("agent")
        )
    assert state["events"][0]["id"] == "e1"


def test_shop_rejects_oversell_and_cancel_restores_stock_once() -> None:
    state = {
        "catalog": [{"id": "seat", "title": "Seat", "price_cents": 1500, "available": 2}],
        "orders": [],
    }
    with pytest.raises(ValueError, match="Insufficient stock"):
        prepare(
            "shop",
            state,
            invocation("agent", "buy", {"item_id": "seat", "quantity": 3}),
            context("agent"),
        )
    assert state["catalog"][0]["available"] == 2
    bought = prepare(
        "shop",
        state,
        invocation("agent", "buy", {"item_id": "seat", "quantity": 2}),
        context("agent"),
    )
    assert bought.result == {"order_id": "order-1", "total_cents": 3000, "status": "confirmed"}
    assert bought.new_state.json_object()["catalog"][0]["available"] == 0
    with pytest.raises(ValueError, match="Unknown order"):
        prepare(
            "shop",
            bought.new_state,
            invocation("other", "cancel_order", {"order_id": "order-1"}),
            context("other"),
        )
    cancelled = prepare(
        "shop",
        bought.new_state,
        invocation("agent", "cancel_order", {"order_id": "order-1"}),
        context("agent"),
    )
    assert cancelled.new_state.json_object()["catalog"][0]["available"] == 2
    with pytest.raises(ValueError, match="Order is not active"):
        prepare(
            "shop",
            cancelled.new_state,
            invocation("agent", "cancel_order", {"order_id": "order-1"}),
            context("agent"),
        )


def test_records_visibility_and_owner_changes() -> None:
    state = {
        "collections": {
            "notes": {
                "records": [
                    {"id": "n1", "owner_id": "person", "visible_to": ["agent"], "text": "shared"},
                    {"id": "n2", "owner_id": "person", "text": "private"},
                ],
                "add": {"required": ["text"]},
                "update": True,
                "delete": True,
            }
        }
    }
    visible = prepare(
        "records",
        state,
        invocation("agent", "list_records", {"collection": "notes"}),
        context("agent"),
    )
    assert [row["id"] for row in visible.result["records"]] == ["n1"]
    with pytest.raises(ValueError, match="Unknown owned record"):
        prepare(
            "records",
            state,
            invocation(
                "agent",
                "update_record",
                {"collection": "notes", "id": "n1", "fields": {"text": "changed"}},
            ),
            context("agent"),
        )
    with pytest.raises(ValueError, match="identity or visibility"):
        prepare(
            "records",
            state,
            invocation(
                "agent",
                "add_record",
                {"collection": "notes", "fields": {"text": "new", "owner_id": "person"}},
            ),
            context("agent"),
        )
    assert state["collections"]["notes"]["records"][0]["text"] == "shared"


def test_seed_validation_rejects_unknown_private_actor() -> None:
    with pytest.raises(ValueError, match="known actor"):
        bind(
            "chat",
            {"conversations": [{"id": "c1", "participants": ["agent", "missing"], "messages": []}]},
            ACTORS,
        )


def test_shop_seed_rejects_orders_that_cannot_be_accounted_for() -> None:
    order, error = (
        {
            "id": "o1",
            "item_id": "missing",
            "buyer_id": "agent",
            "quantity": 1,
            "total_cents": 1500,
            "status": "confirmed",
            "placed_at": 1,
        },
        "unknown item",
    )
    state = {
        "catalog": [{"id": "seat", "title": "Seat", "price_cents": 1500, "available": 0}],
        "orders": [order],
    }
    with pytest.raises(ValueError, match=error):
        bind("shop", state, ACTORS)


def test_records_seed_rejects_text_that_looks_like_a_final_flag() -> None:
    state = {
        "collections": {
            "submissions": {"records": [], "add": {"required": ["text"], "final": "false"}}
        }
    }
    with pytest.raises(ValueError, match=r"add\.final"):
        bind("records", state, ACTORS)


def test_service_structural_typos_fail_instead_of_becoming_empty_state() -> None:
    kind, state = ("records", {"collections": {"notes": {"record": []}}})
    with pytest.raises(ValueError):
        bind(kind, state, ACTORS)


def test_calendar_orders_events_by_instant_across_utc_offsets() -> None:
    state = {
        "events": [
            {
                "id": "later",
                "owner_id": "agent",
                "title": "Later",
                "start": "2026-10-22T08:00:00Z",
                "end": "2026-10-22T09:00:00Z",
            },
            {
                "id": "earlier",
                "owner_id": "agent",
                "title": "Earlier",
                "start": "2026-10-22T10:00:00+04:00",
                "end": "2026-10-22T11:00:00+04:00",
            },
        ]
    }
    result = prepare("calendar", state, invocation("agent", "list_events", {}), context("agent"))
    assert [event["id"] for event in result.result["events"]] == ["earlier", "later"]


def test_recipient_wire_schema_distinguishes_send_from_draft() -> None:
    send = BUILTIN_SERVICES["mail"].operations["send_message"].input_schema
    draft = BUILTIN_SERVICES["mail"].operations["save_draft"].input_schema
    assert send["properties"]["to"]["minItems"] == 1
    assert "minItems" not in draft["properties"]["to"]


def test_mail_rejects_unknown_recipients_in_send_and_seed() -> None:
    with pytest.raises(ValueError, match="known actors"):
        prepare(
            "mail",
            {"messages": []},
            invocation("agent", "send_message", {"to": ["ghost"], "subject": "s", "body": "b"}),
            context("agent"),
        )
    seed = {"id": "m", "from": "person", "to": ["ghost"], "subject": "s", "body": "b", "date": 1}
    with pytest.raises(ValueError, match="known actors"):
        bind("mail", {"messages": [seed]})


def test_snapshot_ignores_whether_defaults_were_authored() -> None:
    row = {"id": "m", "from": "person", "to": ["agent"], "subject": "s", "body": "b", "date": 1}
    omitted = bind("mail", {"messages": [row]}).snapshot()
    explicit = bind("mail", {"messages": [{**row, "draft": False}]}).snapshot()
    assert omitted == explicit


def test_template_validation_matches_strict_dispatch() -> None:
    buy = BUILTIN_SERVICES["shop"].operations["buy"]
    reference = {"$ref": {"result": "earlier", "path": ["n"]}}
    buy.validate_template({"item_id": "x", "quantity": reference})
    with pytest.raises(ValueError, match="invalid event arguments"):
        buy.validate_template({"item_id": "x", "quantity": 1.0})
    with pytest.raises(ValueError, match="invalid result reference"):
        buy.validate_template({"item_id": "x", "quantity": {"$ref": 5}})


def test_calendar_update_null_clears_location_while_omission_keeps_it() -> None:
    state = {
        "events": [
            {
                "id": "e1",
                "owner_id": "person",
                "title": "Visit",
                "start": "2026-10-22T12:00:00Z",
                "end": "2026-10-22T13:00:00Z",
                "location": "Room 1",
            }
        ]
    }
    schema = CalendarUpdate.model_json_schema(by_alias=True)
    assert Draft202012Validator(schema).is_valid({"id": "e1", "location": None})
    assert not Draft202012Validator(schema).is_valid({"id": "e1", "title": None})
    session = bind("calendar", state)
    session.commit(
        session.prepare(
            invocation("person", "update_event", {"id": "e1", "title": "Visit 2"}),
            context("person"),
        )
    )
    assert session.snapshot()["events"][0]["location"] == "Room 1"
    session.commit(
        session.prepare(
            invocation("person", "update_event", {"id": "e1", "location": None}),
            context("person"),
        )
    )
    assert session.snapshot()["events"][0]["location"] is None
    with pytest.raises(ValueError):
        session.prepare(
            invocation("person", "update_event", {"id": "e1", "title": None}),
            context("person"),
        )


def test_records_fields_must_fit_their_collection_rules() -> None:
    rules = {
        "status": {"values": ["kept", "cancelled"]},
        "amount_cents": {"type": "integer"},
        "due": {"type": "time"},
    }
    state = {"collections": {"audit": {"records": [], "add": {"required": []}, "fields": rules}}}

    def add(fields: dict):
        return prepare(
            "records",
            state,
            invocation("agent", "add_record", {"collection": "audit", "fields": fields}),
            context("agent"),
        )

    added = add({"status": " kept ", "amount_cents": 40500, "due": "2026-10-22T11:00:00+04:00"})
    assert added.new_state.json_object()["collections"]["audit"]["records"][0]["status"] == "kept"
    for fields, named in (
        ({"status": "Kept"}, r'status.*"kept", "cancelled"'),
        ({"amount_cents": "40500"}, "amount_cents.*integer"),
        ({"due": "2026-10-22"}, "due.*time"),
    ):
        with pytest.raises(ValueError, match=named):
            add(fields)
    seeded = deepcopy(state)
    seeded["collections"]["audit"]["records"] = [{"id": "a", "owner_id": "agent", "status": "Kept"}]
    with pytest.raises(ValueError, match="status"):
        bind("records", seeded)
