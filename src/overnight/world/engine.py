from collections.abc import Iterable, Mapping
from types import MappingProxyType

from pydantic import JsonValue, ValidationError

from overnight.domain.contracts import Event, FaultSpec, Invocation, JsonObject, Notification
from overnight.domain.models import (
    Actor,
    Context,
    PendingWait,
    Result,
    ServiceInstance,
    TraceEntry,
    Transition,
)
from overnight.domain.values import json_object
from overnight.domain.vocabulary import (
    SYSTEM_SERVICE,
    WILDCARD,
    ActorRole,
    NotificationKind,
    ResultStatus,
)
from overnight.services.operations import ServiceBoundary, ServiceSessionBoundary
from overnight.services.system import System, SystemSession
from overnight.world.events import Dispatch, EventQueue
from overnight.world.faults import FaultPlan
from overnight.world.notifications import NotificationLog, Posted, Settled
from overnight.world.predicates import resolve_references


def _failure_message(error: ValueError) -> str:
    """Render validation errors without Pydantic's version-specific documentation URLs."""
    if not isinstance(error, ValidationError):
        return str(error)
    problems = [
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        if item["loc"]
        else item["msg"]
        for item in error.errors(include_url=False, include_input=False)
    ]
    return f"{error.title}: {'; '.join(problems)}"


class InvocationRejected(ValueError): ...


class World:
    def __init__(
        self,
        *,
        service_definitions: Mapping[str, ServiceBoundary],
        start_time: int,
        horizon: int,
        actors: Iterable[Actor],
        services: Iterable[ServiceInstance],
        events: Iterable[Event] = (),
        faults: Iterable[FaultSpec] = (),
        seed: int = 0,
    ) -> None:
        if type(start_time) is not int or type(horizon) is not int or type(seed) is not int:
            raise ValueError("start_time, horizon and seed must be integers")
        if horizon < start_time:
            raise ValueError("horizon precedes start_time")
        self.now = start_time
        self.start_time = start_time
        self.horizon = horizon
        self.seed = seed
        self._definitions = dict(service_definitions)
        self.actors = self._register_actors(actors)
        self._service_kinds, self._sessions = self._register_services(services)
        self._system: SystemSession = System(horizon).bind({}, self.actors)
        self._service_kinds[SYSTEM_SERVICE] = SYSTEM_SERVICE
        self._sessions[SYSTEM_SERVICE] = self._system
        self._notifications = NotificationLog(self.actors, self.can_see)
        self._threads: dict[tuple[str, str, str], str] = {}
        self._results: dict[str, Result] = {}
        self._result_owners: dict[str, tuple[str, str | None]] = {}
        self._trace: list[TraceEntry] = []
        self._queue = EventQueue()
        self._invocation_ids: set[str] = set()
        self._validate_grants()
        self._fault_plan = FaultPlan(
            faults,
            {
                service_id: session.operations.keys()
                for service_id, session in self._sessions.items()
                if service_id != SYSTEM_SERVICE
            },
            seed,
        )
        self._schedule_events(tuple(events), allowed_dependencies=set())

    @staticmethod
    def _register_actors(actors: Iterable[Actor]) -> Mapping[str, Actor]:
        supplied = tuple(actors)
        indexed = {actor.id: actor for actor in supplied}
        if not indexed:
            raise ValueError("world requires actors and services")
        if len(indexed) != len(supplied):
            raise ValueError("duplicate actor or service ID")
        for actor in indexed.values():
            if not isinstance(actor.id, str) or not actor.id:
                raise ValueError("actor IDs must be nonempty strings")
            if actor.role not in ActorRole:
                raise ValueError(f"invalid actor role: {actor.role}")
        return MappingProxyType(indexed)

    def _register_services(
        self, services: Iterable[ServiceInstance]
    ) -> tuple[dict[str, str], dict[str, ServiceSessionBoundary]]:
        supplied = tuple(services)
        kinds = {service.id: service.kind for service in supplied}
        if not kinds:
            raise ValueError("world requires actors and services")
        if len(kinds) != len(supplied):
            raise ValueError("duplicate actor or service ID")
        sessions: dict[str, ServiceSessionBoundary] = {}
        for service in supplied:
            if not isinstance(service.id, str) or not service.id:
                raise ValueError("service IDs must be nonempty strings")
            if SYSTEM_SERVICE in (service.id, service.kind):
                raise ValueError(f"the {SYSTEM_SERVICE} service is reserved for the world")
            if service.kind not in self._definitions:
                raise ValueError(f"unknown service kind: {service.kind}")
            sessions[service.id] = self._definitions[service.kind].bind(service.state, self.actors)
        return kinds, sessions

    def _validate_grants(self) -> None:
        known = {
            name
            for service_id, session in self._sessions.items()
            if service_id != SYSTEM_SERVICE
            for name in session.operations
        }
        for actor in self.actors.values():
            if SYSTEM_SERVICE in actor.read_services or SYSTEM_SERVICE in actor.operations:
                raise ValueError(f"actor {actor.id} names the reserved {SYSTEM_SERVICE} service")
            for service_id, operations in actor.operations.items():
                if service_id == WILDCARD:
                    valid = known
                elif service_id in self._sessions:
                    valid = set(self._sessions[service_id].operations)
                else:
                    raise ValueError(f"actor {actor.id} is granted unknown service: {service_id}")
                for operation in operations:
                    if operation != WILDCARD and operation not in valid:
                        raise ValueError(
                            f"actor {actor.id} is granted unknown operation: {operation}"
                        )

    @property
    def results(self) -> Mapping[str, Result]:
        return MappingProxyType(self._results)

    @property
    def trace(self) -> tuple[TraceEntry, ...]:
        return tuple(self._trace)

    @property
    def pending_events(self) -> tuple[str, ...]:
        return self._queue.pending_ids

    def next_wake_time(self) -> int | None:
        """The earliest of the next event, the next scheduled reminder and the next wait timeout."""
        dispatch = self._queue.next(now=self.now, state_for=self._state_for, results=self._results)
        times = (
            dispatch.time if dispatch is not None else None,
            self._system.next_due(),
            self._notifications.next_until(),
        )
        return min((time for time in times if time is not None), default=None)

    def _state_for(self, service_id: str) -> JsonObject:
        return self._sessions[service_id].snapshot()

    def record(
        self,
        kind: str,
        payload: object,
        *,
        actor_id: str | None = None,
        invocation_id: str | None = None,
        event_id: str | None = None,
        task_id: str | None = None,
        chat_id: str | None = None,
        cause_id: str | None = None,
    ) -> TraceEntry:
        entry = TraceEntry.model_validate(
            {
                "sequence": len(self._trace),
                "time": self.now,
                "kind": kind,
                "payload": payload,
                "actor_id": actor_id,
                "invocation_id": invocation_id,
                "event_id": event_id,
                "task_id": task_id,
                "chat_id": chat_id,
                "cause_id": cause_id,
            }
        )
        self._trace.append(entry)
        return entry

    def _schedule_events(
        self, additions: tuple[Event, ...], *, allowed_dependencies: set[str]
    ) -> None:
        self._queue.add(
            additions,
            now=self.now,
            actors=set(self.actors),
            services=set(self._sessions) - {SYSTEM_SERVICE},
            reserved=self._invocation_ids,
            allowed_dependencies=allowed_dependencies,
        )

    def _can_invoke(self, actor: Actor, service_id: str, operation: str) -> bool:
        if service_id == SYSTEM_SERVICE:
            return actor.role == ActorRole.AGENT
        for granted_service in (service_id, WILDCARD):
            granted_operations = actor.operations.get(granted_service, ())
            if operation in granted_operations or WILDCARD in granted_operations:
                return True
        return False

    def can_see(self, actor: Actor, service_id: str) -> bool:
        """Whether a write to the service can concern the actor: it reads it or holds a grant."""
        if service_id == SYSTEM_SERVICE:
            return actor.role == ActorRole.AGENT
        if service_id in actor.read_services or WILDCARD in actor.read_services:
            return True
        return bool(actor.operations.get(service_id) or actor.operations.get(WILDCARD))

    def _record_result(
        self,
        invocation: Invocation,
        status: ResultStatus,
        value: JsonValue,
        error: str | None,
        event_id: str | None,
    ) -> Result:
        result = Result.model_validate(
            {
                "id": invocation.id,
                "status": ResultStatus(status),
                "value": value,
                "error": error,
                "time": self.now,
                "invocation_id": invocation.id,
                "event_id": event_id,
            }
        )
        self._results[invocation.id] = result
        self._result_owners[invocation.id] = (invocation.actor_id, invocation.chat_id)
        self.record(
            "result",
            {"status": result.status, "value": result.value, "error": error},
            actor_id=invocation.actor_id,
            invocation_id=invocation.id,
            event_id=event_id,
            task_id=invocation.task_id,
            chat_id=invocation.chat_id,
            cause_id=invocation.cause_id,
        )
        return result

    def _record_invocation(
        self, invocation: Invocation, args: object, event_id: str | None
    ) -> None:
        self.record(
            "invocation",
            {
                "service_id": invocation.service_id,
                "operation": invocation.operation,
                "args": args,
                "requested_args": invocation.args,
            },
            actor_id=invocation.actor_id,
            invocation_id=invocation.id,
            event_id=event_id,
            task_id=invocation.task_id,
            chat_id=invocation.chat_id,
            cause_id=invocation.cause_id,
        )

    def _prepare_invocation(
        self, invocation: Invocation, actor: Actor, event_id: str | None
    ) -> Invocation:
        args = invocation.args
        try:
            session = self._sessions.get(invocation.service_id)
            if session is None or not self._can_invoke(
                actor, invocation.service_id, invocation.operation
            ):
                raise PermissionError("operation not permitted")
            if invocation.operation not in session.operations:
                raise ValueError("unknown operation")
            resolved = resolve_references(
                args,
                (actor.id, invocation.chat_id),
                event_id is not None,
                self._results,
                self._result_owners,
            )
            if not isinstance(resolved, dict):
                raise ValueError("invocation arguments must resolve to an object")
            args = resolved
            return Invocation.model_validate({**invocation.model_dump(mode="json"), "args": args})
        finally:
            self._record_invocation(invocation, args, event_id)

    def _apply_operation(
        self, invocation: Invocation, actor: Actor, event_id: str | None
    ) -> Transition:
        self._sessions[invocation.service_id].operations[invocation.operation].validate_args(
            json_object(invocation.args)
        )
        fault = self._fault_plan.consume(invocation, self.now)
        if fault is not None:
            self.record(
                "fault",
                {"fault_id": fault.id},
                actor_id=actor.id,
                invocation_id=invocation.id,
                event_id=event_id,
            )
            return Transition(None, {}, status=ResultStatus.FAILED, error=fault.error)
        return self._sessions[invocation.service_id].prepare(
            invocation,
            Context(
                self.now,
                actor,
                self.actors,
                invocation.service_id,
                chat_id=invocation.chat_id,
                task_id=invocation.task_id,
            ),
        )

    def _commit_transition(
        self, invocation: Invocation, transition: Transition, event_id: str | None
    ) -> Result:
        if not isinstance(transition, Transition) or transition.status not in {
            ResultStatus.SUCCEEDED,
            ResultStatus.FAILED,
            ResultStatus.DENIED,
        }:
            raise TypeError("service returned invalid transition")
        if transition.status != ResultStatus.SUCCEEDED:
            return self._record_result(
                invocation, transition.status, transition.result, transition.error, event_id
            )
        service_id = invocation.service_id
        self._schedule_events(tuple(transition.emissions), allowed_dependencies={invocation.id})
        changed = self._sessions[service_id].commit(transition)
        if changed:
            self.record(
                "state_change",
                {"service_id": service_id, "state": self._sessions[service_id].snapshot()},
                actor_id=invocation.actor_id,
                invocation_id=invocation.id,
                event_id=event_id,
                task_id=invocation.task_id,
                chat_id=invocation.chat_id,
                cause_id=invocation.cause_id,
            )
        for notice in transition.notices:
            chats = {
                recipient: chat
                for recipient in notice.recipients
                if (chat := self._threads.get((recipient, service_id, notice.ref))) is not None
            }
            self._record_posted(
                self._notifications.post(
                    time=self.now,
                    cause=invocation.id,
                    invoker=invocation.actor_id,
                    recipients=notice.recipients,
                    kind=notice.kind,
                    service_id=service_id,
                    ref=notice.ref,
                    summary=notice.summary,
                    chats=chats,
                    invoker_chat=invocation.chat_id,
                )
            )
            if invocation.chat_id is not None:
                self._threads[(invocation.actor_id, service_id, notice.ref)] = invocation.chat_id
        result = self._record_result(
            invocation, ResultStatus.SUCCEEDED, transition.result, None, event_id
        )
        self._settle()
        return result

    def _begin_wait(self, invocation: Invocation, until: int) -> Result | PendingWait:
        inbox = self._notifications.resolve(invocation.actor_id, invocation.chat_id)
        wait = PendingWait(invocation=invocation, since=self.now, until=until)
        self._notifications.begin_wait(wait, inbox)
        self._settle()
        return self._results.get(invocation.id) or wait

    def invoke(
        self, invocation: Invocation, *, _event_id: str | None = None
    ) -> Result | PendingWait:
        """Run one invocation. ``system.wait_for_notification`` returns a ``PendingWait`` until a
        notification or its timeout settles it; its result is then recorded under the same id."""
        if not invocation.id or invocation.id in self._results:
            raise InvocationRejected("duplicate or empty invocation ID")
        if _event_id is None and invocation.id in self._invocation_ids:
            raise InvocationRejected("invocation ID reserved by event")
        actor = self.actors.get(invocation.actor_id)
        if actor is None:
            raise ValueError("unknown actor")
        self._invocation_ids.add(invocation.id)
        try:
            try:
                resolved = self._prepare_invocation(invocation, actor, _event_id)
                transition = self._apply_operation(resolved, actor, _event_id)
                if transition.wait_until is not None:
                    return self._begin_wait(invocation, transition.wait_until)
            except PermissionError as error:
                return self._record_result(
                    invocation, ResultStatus.DENIED, {}, str(error), _event_id
                )
            except ValueError as error:
                return self._record_result(
                    invocation, ResultStatus.FAILED, {}, _failure_message(error), _event_id
                )
            return self._commit_transition(invocation, transition, _event_id)
        except Exception as error:
            self.record(
                "invalid",
                {"error": f"{type(error).__name__}: {error}"},
                actor_id=actor.id,
                invocation_id=invocation.id,
                event_id=_event_id,
            )
            raise

    def advance(self, to: int) -> tuple[Result, ...]:
        """Move time to ``to``. At each time: due events in insertion order, then due reminders by
        (due, id), then pending waits settle."""
        if type(to) is not int or to < self.now:
            raise ValueError("time must advance monotonically by integer seconds")
        produced: list[Result] = []
        while True:
            dispatch = self._queue.next(
                now=self.now, state_for=self._state_for, results=self._results, until=to
            )
            times = [
                time
                for time in (
                    dispatch.time if dispatch is not None else None,
                    self._system.next_due(),
                    self._notifications.next_until(),
                )
                if time is not None and time <= to
            ]
            if not times:
                break
            time = max(self.now, min(times))
            if dispatch is not None and dispatch.time == time:
                produced.append(self._dispatch(dispatch))
                continue
            self.now = time
            self._fire_reminders()
            self._settle()
        self.now = to
        return tuple(produced)

    def _dispatch(self, dispatch: Dispatch) -> Result:
        event = dispatch.scheduled.event
        failed = dispatch.failed_dependency
        self.now = dispatch.time
        self._queue.finish(dispatch)
        self.record(
            "event",
            {
                "dependencies": list(event.dependencies),
                "status": ResultStatus.SKIPPED if failed else "dispatched",
            },
            actor_id=event.invocation.actor_id,
            invocation_id=event.invocation.id,
            event_id=event.id,
            task_id=event.invocation.task_id,
            chat_id=event.invocation.chat_id,
            cause_id=event.invocation.cause_id,
        )
        if failed:
            self._invocation_ids.add(event.invocation.id)
            return self._record_result(
                event.invocation, ResultStatus.SKIPPED, {}, "dependency failed", event.id
            )
        try:
            result = self.invoke(event.invocation, _event_id=event.id)
        except Exception as error:
            if event.invocation.id not in self._results:
                self._record_result(
                    event.invocation,
                    ResultStatus.FAILED,
                    {},
                    f"{type(error).__name__}: {error}",
                    event.id,
                )
            raise
        if not isinstance(result, Result):
            raise TypeError("a world event cannot wait for notifications")
        return result

    def _fire_reminders(self) -> None:
        for reminder in self._system.due(self.now):
            self._record_posted(
                self._notifications.post(
                    time=self.now,
                    cause=f"reminder:{reminder.id}",
                    invoker=None,
                    recipients=(reminder.owner_id,),
                    kind=NotificationKind.REMINDER,
                    service_id=SYSTEM_SERVICE,
                    ref=reminder.id,
                    summary=reminder.message,
                    chat_id=reminder.chat_id,
                )
            )
            if self._system.mark_fired(reminder.id):
                self.record(
                    "reminder_fired",
                    {"reminder_id": reminder.id, "due": reminder.due},
                    actor_id=reminder.owner_id,
                    task_id=reminder.task_id,
                    chat_id=reminder.chat_id,
                )

    def _record_posted(self, posted: list[Posted]) -> None:
        for entry in posted:
            self.record(
                "notification",
                {
                    **entry.notification.model_dump(mode="json"),
                    "recipient": entry.recipient,
                    "cause": entry.cause,
                },
                actor_id=entry.recipient,
                chat_id=entry.notification.chat_id,
            )

    def _settle(self) -> None:
        for settled in self._notifications.settle(self.now):
            self._record_wait(settled)

    def _record_wait(self, settled: Settled) -> None:
        invocation = settled.wait.invocation
        if invocation.id in self._results:
            return
        actor_id, chat_id = settled.inbox
        if settled.notifications:
            self.record(
                "notification_delivered",
                {
                    "chat_id": chat_id,
                    "ids": [notification.id for notification in settled.notifications],
                    "via": "wait",
                },
                actor_id=actor_id,
                invocation_id=invocation.id,
                task_id=invocation.task_id,
                chat_id=invocation.chat_id,
            )
        self._record_result(
            invocation,
            ResultStatus.SUCCEEDED,
            {
                "time": self.now,
                "until": settled.wait.until,
                "timed_out": settled.timed_out,
                "notifications": [
                    notification.model_dump(mode="json") for notification in settled.notifications
                ],
            },
            None,
            None,
        )

    def open_inbox(self, actor_id: str, chat_id: str | None) -> None:
        """Start the actor's inbox for a chat; earlier notifications are not offered to it."""
        self._notifications.open(actor_id, chat_id)

    def undelivered(self, actor_id: str, chat_id: str | None) -> list[Notification]:
        return self._notifications.undelivered(actor_id, chat_id)

    def deliver(self, actor_id: str, chat_id: str | None, ids: Iterable[str], via: str) -> None:
        fresh = self._notifications.deliver(actor_id, chat_id, ids)
        if fresh:
            self.record(
                "notification_delivered",
                {"chat_id": chat_id, "ids": fresh, "via": via},
                actor_id=actor_id,
                chat_id=chat_id,
            )

    def expire(self, actor_id: str, chat_id: str | None) -> None:
        """Time out the inbox's pending waits now, as when its task reaches a deadline."""
        for settled in self._notifications.expire(actor_id, chat_id):
            self._record_wait(settled)

    def post_operator_message(
        self, actor_id: str, chat_id: str | None, message_id: str, message: str
    ) -> None:
        self._record_posted(
            self._notifications.post(
                time=self.now,
                cause=f"operator:{message_id}",
                invoker=None,
                recipients=(actor_id,),
                kind=NotificationKind.OPERATOR,
                service_id=None,
                ref=message_id,
                summary=message,
                chat_id=chat_id,
            )
        )
        self._settle()

    def observe(self, actor_id: str, chat_id: str | None = None) -> JsonObject:
        """The actor's view: readable services and its own results; with ``chat_id``, only the
        results of calls made in that chat."""
        actor = self.actors.get(actor_id)
        if actor is None:
            raise ValueError("unknown actor")
        visible: JsonObject = {}
        for service_id, session in self._sessions.items():
            readable = service_id in actor.read_services or WILDCARD in actor.read_services
            if service_id == SYSTEM_SERVICE:
                readable = actor.role == ActorRole.AGENT
            if not readable:
                continue
            visible[service_id] = session.observe(
                actor,
                Context(self.now, actor, self.actors, service_id),
            )
        own_results = {
            id_: {
                "status": result.status,
                "value": result.value,
                "error": result.error,
                "time": result.time,
            }
            for id_, result in self._results.items()
            if (owner := self._result_owners.get(id_)) is not None
            and owner[0] == actor_id
            and (chat_id is None or owner[1] == chat_id)
        }
        observation = json_object(
            {
                "time": self.now,
                "actor_id": actor_id,
                "services": visible,
                "results": own_results,
            }
        )
        self.record("observation", observation, actor_id=actor_id)
        return observation

    def operations_for(self, actor_id: str) -> dict[str, list[JsonObject]]:
        actor = self.actors.get(actor_id)
        if actor is None:
            raise ValueError("unknown actor")
        available: dict[str, list[JsonObject]] = {}
        for service_id, session in self._sessions.items():
            specs = session.operations
            allowed: list[JsonObject] = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                    "read_only": spec.read_only,
                }
                for name, spec in specs.items()
                if self._can_invoke(actor, service_id, name)
            ]
            if allowed:
                available[service_id] = allowed
        return available

    def snapshot_for_evaluator(self) -> JsonObject:
        return json_object(
            {
                "time": self.now,
                "seed": self.seed,
                "actors": {
                    id_: {"role": actor.role, "persona": actor.persona}
                    for id_, actor in self.actors.items()
                },
                "services": {
                    id_: {"kind": kind, "state": self._sessions[id_].snapshot()}
                    for id_, kind in self._service_kinds.items()
                },
                "results": {
                    id_: {
                        "status": result.status,
                        "value": result.value,
                        "error": result.error,
                        "time": result.time,
                        "event_id": result.event_id,
                    }
                    for id_, result in self._results.items()
                },
                "pending_events": list(self.pending_events),
                "faults_remaining": dict(self._fault_plan.remaining),
            }
        )
