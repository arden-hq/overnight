from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

from overnight.domain.models import Actor
from overnight.services.state import distinct


class Identified(Protocol):
    id: str


def find_row[T](rows: Iterable[T], predicate: Callable[[T], bool], label: str) -> T:
    for row in rows:
        if predicate(row):
            return row
    raise ValueError(f"Unknown {label}")


def next_id(prefix: str, rows: Iterable[Identified]) -> str:
    taken = {row.id for row in rows}
    number = 1
    while f"{prefix}-{number}" in taken:
        number += 1
    return f"{prefix}-{number}"


def validate_participants(ids: list[str], actors: Mapping[str, Actor], label: str) -> None:
    distinct(ids, label)
    if any(actor_id not in actors for actor_id in ids):
        raise ValueError(f"{label} must contain distinct known actors")
