import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from overnight.domain.contracts import CommandAgent, Event, Overrides
from overnight.services.calendar import CalendarUpdate
from overnight.services.records import RecordAdd


def _accepted(model, payload):
    try:
        model.model_validate(payload)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize(
    "model,base,field,value",
    [
        (CalendarUpdate, {"id": "event-1"}, "title", "Meeting"),
        (CommandAgent, {"kind": "command", "command": ["agent"]}, "timeout_seconds", 20),
        (Overrides, {}, "faults", []),
    ],
)
@pytest.mark.parametrize("case,valid", [("omitted", True), ("value", True), ("null", False)])
def test_omittable_fields_share_schema_and_runtime_contract(model, base, field, value, case, valid):
    payload = {"omitted": base, "value": {**base, field: value}, "null": {**base, field: None}}[
        case
    ]
    schema = model.model_json_schema(by_alias=True)
    assert schema["properties"][field].get("default", "omitted") is not None
    assert Draft202012Validator(schema).is_valid(payload) is valid
    assert _accepted(model, payload) is valid


def test_nested_record_null_remains_a_valid_json_payload():
    payload = {"collection": "notes", "fields": {"review": None, "nested": {"value": None}}}
    assert Draft202012Validator(RecordAdd.model_json_schema()).is_valid(payload)
    assert RecordAdd.model_validate(payload).fields == {"review": None, "nested": {"value": None}}


def test_duplicate_operation_names_cannot_replace_dispatch():
    from overnight.services.mail import Mail, MailList, MailState
    from overnight.services.operations import OperationSpec, Service

    operation = OperationSpec(MailList, Mail().list_messages, read_only=True)
    with pytest.raises(ValueError, match="Duplicate operation name"):
        Service(MailState, [operation, operation])


def test_world_records_keep_wire_defaults_omitted() -> None:
    contract, payload = (
        Event,
        {
            "id": "event",
            "invocation": {
                "id": "call",
                "actor_id": "agent",
                "service_id": "mail",
                "operation": "list_messages",
            },
        },
    )
    assert Draft202012Validator(contract.model_json_schema()).is_valid(payload)
    assert contract.model_validate(payload).model_dump(exclude_unset=True) == payload
    with pytest.raises(ValidationError):
        contract(**{**payload, "id": ""})


def test_operation_requires_a_documented_handler():
    from overnight.services.operations import NoArguments, OperationSpec

    def undocumented(state, args, context):
        raise AssertionError("not invoked")

    with pytest.raises(ValueError, match="requires a description"):
        OperationSpec(NoArguments, undocumented)
