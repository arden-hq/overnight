import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import TypeAdapter

from overnight.domain.actors import execution_actors
from overnight.domain.contracts import (
    Actor,
    Check,
    Checks,
    CompoundCondition,
    Condition,
    EqualsCheck,
    InvokeAction,
    JsonObject,
    NegatedCondition,
    OperatorMessage,
    OperatorPolicy,
    OrderCheck,
    Overrides,
    ResultEquals,
    ResultExists,
    Scenario,
    ScenarioError,
    ScheduledDecision,
    ScriptedAgent,
    WaitAction,
)
from overnight.domain.models import ServiceInstance
from overnight.domain.values import json_object
from overnight.domain.vocabulary import SYSTEM_SERVICE, WILDCARD, ActorRole
from overnight.services.catalog import BUILTIN_SERVICES
from overnight.services.operations import ServiceBoundary
from overnight.world.engine import World
from overnight.world.predicates import state_targets

_RESERVED = f"the {SYSTEM_SERVICE} service is reserved for the world and every agent"


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ScenarioError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_number(constant: str) -> None:
    raise ScenarioError(f"invalid JSON number: {constant}")


def load_json(path: str | Path) -> JsonObject:
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_invalid_number,
        )
        return TypeAdapter(JsonObject).validate_python(value, strict=True)
    except (OSError, ValueError) as error:
        raise ScenarioError(str(error)) from error


def _merge_known(base: JsonObject, patch: JsonObject, where: str) -> None:
    for key, value in patch.items():
        if key not in base:
            raise ScenarioError(f"{where}: unknown key {key}")
        prior = base[key]
        if isinstance(prior, dict) and isinstance(value, dict):
            _merge_known(prior, value, f"{where}.{key}")
        else:
            base[key] = value


def _apply_overrides(doc: Scenario, overrides: Overrides) -> None:
    services = {service.id: service for service in doc.services}
    actors = {actor.id: actor for actor in doc.actors}
    for service_id, patch in overrides.data.items():
        if service_id not in services:
            raise ScenarioError(f"unknown override service: {service_id}")
        _merge_known(services[service_id].state, patch, f"overrides.data.{service_id}")
    for actor_id, patch in overrides.people.items():
        if actor_id not in actors or actors[actor_id].role != ActorRole.PERSON:
            raise ScenarioError(f"unknown person override: {actor_id}")
        actor = actors[actor_id]
        persona = json_object(actor.persona)
        _merge_known(persona, patch, f"overrides.people.{actor_id}")
        replacement = Actor.model_validate({**actor.model_dump(mode="json"), "persona": persona})
        doc.actors = [replacement if item.id == actor_id else item for item in doc.actors]
    if overrides.faults is not None:
        doc.faults = overrides.faults


def _condition_results(condition: Condition) -> Iterator[str]:
    match condition:
        case CompoundCondition():
            for child in condition.conditions:
                yield from _condition_results(child)
        case NegatedCondition():
            yield from _condition_results(condition.condition)
        case ResultEquals() | ResultExists():
            yield condition.result_id


def _named_ids(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "invocation_id":
                yield str(item)
            yield from _named_ids(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _named_ids(item)


def _referenced_results(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        target = value.get("$ref")
        if isinstance(target, Mapping) and "result" in target:
            yield str(target["result"])
        for item in value.values():
            yield from _referenced_results(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _referenced_results(item)


def _check_ids(check: Check) -> Iterator[str]:
    if isinstance(check, OrderCheck):
        yield from _named_ids(check.first)
        yield from _named_ids(check.then)
        return
    expected = check.value if isinstance(check, EqualsCheck) else None
    if check.source == "state" and check.path[:1] == ["results"]:
        if len(check.path) > 1:
            yield str(check.path[1])
        elif isinstance(expected, dict):
            yield from expected
    if check.source == "trace":
        if check.path[-1:] == ["invocation_id"] and expected is not None:
            yield str(expected)
        yield from _named_ids(expected)


def _check_results(checks: Checks) -> Iterator[str]:
    for check in [*checks.outcomes, *checks.collateral, *(c.requires for c in checks.claims)]:
        yield from _check_ids(check)
    for forbidden in checks.forbidden:
        yield from _named_ids(forbidden.match)


class ScenarioReferences:
    def __init__(self, doc: Scenario, definitions: Mapping[str, ServiceBoundary]) -> None:
        self.definitions = definitions
        self.doc = doc
        self.actors = {actor.id: actor for actor in doc.actors}
        self.services = {service.id: service for service in doc.services}
        for service in doc.services:
            if SYSTEM_SERVICE in (service.id, service.kind):
                raise ScenarioError(_RESERVED)
            if service.kind not in definitions:
                raise ScenarioError(f"unknown service kind: {service.kind}")
        if len(self.actors) != len(doc.actors) or len(self.services) != len(doc.services):
            raise ScenarioError("duplicate actor or service ID")

    def require_operation(self, service_id: str, operation: str) -> None:
        service = self.services.get(service_id)
        if service is None:
            raise ScenarioError(f"unknown service or operation: {service_id}.{operation}")
        if operation == WILDCARD:
            return
        if operation not in self.definitions[service.kind].operations:
            raise ScenarioError(f"unknown service or operation: {service_id}.{operation}")

    def require_time_in_horizon(self, at: int) -> None:
        if not self.doc.start_time <= at <= self.doc.horizon:
            raise ScenarioError("event/operator time outside scenario horizon")

    def validate_reserved_service(self) -> None:
        named = [
            *(service_id for actor in self.doc.actors for service_id in actor.read_services),
            *(service_id for actor in self.doc.actors for service_id in actor.operations),
            *(event.invocation.service_id for event in self.doc.events),
            *(
                target
                for event in self.doc.events
                if event.condition is not None
                for target in state_targets(event.condition)
            ),
            *(fault.service_id for fault in self.doc.faults),
            *(rule.service_id for rule in self.doc.operator.policy.rules),
            *(
                step.service_id
                for step in self.doc.operator.steps
                if isinstance(step, OperatorPolicy)
            ),
            *(
                step.match.service_id
                for step in self.doc.operator.steps
                if isinstance(step, ScheduledDecision)
            ),
        ]
        if SYSTEM_SERVICE in named:
            raise ScenarioError(_RESERVED)

    def validate_actor_grants(self) -> None:
        execution_actors(self.doc.actors)
        for actor in self.doc.actors:
            for service_id in actor.read_services:
                if service_id != WILDCARD and service_id not in self.services:
                    raise ScenarioError(f"actor {actor.id} reads unknown service")
            for service_id, operations in actor.operations.items():
                targets = self.services.keys() if service_id == WILDCARD else [service_id]
                for target in targets:
                    for operation in operations:
                        self.require_operation(target, operation)

    def validate_faults(self) -> None:
        if len({fault.id for fault in self.doc.faults}) != len(self.doc.faults):
            raise ScenarioError("duplicate fault ID")
        for fault in self.doc.faults:
            self.require_operation(fault.service_id, fault.operation)

    def validate_world_events(self) -> None:
        task_chats = {task.id: task.chat_id for task in self.doc.tasks}
        for event in self.doc.events:
            invocation = event.invocation
            actor = self.actors.get(invocation.actor_id)
            if actor is None or actor.role not in {ActorRole.PERSON, ActorRole.SYSTEM}:
                raise ScenarioError("trusted world events require person or system actor")
            self.require_operation(invocation.service_id, invocation.operation)
            if event.at is not None:
                self.require_time_in_horizon(event.at)
            spec = self.definitions[self.services[invocation.service_id].kind].operations.get(
                invocation.operation
            )
            if spec is None:
                raise ScenarioError("event targets unknown operation")
            spec.validate_template(invocation.args)
            if self.doc.tasks:
                has_task = invocation.task_id is not None
                has_chat = invocation.chat_id is not None
                if has_task != has_chat:
                    raise ScenarioError("episode event needs both task_id and chat_id")
                if has_task and task_chats.get(invocation.task_id) != invocation.chat_id:
                    raise ScenarioError("event task and chat do not match")

    def validate_operator_plan(self) -> None:
        rules = self.doc.operator.policy.rules
        if len({(rule.service_id, rule.operation) for rule in rules}) != len(rules):
            raise ScenarioError("duplicate policy rule")
        for rule in rules:
            self.require_operation(rule.service_id, rule.operation)
        for step in self.doc.operator.steps:
            self.require_time_in_horizon(step.at)
            if isinstance(step, OperatorPolicy):
                self.require_operation(step.service_id, step.operation)
            if isinstance(step, ScheduledDecision):
                match = step.match
                self.require_operation(match.service_id, match.operation)
                if match.operation == WILDCARD:
                    continue
                kind = self.services[match.service_id].kind
                try:
                    self.definitions[kind].operations[match.operation].validate_args(match.args)
                except ValueError as error:
                    raise ScenarioError(
                        f"operator decision arguments do not fit "
                        f"{match.service_id}.{match.operation}: {error}"
                    ) from error
        self.validate_persona()

    def validate_persona(self) -> None:
        persona = self.doc.operator.persona
        if persona is None:
            return
        if any(isinstance(step, ScheduledDecision) for step in self.doc.operator.steps):
            raise ScenarioError("an operator persona decides approvals itself: remove timed steps")
        named = [brief.task_id for brief in persona.briefs]
        tasks = [task.id for task in self.doc.tasks]
        if not tasks:
            if named != [None]:
                raise ScenarioError("without tasks, the persona has one brief with no task_id")
            return
        if len(set(named)) != len(named) or set(named) != set(tasks):
            raise ScenarioError("the persona needs exactly one brief for each task")

    def validate_episode_tasks(self) -> None:
        if not self.doc.tasks:
            return
        task_ids = {task.id for task in self.doc.tasks}
        chat_ids = {task.chat_id for task in self.doc.tasks}
        if len(task_ids) != len(self.doc.tasks):
            raise ScenarioError("duplicate task ID")
        for task in self.doc.tasks:
            start = self.doc.start_time if task.start_at is None else task.start_at
            self.require_time_in_horizon(start)
            if task.deadline is not None:
                self.require_time_in_horizon(task.deadline)
        for step in self.doc.operator.steps:
            if not isinstance(step, OperatorMessage):
                continue
            if step.task_id is None or step.chat_id is None:
                raise ScenarioError("episode operator messages need task_id and chat_id")
            if step.task_id not in task_ids or step.chat_id not in chat_ids:
                raise ScenarioError("operator message references unknown episode task or chat")
            task = next(task for task in self.doc.tasks if task.id == step.task_id)
            if task.chat_id != step.chat_id:
                raise ScenarioError("operator message task and chat do not match")

    def validate_scripted_actions(self) -> None:
        if not isinstance(self.doc.agent, ScriptedAgent):
            return
        for action in self.doc.agent.actions:
            if isinstance(action, InvokeAction) and action.service_id not in {
                *self.services,
                SYSTEM_SERVICE,
            }:
                raise ScenarioError("agent action references unknown service")
            if isinstance(action, WaitAction) and action.until is not None:
                self.require_time_in_horizon(action.until)
                if action.until <= self.doc.start_time:
                    raise ScenarioError("wait.until must follow start_time")

    def validate_author_references(self) -> None:
        events = {event.invocation.id for event in self.doc.events}
        named = [
            *(
                ("event condition", invocation_id)
                for event in self.doc.events
                if event.condition is not None
                for invocation_id in _condition_results(event.condition)
            ),
            *(
                ("event argument", invocation_id)
                for event in self.doc.events
                for invocation_id in _referenced_results(event.invocation.args)
            ),
            *(("check", invocation_id) for invocation_id in _check_results(self.doc.checks)),
            *(
                ("check", invocation_id)
                for task in self.doc.tasks
                for invocation_id in _check_results(task.checks)
            ),
        ]
        for where, invocation_id in named:
            if invocation_id not in events:
                raise ScenarioError(
                    f"{where} names invocation {invocation_id!r}, which no event makes; "
                    "check world state or the trace instead"
                )

    def validate(self) -> None:
        self.validate_reserved_service()
        self.validate_actor_grants()
        self.validate_faults()
        self.validate_world_events()
        self.validate_operator_plan()
        self.validate_episode_tasks()
        self.validate_scripted_actions()
        self.validate_author_references()


@dataclass(frozen=True)
class ResolvedScenario:
    config: Scenario
    service_definitions: Mapping[str, ServiceBoundary]

    def to_dict(self) -> JsonObject:
        resolved = self.config.model_dump(mode="json", by_alias=True, exclude_unset=True)
        resolved["checks"] = self.config.checks.model_dump(mode="json")
        return resolved


def resolve_scenario(
    raw: JsonObject,
    overrides: JsonObject | None = None,
    *,
    service_definitions: Mapping[str, ServiceBoundary] = BUILTIN_SERVICES,
) -> ResolvedScenario:
    definitions = MappingProxyType(dict(service_definitions))
    try:
        doc = Scenario.model_validate(raw)
        _apply_overrides(doc, Overrides.model_validate(overrides or {}))
        ScenarioReferences(doc, definitions).validate()
        json.dumps(doc.model_dump(mode="json"), allow_nan=False)
        construct_world(doc, service_definitions=definitions)
    except (ValueError, TypeError) as error:
        raise ScenarioError(str(error)) from error
    return ResolvedScenario(doc, definitions)


def construct_world(
    doc: Scenario | JsonObject, *, service_definitions: Mapping[str, ServiceBoundary]
) -> World:
    config = doc if isinstance(doc, Scenario) else Scenario.model_validate(doc)
    services = [
        ServiceInstance(service.id, service.kind, service.state) for service in config.services
    ]
    return World(
        service_definitions=service_definitions,
        start_time=config.start_time,
        horizon=config.horizon,
        actors=config.actors,
        services=services,
        events=config.events,
        faults=config.faults,
        seed=config.seed,
    )
