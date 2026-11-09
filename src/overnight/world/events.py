from collections.abc import Callable, Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from overnight.domain.contracts import Event, JsonObject
from overnight.domain.models import Result
from overnight.domain.vocabulary import ResultStatus
from overnight.world.predicates import satisfied, state_targets


@dataclass(frozen=True)
class ScheduledEvent:
    event: Event
    scheduled_at: int
    insertion_order: int


@dataclass(frozen=True)
class Dispatch:
    scheduled: ScheduledEvent
    time: int
    failed_dependency: bool


class EventQueue:
    def __init__(self) -> None:
        self.events: dict[str, ScheduledEvent] = {}
        self.terminal: set[str] = set()

    @property
    def pending_ids(self) -> tuple[str, ...]:
        return tuple(event_id for event_id in self.events if event_id not in self.terminal)

    def add(
        self,
        additions: tuple[Event, ...],
        *,
        now: int,
        actors: set[str],
        services: set[str],
        reserved: set[str],
        allowed_dependencies: set[str],
    ) -> None:
        invocation_ids = [event.invocation.id for event in additions]
        event_ids = [event.id for event in additions]
        if (
            len(set(invocation_ids)) != len(invocation_ids)
            or len(set(event_ids)) != len(event_ids)
            or set(invocation_ids) & reserved
            or set(event_ids) & self.events.keys()
        ):
            raise ValueError("duplicate event or invocation ID")
        if any(not value for value in (*invocation_ids, *event_ids)):
            raise ValueError("event and invocation IDs must be nonempty")
        known_invocations = (
            {scheduled.event.invocation.id for scheduled in self.events.values()}
            | set(invocation_ids)
            | allowed_dependencies
        )
        prepared: list[ScheduledEvent] = []
        for event in additions:
            if event.invocation.service_id not in services:
                raise ValueError("event service does not exist")
            if event.invocation.actor_id not in actors:
                raise ValueError("event actor does not exist")
            if len(set(event.dependencies)) != len(event.dependencies):
                raise ValueError("duplicate dependency")
            if event.delay and not event.dependencies:
                raise ValueError("event delay requires dependencies")
            if any(dependency not in known_invocations for dependency in event.dependencies):
                raise ValueError("missing event dependency")
            condition = event.condition
            if condition is not None and any(
                target not in services for target in state_targets(condition)
            ):
                raise ValueError("condition targets unknown service")
            prepared.append(
                ScheduledEvent(
                    event=event,
                    scheduled_at=now,
                    insertion_order=len(self.events) + len(prepared),
                )
            )
        graph = {
            scheduled.event.invocation.id: scheduled.event.dependencies
            for scheduled in (*self.events.values(), *prepared)
        }
        try:
            tuple(TopologicalSorter(graph).static_order())
        except CycleError as error:
            raise ValueError("cyclic event dependency") from error
        self.events.update({scheduled.event.id: scheduled for scheduled in prepared})
        reserved.update(invocation_ids)

    def next(
        self,
        *,
        now: int,
        state_for: Callable[[str], JsonObject],
        results: Mapping[str, Result],
        until: int | None = None,
    ) -> Dispatch | None:
        ready_events: list[Dispatch] = []
        for event_id in self.pending_ids:
            dispatch = _dispatch_when_ready(
                self.events[event_id],
                now=now,
                deadline=until,
                state_for=state_for,
                results=results,
            )
            if dispatch is not None:
                ready_events.append(dispatch)
        if not ready_events:
            return None
        return min(
            ready_events,
            key=lambda dispatch: (dispatch.time, dispatch.scheduled.insertion_order),
        )

    def finish(self, dispatch: Dispatch) -> None:
        self.terminal.add(dispatch.scheduled.event.id)


def _dispatch_when_ready(
    scheduled: ScheduledEvent,
    *,
    now: int,
    deadline: int | None,
    state_for: Callable[[str], JsonObject],
    results: Mapping[str, Result],
) -> Dispatch | None:
    event = scheduled.event
    ready_at = event.at if event.at is not None else scheduled.scheduled_at
    failed_dependency = False
    unresolved = False
    for dependency_id in event.dependencies:
        result = results.get(dependency_id)
        if result is None:
            unresolved = True
            continue
        ready_at = max(ready_at, result.time + event.delay)
        if result.status != ResultStatus.SUCCEEDED:
            failed_dependency = True
    if unresolved and not failed_dependency:
        return None

    if deadline is not None and ready_at > deadline:
        return None

    dispatch_at = max(now, ready_at)
    if failed_dependency:
        return Dispatch(scheduled, dispatch_at, True)

    if event.condition is not None and not satisfied(event.condition, state_for, results):
        return None

    return Dispatch(scheduled, dispatch_at, False)
