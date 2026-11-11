import json
from collections.abc import Callable, Mapping
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, JsonValue, model_validator

from overnight.domain.contracts import Contract, JsonObject, Names, NameSet, Text
from overnight.domain.models import Actor, Context, json_equal
from overnight.domain.values import instant
from overnight.domain.vocabulary import NotificationKind
from overnight.services.common import find_row, next_id, validate_participants
from overnight.services.operations import OperationSpec, Parameters, Service
from overnight.services.state import State, distinct


def trim_fields(fields: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        key: value.strip() if isinstance(value, str) else value for key, value in fields.items()
    }


RecordFields = Annotated[JsonObject, AfterValidator(trim_fields)]


class Record(State):
    model_config = ConfigDict(extra="allow", strict=True, allow_inf_nan=False)
    # Pydantic requires a non-optional dict here to validate extra JSON values.
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)  # ty: ignore[invalid-mutable-override]

    id: Text
    owner_id: Text
    visible_to: NameSet = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def trim_text(cls, data: object) -> object:
        return trim_fields(data) if isinstance(data, dict) else data

    @property
    def custom_fields(self) -> dict[str, JsonValue]:
        return dict(self.__pydantic_extra__)


class AddRule(Contract):
    required: Names = Field(default_factory=list)
    final: bool = False


type FieldType = Literal["text", "integer", "number", "boolean", "time"]

FIELD_TYPES: dict[str, tuple[str, Callable[[JsonValue], bool]]] = {
    "text": ("text", lambda value: isinstance(value, str)),
    "integer": ("an integer", lambda value: type(value) is int),
    "number": ("a number", lambda value: type(value) in (int, float)),
    "boolean": ("true or false", lambda value: type(value) is bool),
    "time": (
        "an ISO 8601 time with a UTC offset",
        lambda value: isinstance(value, str) and instant(value) is not None,
    ),
}


class FieldRule(State):
    """What one record field may hold. A write that does not fit fails and says what fits."""

    type: FieldType | None = None
    values: Annotated[list[JsonValue], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if self.type is not None and self.values is not None:
            if not all(FIELD_TYPES[self.type][1](value) for value in self.values):
                raise ValueError(f"field rule values must be {FIELD_TYPES[self.type][0]}")
        return self

    def problem(self, name: str, value: JsonValue) -> str | None:
        if self.type is not None and not FIELD_TYPES[self.type][1](value):
            return f"Field {name} must be {FIELD_TYPES[self.type][0]}"
        if self.values is not None and not any(json_equal(value, item) for item in self.values):
            allowed = ", ".join(json.dumps(item, ensure_ascii=False) for item in self.values)
            return f"Field {name} must be one of: {allowed}"
        return None


class Collection(State):
    records: list[Record] = Field(default_factory=list)
    add: AddRule | None = None
    update: bool = False
    delete: bool = False
    fields: dict[Text, FieldRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        distinct([row.id for row in self.records], "record")
        if self.fields.keys() & RESERVED_FIELDS:
            raise ValueError("field rules cannot name id, owner_id or visible_to")
        for row in self.records:
            self.check(row)
        return self

    def check(self, row: Record) -> None:
        for name, value in row.custom_fields.items():
            rule = self.fields.get(name)
            problem = None if rule is None else rule.problem(name, value)
            if problem is not None:
                raise ValueError(problem)


class RecordsState(State):
    collections: dict[Text, Collection] = Field(default_factory=dict)


class RecordList(Parameters):
    collection: Text


class RecordGet(RecordList):
    id: Text


class RecordAdd(RecordList):
    fields: RecordFields
    visible_to: NameSet = Field(default_factory=list)


class RecordUpdate(RecordGet):
    fields: RecordFields


def _record_visible(row: Record, actor_id: str) -> bool:
    return row.owner_id == actor_id or actor_id in row.visible_to


class Records(Service[RecordsState]):
    def __init__(self) -> None:
        super().__init__(
            RecordsState,
            [
                OperationSpec(RecordList, self.list_records, read_only=True),
                OperationSpec(RecordGet, self.get_record, read_only=True),
                OperationSpec(RecordAdd, self.add_record),
                OperationSpec(RecordUpdate, self.update_record),
                OperationSpec(RecordGet, self.delete_record),
            ],
        )

    def validate_actors(self, state: RecordsState, actors: Mapping[str, Actor]) -> None:
        for collection in state.collections.values():
            for row in collection.records:
                if row.owner_id not in actors:
                    raise ValueError("Record owner must be a known actor")
                validate_participants(row.visible_to, actors, "visible_to")

    def visible(self, state: RecordsState, actor: Actor, context: Context) -> JsonObject:
        collections: JsonObject = {}
        for name, collection in state.collections.items():
            visible_records: list[JsonValue] = []
            for record in collection.records:
                if _record_visible(record, actor.id):
                    visible_records.append(record.view_object())
            collections[name] = {"records": visible_records}
        return {"as_of": context.now, "collections": collections}

    def _named_collection(self, state: RecordsState, name: str) -> Collection:
        if name not in state.collections:
            raise ValueError(f"Unknown collection: {name}")
        return state.collections[name]

    def _owned_record(self, collection: Collection, record_id: str, actor_id: str) -> Record:
        return find_row(
            collection.records,
            lambda row: row.id == record_id and row.owner_id == actor_id,
            "owned record",
        )

    def list_records(self, state: RecordsState, args: RecordList, context: Context) -> JsonObject:
        """List visible records in a named collection."""
        collection = self._named_collection(state, args.collection)
        return {
            "collection": args.collection,
            "as_of": context.now,
            "records": [
                row.view_object()
                for row in collection.records
                if _record_visible(row, context.actor.id)
            ],
        }

    def get_record(self, state: RecordsState, args: RecordGet, context: Context) -> JsonObject:
        """Read a visible record by collection and id."""
        collection = self._named_collection(state, args.collection)
        row = find_row(
            collection.records,
            lambda row: row.id == args.id and _record_visible(row, context.actor.id),
            "record",
        )
        return row.view_object()

    def add_record(self, state: RecordsState, args: RecordAdd, context: Context) -> JsonObject:
        """Add a record where the collection allows additions."""
        collection = self._named_collection(state, args.collection)
        if collection.add is None:
            raise PermissionError("Collection does not allow additions")
        _validate_record_fields(args.fields)
        validate_participants(args.visible_to, context.actors, "visible_to")
        row = Record.model_validate(
            {
                "id": next_id(args.collection, collection.records),
                "owner_id": context.actor.id,
                "visible_to": args.visible_to,
                **args.fields,
            }
        )
        for field in collection.add.required:
            if row.custom_fields.get(field) in (None, ""):
                raise ValueError(f"Missing required field: {field}")
        collection.check(row)
        collection.records = [*collection.records, row]
        _notify_viewers(args.collection, row, "added", context)
        return {"id": row.id, "collection": args.collection, "final": collection.add.final}

    def update_record(
        self, state: RecordsState, args: RecordUpdate, context: Context
    ) -> JsonObject:
        """Update an owned record where changes are allowed."""
        collection = self._named_collection(state, args.collection)
        if not collection.update:
            raise PermissionError("Collection does not allow this change")
        row = self._owned_record(collection, args.id, context.actor.id)
        _validate_record_fields(args.fields)
        updated = Record.model_validate({**row.view_object(), **args.fields})
        collection.check(updated)
        collection.records = [updated if item.id == row.id else item for item in collection.records]
        _notify_viewers(args.collection, updated, "updated", context)
        return {"id": row.id, "updated": True}

    def delete_record(self, state: RecordsState, args: RecordGet, context: Context) -> JsonObject:
        """Delete an owned record where deletion is allowed."""
        collection = self._named_collection(state, args.collection)
        if not collection.delete:
            raise PermissionError("Collection does not allow this change")
        row = self._owned_record(collection, args.id, context.actor.id)
        collection.records = [item for item in collection.records if item.id != row.id]
        _notify_viewers(args.collection, row, "deleted", context)
        return {"id": row.id, "deleted": True}


def _notify_viewers(collection: str, row: Record, change: str, context: Context) -> None:
    context.notify(
        row.visible_to,
        NotificationKind.RECORD,
        f"{collection}/{row.id}",
        f"Record {collection}/{row.id} {change} by {context.actor.id}",
    )


RESERVED_FIELDS = frozenset({"id", "owner_id", "visible_to"})


def _validate_record_fields(fields: JsonObject) -> None:
    if fields.keys() & RESERVED_FIELDS:
        raise ValueError("fields cannot set identity or visibility")
