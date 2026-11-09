from collections.abc import Callable, Iterable, Mapping

from pydantic import JsonValue

from overnight.domain.contracts import (
    CompoundCondition,
    Condition,
    NegatedCondition,
    Reference,
    ResultEquals,
    ResultExists,
    StateEquals,
    StateExists,
)
from overnight.domain.immutable import ImmutableJson
from overnight.domain.models import Result, json_equal, json_matches
from overnight.domain.values import JsonObject, at_path, json_object, json_value
from overnight.domain.vocabulary import ResultStatus


def state_targets(condition: Condition) -> Iterable[str]:
    match condition:
        case CompoundCondition():
            for child in condition.conditions:
                yield from state_targets(child)
        case NegatedCondition():
            yield from state_targets(condition.condition)
        case StateEquals() | StateExists():
            yield condition.service_id


def satisfied(
    condition: Condition, state_for: Callable[[str], JsonObject], results: Mapping[str, Result]
) -> bool:
    match condition:
        case CompoundCondition(op="all"):
            return all(satisfied(child, state_for, results) for child in condition.conditions)
        case CompoundCondition(op="any"):
            return any(satisfied(child, state_for, results) for child in condition.conditions)
        case NegatedCondition():
            return not satisfied(condition.condition, state_for, results)
        case StateEquals() | StateExists():
            value = state_for(condition.service_id)
        case ResultEquals() | ResultExists():
            result = results.get(condition.result_id)
            if result is None or result.status != ResultStatus.SUCCEEDED:
                return False
            value = result.value
        case _:
            raise ValueError("unknown condition")
    try:
        value = at_path(value, condition.path)
    except KeyError:
        return False
    if isinstance(condition, (StateExists, ResultExists)):
        return True
    assert isinstance(condition, (StateEquals, ResultEquals))
    if condition.op == "match":
        return json_matches(value, condition.value)
    return json_equal(value, condition.value)


def resolve_references(
    value: ImmutableJson | JsonValue,
    owner: tuple[str, str | None],
    trusted_event: bool,
    results: Mapping[str, Result],
    owners: Mapping[str, tuple[str, str | None]],
) -> JsonValue:
    """Replace `$ref` objects with earlier result values. Outside trusted events, a reference
    may read only results of the same actor and chat."""
    if isinstance(value, (list, tuple)):
        return [resolve_references(item, owner, trusted_event, results, owners) for item in value]
    if not isinstance(value, Mapping):
        return value
    if "$ref" not in value:
        return {
            key: resolve_references(item, owner, trusted_event, results, owners)
            for key, item in value.items()
        }
    reference = Reference.model_validate(json_object(value)).target
    prior = results.get(reference.result)
    if prior is None or prior.status != ResultStatus.SUCCEEDED:
        raise ValueError(f"unavailable result reference: {reference.result}")
    if not trusted_event and owners[reference.result] != owner:
        raise PermissionError("result reference not permitted")
    try:
        return json_value(at_path(prior.value, reference.path))
    except KeyError as error:
        raise ValueError(f"missing result reference path: {reference.result}") from error
