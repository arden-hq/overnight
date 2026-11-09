from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from overnight.domain.contracts import JsonObject, Nonnegative, Omittable, Text
from overnight.domain.models import Actor, Context, Transition
from overnight.services.common import find_row, next_id
from overnight.services.operations import (
    IdArgument,
    NoArguments,
    OperationSpec,
    Parameters,
    Service,
    ServiceSession,
)
from overnight.services.state import State, distinct

type Seconds = Annotated[int, Field(ge=1)]


class Reminder(State):
    id: Text
    owner_id: Text
    chat_id: Text | None = None
    task_id: Text | None = None
    due: Nonnegative
    message: Text
    created_at: Nonnegative
    status: Literal["scheduled", "fired", "cancelled"] = "scheduled"


class SystemState(State):
    reminders: list[Reminder] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        distinct([row.id for row in self.reminders], "reminder")
        return self


class ReminderSet(Parameters):
    """Exactly one of ``at`` (an absolute world time) or ``delay`` (seconds from now)."""

    message: Text
    at: Omittable[Nonnegative] = Field(default_factory=lambda: None)
    delay: Omittable[Seconds] = Field(default_factory=lambda: None)

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        if (self.at is None) == (self.delay is None):
            raise ValueError("set exactly one of at or delay")
        return self


class WaitArguments(Parameters):
    timeout: Seconds


def _owned(row: Reminder, actor_id: str) -> bool:
    return row.owner_id == actor_id


class System(Service[SystemState]):
    """The world's own clock, wait and reminders, available to every agent actor."""

    def __init__(self, horizon: int) -> None:
        self.horizon = horizon
        super().__init__(
            SystemState,
            [
                OperationSpec(NoArguments, self.current_time, read_only=True),
                OperationSpec(WaitArguments, self.wait_for_notification),
                OperationSpec(ReminderSet, self.set_reminder),
                OperationSpec(NoArguments, self.list_reminders, read_only=True),
                OperationSpec(IdArgument, self.cancel_reminder),
            ],
        )

    def bind(self, state: JsonObject, actors: Mapping[str, Actor]) -> SystemSession:
        return SystemSession(self, state, actors)

    def validate_actors(self, state: SystemState, actors: Mapping[str, Actor]) -> None:
        if any(row.owner_id not in actors for row in state.reminders):
            raise ValueError("Reminder owner must be a known actor")

    def visible(self, state: SystemState, actor: Actor, context: Context) -> JsonObject:
        return {
            "as_of": context.now,
            "reminders": [row.json_object() for row in state.reminders if _owned(row, actor.id)],
        }

    def current_time(self, state: SystemState, args: NoArguments, context: Context) -> JsonObject:
        """Read the world time and the run's horizon, in integer seconds."""
        return {"time": context.now, "horizon": self.horizon}

    def wait_for_notification(
        self, state: SystemState, args: WaitArguments, context: Context
    ) -> Transition[SystemState]:
        """Wait until a notification for you arrives or timeout seconds pass, whichever is first.
        The result lists the notifications it delivered; timed_out is true when none arrived."""
        return Transition(state, {}, wait_until=min(context.now + args.timeout, self.horizon))

    def set_reminder(self, state: SystemState, args: ReminderSet, context: Context) -> JsonObject:
        """Schedule a reminder notification for yourself at an absolute time or after a delay."""
        due = args.at if args.at is not None else context.now + (args.delay or 0)
        if not context.now < due <= self.horizon:
            raise ValueError("reminder must be due after now and no later than the horizon")
        owner = context.actor.id
        for row in state.reminders:
            if (row.status, row.owner_id, row.chat_id, row.due, row.message) == (
                "scheduled",
                owner,
                context.chat_id,
                due,
                args.message,
            ):
                return {"id": row.id, "due": row.due, "created": False}
        row = Reminder(
            id=next_id("reminder", state.reminders),
            owner_id=owner,
            chat_id=context.chat_id,
            task_id=context.task_id,
            due=due,
            message=args.message,
            created_at=context.now,
        )
        state.reminders = [*state.reminders, row]
        return {"id": row.id, "due": row.due, "created": True}

    def list_reminders(self, state: SystemState, args: NoArguments, context: Context) -> JsonObject:
        """List your reminders in every status: scheduled, fired or cancelled."""
        return {
            "reminders": [
                row.json_object() for row in state.reminders if _owned(row, context.actor.id)
            ]
        }

    def cancel_reminder(self, state: SystemState, args: IdArgument, context: Context) -> JsonObject:
        """Cancel one of your scheduled reminders; a fired or cancelled one stays as it is."""
        row = find_row(
            state.reminders,
            lambda row: row.id == args.id and _owned(row, context.actor.id),
            "reminder",
        )
        if row.status != "scheduled":
            return {"id": row.id, "status": row.status, "changed": False}
        row.status = "cancelled"
        return {"id": row.id, "status": row.status, "changed": True}


class SystemSession(ServiceSession[SystemState]):
    def due(self, now: int) -> list[Reminder]:
        return sorted(
            (row for row in self._state.reminders if row.status == "scheduled" and row.due <= now),
            key=lambda row: (row.due, row.id),
        )

    def next_due(self) -> int | None:
        return min(
            (row.due for row in self._state.reminders if row.status == "scheduled"), default=None
        )

    def mark_fired(self, reminder_id: str) -> bool:
        for row in self._state.reminders:
            if row.id == reminder_id and row.status == "scheduled":
                row.status = "fired"
                return True
        return False
