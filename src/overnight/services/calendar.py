from collections.abc import Mapping
from datetime import datetime
from typing import Self

from pydantic import Field, model_validator

from overnight.domain.contracts import JsonObject, NameSet, Omittable, OmittableNameSet, Text
from overnight.domain.models import Actor, Context
from overnight.domain.values import instant
from overnight.domain.vocabulary import NotificationKind
from overnight.services.common import find_row, next_id, validate_participants
from overnight.services.operations import IdArgument, OperationSpec, Parameters, Service
from overnight.services.state import State, distinct


def event_time(value: str) -> datetime:
    parsed = instant(value)
    if parsed is None:
        raise ValueError("event time must be an ISO 8601 time with a UTC offset")
    return parsed


class CalendarEvent(State):
    id: Text
    owner_id: Text
    title: Text
    start: Text
    end: Text
    location: Text | None = None
    attendees: NameSet = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if event_time(self.end) <= event_time(self.start):
            raise ValueError("Event end must follow start")
        return self


class CalendarState(State):
    events: list[CalendarEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        distinct([row.id for row in self.events], "event")
        return self


class CalendarList(Parameters):
    from_time: Omittable[Text] = Field(default_factory=lambda: None, alias="from")
    to: Omittable[Text] = Field(default_factory=lambda: None)


class CalendarCreate(Parameters):
    title: Text
    start: Text
    end: Text
    location: Omittable[Text] = Field(default_factory=lambda: None)
    attendees: NameSet = Field(default_factory=list)


class CalendarUpdate(IdArgument):
    title: Omittable[Text] = Field(default_factory=lambda: None)
    start: Omittable[Text] = Field(default_factory=lambda: None)
    end: Omittable[Text] = Field(default_factory=lambda: None)
    location: Text | None = Field(default_factory=lambda: None)
    attendees: OmittableNameSet = Field(default_factory=lambda: None)


def _event_visible(row: CalendarEvent, actor_id: str) -> bool:
    return row.owner_id == actor_id or actor_id in row.attendees


class Calendar(Service[CalendarState]):
    def __init__(self) -> None:
        super().__init__(
            CalendarState,
            [
                OperationSpec(CalendarList, self.list_events, read_only=True),
                OperationSpec(CalendarCreate, self.create_event),
                OperationSpec(CalendarUpdate, self.update_event),
                OperationSpec(IdArgument, self.delete_event),
            ],
        )

    def validate_actors(self, state: CalendarState, actors: Mapping[str, Actor]) -> None:
        for row in state.events:
            if row.owner_id not in actors:
                raise ValueError("Event owner must be a known actor")
            validate_participants(row.attendees, actors, "attendees")

    def visible(self, state: CalendarState, actor: Actor, context: Context) -> JsonObject:
        return {
            "as_of": context.now,
            "events": [row.view_object() for row in state.events if _event_visible(row, actor.id)],
        }

    def _owned_event(self, state: CalendarState, event_id: str, actor_id: str) -> CalendarEvent:
        return find_row(
            state.events, lambda row: row.id == event_id and row.owner_id == actor_id, "owned event"
        )

    def list_events(self, state: CalendarState, args: CalendarList, context: Context) -> JsonObject:
        """List events owned by or inviting the actor."""
        start = event_time(args.from_time) if args.from_time is not None else None
        end = event_time(args.to) if args.to is not None else None
        events: list[CalendarEvent] = []
        for event in state.events:
            if not _event_visible(event, context.actor.id):
                continue
            starts_at = event_time(event.start)
            if start is not None and starts_at < start:
                continue
            if end is not None and starts_at > end:
                continue
            events.append(event)
        events.sort(key=lambda event: event_time(event.start))
        return {"as_of": context.now, "events": [event.view_object() for event in events]}

    def create_event(
        self, state: CalendarState, args: CalendarCreate, context: Context
    ) -> JsonObject:
        """Create an event owned by the actor."""
        validate_participants(args.attendees, context.actors, "attendees")
        row = CalendarEvent.model_validate(
            {
                "id": next_id("event", state.events),
                "owner_id": context.actor.id,
                "attendees": [],
                **args.model_dump(exclude_unset=True),
            }
        )
        state.events = [*state.events, row]
        _notify_attendees(row.attendees, row, "created", context)
        return {"id": row.id, "created": True}

    def update_event(
        self, state: CalendarState, args: CalendarUpdate, context: Context
    ) -> JsonObject:
        """Update an event owned by the actor."""
        row = self._owned_event(state, args.id, context.actor.id)
        if args.attendees is not None:
            validate_participants(args.attendees, context.actors, "attendees")
        updated = CalendarEvent.model_validate(
            {**row.view_object(), **args.model_dump(exclude_unset=True, exclude={"id"})}
        )
        state.events = [updated if item.id == row.id else item for item in state.events]
        _notify_attendees([*row.attendees, *updated.attendees], updated, "updated", context)
        return {"id": row.id, "updated": True}

    def delete_event(self, state: CalendarState, args: IdArgument, context: Context) -> JsonObject:
        """Delete an event owned by the actor."""
        row = self._owned_event(state, args.id, context.actor.id)
        state.events = [item for item in state.events if item.id != row.id]
        _notify_attendees(row.attendees, row, "deleted", context)
        return {"id": row.id, "deleted": True}


def _notify_attendees(
    attendees: list[str], row: CalendarEvent, change: str, context: Context
) -> None:
    context.notify(
        attendees,
        NotificationKind.CALENDAR,
        row.id,
        f"Calendar event {row.title} {change} by {context.actor.id}",
    )
