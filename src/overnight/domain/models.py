from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from overnight.domain.contracts import Actor, Event, Fact, Invocation, JsonObject
from overnight.domain.immutable import FrozenJson
from overnight.domain.values import text_equal
from overnight.domain.vocabulary import NotificationKind, ResultStatus


def json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is bool and type(right) is bool and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return text_equal(left, right)
    if isinstance(left, Mapping):
        if not isinstance(right, Mapping):
            return False
        if left.keys() != right.keys():
            return False
        return all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)):
        if not isinstance(right, (list, tuple)):
            return False
        if len(left) != len(right):
            return False
        for left_item, right_item in zip(left, right, strict=True):
            if not json_equal(left_item, right_item):
                return False
        return True
    return type(left) is type(right) and left == right


def json_matches(value: object, pattern: object) -> bool:
    if isinstance(pattern, Mapping):
        return isinstance(value, Mapping) and all(
            key in value and json_matches(value[key], expected) for key, expected in pattern.items()
        )
    if isinstance(pattern, (list, tuple)):
        return isinstance(value, (list, tuple)) and all(
            any(json_matches(item, expected) for item in value) for expected in pattern
        )
    return json_equal(value, pattern)


@dataclass(frozen=True)
class ServiceInstance:
    id: str
    kind: str
    state: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class Notice:
    recipients: tuple[str, ...]
    kind: NotificationKind
    ref: str
    summary: str


@dataclass(frozen=True)
class Context:
    now: int
    actor: Actor
    actors: Mapping[str, Actor]
    service_id: str
    chat_id: str | None = None
    task_id: str | None = None
    notices: list[Notice] = field(default_factory=list)

    def notify(
        self, recipients: Iterable[str], kind: NotificationKind, ref: str, summary: str
    ) -> None:
        self.notices.append(Notice(tuple(dict.fromkeys(recipients)), kind, ref, summary))


@dataclass(frozen=True)
class Transition[S]:
    new_state: S
    result: JsonValue = field(default_factory=dict)
    emissions: tuple[Event, ...] = ()
    status: ResultStatus = ResultStatus.SUCCEEDED
    error: str | None = None
    notices: tuple[Notice, ...] = ()
    wait_until: int | None = None


class Result(Fact):
    id: str
    status: ResultStatus
    value: FrozenJson
    error: str | None
    time: int
    invocation_id: str
    event_id: str | None = None


class PendingWait(Fact):
    invocation: Invocation
    since: int
    until: int


class TraceEntry(Fact):
    sequence: int
    time: int
    kind: str
    payload: FrozenJson
    actor_id: str | None = None
    invocation_id: str | None = None
    event_id: str | None = None
    task_id: str | None = None
    chat_id: str | None = None
    cause_id: str | None = None
