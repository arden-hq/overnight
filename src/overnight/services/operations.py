from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from typing import Protocol

from jsonschema import Draft202012Validator, validators
from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError

from overnight.domain.contracts import Contract, Invocation, JsonObject, Reference, Text
from overnight.domain.immutable import FrozenObject
from overnight.domain.models import Actor, Context, Transition
from overnight.domain.values import json_object
from overnight.domain.vocabulary import ResultStatus
from overnight.services.state import State


class Parameters(Contract):
    pass


@cache
def _input_schema(parameters: type[Parameters]) -> JsonObject:
    return parameters.model_json_schema(by_alias=True)


class NoArguments(Parameters):
    pass


class IdArgument(Parameters):
    id: Text


def _allow_reference(validator: Callable) -> Callable:
    def validate(schema_validator, schema_value, instance, schema):
        if isinstance(instance, dict) and "$ref" in instance:
            try:
                Reference.model_validate(instance)
            except ValidationError as error:
                yield SchemaError(f"invalid result reference: {error.errors()[0]['msg']}")
            return
        yield from validator(schema_validator, schema_value, instance, schema)

    return validate


# Strict Pydantic parameters reject 1.0 for an integer, so the template type check does too.
TemplateValidator = validators.extend(
    Draft202012Validator,
    {name: _allow_reference(check) for name, check in Draft202012Validator.VALIDATORS.items()},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _, value: isinstance(value, int) and not isinstance(value, bool)
    ),
)


class OperationDescription(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def read_only(self) -> bool: ...
    @property
    def input_schema(self) -> JsonObject: ...
    def validate_template(self, args: FrozenObject | JsonObject) -> None: ...
    def validate_args(self, args: JsonObject) -> None: ...


class Operation[S: State](OperationDescription, Protocol):
    def invoke(
        self, state: S, args: JsonObject, context: Context
    ) -> JsonObject | Transition[S]: ...


@dataclass(frozen=True)
class OperationSpec[S: State, P: Parameters]:
    parameters: type[P]
    handler: Callable[[S, P, Context], JsonObject | Transition[S]]
    read_only: bool = False

    def __post_init__(self) -> None:
        if not self.description:
            raise ValueError(f"Operation {self.name} requires a description")

    @property
    def name(self) -> str:
        name = getattr(self.handler, "__name__", None)
        if not isinstance(name, str) or not name:
            raise ValueError("Operation handler must have a name")
        return name

    @property
    def description(self) -> str:
        return (self.handler.__doc__ or "").strip()

    @property
    def input_schema(self) -> JsonObject:
        # Every observation lists the operations, so the schema is built once per parameter type
        # for the process; callers get their own copy.
        return deepcopy(_input_schema(self.parameters))

    def invoke(self, state: S, args: JsonObject, context: Context) -> JsonObject | Transition[S]:
        return self.handler(state, self.parameters.model_validate(args), context)

    def validate_args(self, args: JsonObject) -> None:
        self.parameters.model_validate(args)

    def validate_template(self, args: FrozenObject | JsonObject) -> None:
        try:
            TemplateValidator(self.input_schema).validate(json_object(args))
        except SchemaError as error:
            raise ValueError(f"missing or invalid event arguments: {error.message}") from error


class ServiceSessionBoundary(Protocol):
    @property
    def operations(self) -> Mapping[str, OperationDescription]: ...
    def prepare(self, invocation: Invocation, context: Context) -> Transition: ...
    def commit(self, transition: Transition) -> bool: ...
    def observe(self, actor: Actor, context: Context) -> JsonObject: ...
    def snapshot(self) -> JsonObject: ...


class ServiceBoundary(Protocol):
    @property
    def operations(self) -> Mapping[str, OperationDescription]: ...
    def bind(self, state: JsonObject, actors: Mapping[str, Actor]) -> ServiceSessionBoundary: ...


class Service[S: State]:
    def __init__(self, state_model: type[S], operations: list[Operation[S]]) -> None:
        self.state_model = state_model
        self.operations = {operation.name: operation for operation in operations}
        if len(self.operations) != len(operations):
            raise ValueError("Duplicate operation name")

    def bind(self, state: JsonObject, actors: Mapping[str, Actor]) -> ServiceSession[S]:
        return ServiceSession(self, state, actors)

    def validate_actors(self, state: S, actors: Mapping[str, Actor]) -> None:
        raise NotImplementedError

    def visible(self, state: S, actor: Actor, context: Context) -> JsonObject:
        raise NotImplementedError


class ServiceSession[S: State]:
    def __init__(
        self, definition: Service[S], state: JsonObject, actors: Mapping[str, Actor]
    ) -> None:
        self._definition = definition
        self._actors = actors
        self._state = definition.state_model.model_validate(state)
        definition.validate_actors(self._state, actors)

    @property
    def operations(self) -> Mapping[str, Operation[S]]:
        return self._definition.operations

    def prepare(self, invocation: Invocation, context: Context) -> Transition[S]:
        operation = self.operations.get(invocation.operation)
        if operation is None:
            raise ValueError(f"Unknown operation: {invocation.operation}")
        candidate = self._state.model_copy(deep=True)
        result = operation.invoke(candidate, json_object(invocation.args), context)
        if isinstance(result, Transition):
            transition = result
        else:
            transition = Transition(candidate, json_object(result))
        if transition.status != ResultStatus.SUCCEEDED:
            return transition
        if not isinstance(transition.new_state, self._definition.state_model):
            raise TypeError("operation returned state of the wrong service kind")
        # Revalidate after mutation. Pydantic accepts an existing instance without
        # checking changed fields, so validation starts from its Python data.
        validated = self._definition.state_model.model_validate(
            transition.new_state.model_dump(mode="python", by_alias=True, exclude_unset=True)
        )
        self._definition.validate_actors(validated, self._actors)
        return Transition(
            new_state=validated,
            result=json_object(transition.result),
            emissions=tuple(transition.emissions),
            status=transition.status,
            error=transition.error,
            notices=(*context.notices, *transition.notices),
            wait_until=transition.wait_until,
        )

    def commit(self, transition: Transition[S]) -> bool:
        if transition.status != ResultStatus.SUCCEEDED:
            raise ValueError("cannot commit an unsuccessful transition")
        if not isinstance(transition.new_state, self._definition.state_model):
            raise TypeError("operation returned state of the wrong service kind")
        changed = self._state != transition.new_state
        self._state = transition.new_state
        return changed

    def observe(self, actor: Actor, context: Context) -> JsonObject:
        return json_object(self._definition.visible(self._state, actor, context))

    def snapshot(self) -> JsonObject:
        return self._state.json_object()
