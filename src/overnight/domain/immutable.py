from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import GetPydanticSchema, JsonValue
from pydantic_core import core_schema

type ImmutableJson = (
    None | bool | int | float | str | tuple[ImmutableJson, ...] | Mapping[str, ImmutableJson]
)


def freeze_json(value: JsonValue) -> ImmutableJson:
    if isinstance(value, dict):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    return value


def export_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: export_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [export_json(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Not a JSON value: {type(value).__name__}")


def _sealed_schema(json_type: object):
    def schema(source, handler):
        return core_schema.no_info_after_validator_function(
            freeze_json,
            core_schema.no_info_before_validator_function(
                export_json, handler.generate_schema(json_type)
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(export_json),
        )

    return schema


type FrozenJson = Annotated[ImmutableJson, GetPydanticSchema(_sealed_schema(JsonValue))]
type FrozenObject = Annotated[
    Mapping[str, ImmutableJson], GetPydanticSchema(_sealed_schema(dict[str, JsonValue]))
]
