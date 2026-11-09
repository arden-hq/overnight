import json
from collections.abc import Mapping, Sequence

from pydantic import JsonValue, TypeAdapter

from overnight.domain.immutable import ImmutableJson, export_json

type JsonObject = dict[str, JsonValue]

_json_value = TypeAdapter(JsonValue)
_json_object = TypeAdapter(JsonObject)


def at_path(
    value: ImmutableJson | JsonValue, parts: Sequence[str | int]
) -> ImmutableJson | JsonValue:
    for part in parts:
        if isinstance(value, Mapping) and isinstance(part, str) and part in value:
            value = value[part]
        elif isinstance(value, (list, tuple)) and type(part) is int and 0 <= part < len(value):
            value = value[part]
        else:
            raise KeyError(part)
    return value


def json_value(value: object) -> JsonValue:
    value = export_json(value)
    json.dumps(value, allow_nan=False)
    return _json_value.validate_python(value, strict=True)


def json_object(value: object) -> JsonObject:
    value = export_json(value)
    json.dumps(value, allow_nan=False)
    return _json_object.validate_python(value, strict=True)
