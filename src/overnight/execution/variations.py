import hashlib
import random
from typing import Annotated, Self

from pydantic import Field, model_validator

from overnight.domain.contracts import (
    Contract,
    Event,
    FaultSpec,
    Invocation,
    Name,
    Nonnegative,
    Scenario,
)
from overnight.domain.values import JsonObject
from overnight.execution.scenario import ResolvedScenario, resolve_scenario

MAX_BACKGROUND_EVENTS = 128


class BackgroundSchedule(Contract):
    """A bounded sample of authored event payloads placed within a time window."""

    id: Name
    count: Annotated[int, Field(ge=1, le=64)]
    candidates: Annotated[list[Event], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_templates(self) -> Self:
        if any(candidate.at is not None for candidate in self.candidates):
            raise ValueError("background event candidates cannot set at")
        if any(candidate.dependencies for candidate in self.candidates):
            raise ValueError("background event candidates cannot set dependencies")
        if any(candidate.delay != 0 for candidate in self.candidates):
            raise ValueError("background event candidates cannot set delay")
        return self


class VariationConfig(Contract):
    """Authored noise, adversarial events, and outages for one scenario variant."""

    id: Name
    seed: Nonnegative
    start: Nonnegative
    end: Nonnegative
    background: list[BackgroundSchedule] = Field(default_factory=list)
    adversarial_events: list[Event] = Field(default_factory=list)
    outage_faults: list[FaultSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.end < self.start:
            raise ValueError("variation end precedes start")
        if len({schedule.id for schedule in self.background}) != len(self.background):
            raise ValueError("duplicate background schedule ID")
        if sum(schedule.count for schedule in self.background) > MAX_BACKGROUND_EVENTS:
            raise ValueError(f"background variation exceeds {MAX_BACKGROUND_EVENTS} events")
        return self


def apply_variation(scenario: Scenario, config: VariationConfig) -> Scenario:
    if config.start < scenario.start_time or config.end > scenario.horizon:
        raise ValueError("variation window outside scenario horizon")
    for event in config.adversarial_events:
        if event.at is not None and not scenario.start_time <= event.at <= scenario.horizon:
            raise ValueError("adversarial event time outside scenario horizon")
    for fault in config.outage_faults:
        if fault.start is not None and not scenario.start_time <= fault.start <= scenario.horizon:
            raise ValueError("outage start outside scenario horizon")
        if fault.end is not None and not scenario.start_time <= fault.end <= scenario.horizon:
            raise ValueError("outage end outside scenario horizon")

    generated = [
        event for schedule in config.background for event in _compile_background(config, schedule)
    ]
    additions = [*generated, *config.adversarial_events]
    _reject_collisions(scenario, additions, config.outage_faults)

    copied = Scenario.model_validate(
        scenario.model_dump(mode="json", by_alias=True, exclude_unset=True)
    )
    copied.events = [*copied.events, *additions]
    copied.faults = [*copied.faults, *config.outage_faults]
    return copied


def resolve_variant(
    raw: JsonObject, overrides: JsonObject | None, variation: VariationConfig | None
) -> ResolvedScenario:
    resolved = resolve_scenario(raw, overrides)
    if variation is None:
        return resolved
    varied = apply_variation(resolved.config, variation)
    return resolve_scenario(varied.model_dump(mode="json", by_alias=True, exclude_unset=True))


def _compile_background(config: VariationConfig, schedule: BackgroundSchedule) -> list[Event]:
    rng = random.Random(_schedule_seed(config.seed, schedule.id))
    events: list[Event] = []
    for index in range(schedule.count):
        candidate = rng.choice(schedule.candidates)
        invocation = Invocation.model_validate(
            {
                **candidate.invocation.model_dump(mode="json"),
                "id": _generated_id(config.id, schedule.id, index, "invocation"),
            }
        )
        events.append(
            Event.model_validate(
                {
                    **candidate.model_dump(mode="json"),
                    "id": _generated_id(config.id, schedule.id, index, "event"),
                    "invocation": invocation.model_dump(mode="json"),
                    "at": rng.randint(config.start, config.end),
                }
            )
        )
    return events


def _schedule_seed(seed: int, schedule_id: str) -> int:
    digest = hashlib.sha256(f"overnight-variation-v1\0{seed}\0{schedule_id}".encode()).digest()
    return int.from_bytes(digest[:8])


def _generated_id(variation_id: str, schedule_id: str, index: int, kind: str) -> str:
    return f"variation:{variation_id}:{schedule_id}:{index}:{kind}"


def _reject_collisions(
    scenario: Scenario, additions: list[Event], outage_faults: list[FaultSpec]
) -> None:
    event_ids = [event.id for event in (*scenario.events, *additions)]
    invocation_ids = [event.invocation.id for event in (*scenario.events, *additions)]
    fault_ids = [fault.id for fault in (*scenario.faults, *outage_faults)]
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("variation event ID collision")
    if len(set(invocation_ids)) != len(invocation_ids):
        raise ValueError("variation invocation ID collision")
    if len(set(fault_ids)) != len(fault_ids):
        raise ValueError("variation fault ID collision")
