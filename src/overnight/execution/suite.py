from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from overnight.domain.contracts import Agent, Contract, Nonnegative, OperatorModel
from overnight.domain.values import JsonObject
from overnight.execution.scenario import load_json
from overnight.execution.variations import VariationConfig, resolve_variant

type SuiteId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]


class SuiteCase(Contract):
    """One world: a scenario file with its optional overrides and variation. ``operator_answers``
    and ``probes`` are for `suite qa` only: the recorded answers that prove a persona case's
    oracle, and the wrong agents the case must reject."""

    id: SuiteId
    scenario: str
    overrides: str | None = None
    variation: str | None = None
    operator_answers: str | None = None
    probes: str | None = None


class SuiteArm(Contract):
    """One setup under test. Without ``agent``, each case keeps its authored agent."""

    id: SuiteId
    agent: Agent | None = None


class Suite(Contract):
    id: SuiteId
    attempts: Annotated[int, Field(ge=1, le=100)]
    arms: Annotated[list[SuiteArm], Field(min_length=1)]
    cases: Annotated[list[SuiteCase], Field(min_length=1)]
    retries: Annotated[int, Field(ge=0, le=5)] = 2
    operator_model: OperatorModel | None = None
    resamples: Annotated[int, Field(ge=100, le=100_000)] = 5000
    bootstrap_seed: Nonnegative = 0

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        if len({arm.id for arm in self.arms}) != len(self.arms):
            raise ValueError("duplicate suite arm ID")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate suite case ID")
        return self


@dataclass(frozen=True)
class PlannedCell:
    arm_id: str
    case_id: str
    agent_kind: str
    scenario: JsonObject
    overrides: JsonObject | None
    variation: JsonObject | None


@dataclass(frozen=True)
class SuitePlan:
    suite: Suite
    cells: tuple[PlannedCell, ...]


def _operator_scenario(scenario: JsonObject, model: OperatorModel | None) -> JsonObject:
    """Run every persona operator on the suite's operator model, so arms and operator share one
    provider."""
    operator = scenario.get("operator")
    persona = operator.get("persona") if isinstance(operator, dict) else None
    if model is None or not isinstance(operator, dict) or not isinstance(persona, dict):
        return scenario
    replaced = {**persona, "model": model.model_dump(mode="json")}
    return {**scenario, "operator": {**operator, "persona": replaced}}


def _arm_scenario(scenario: JsonObject, arm: SuiteArm) -> JsonObject:
    if arm.agent is None:
        return scenario
    agent = arm.agent.model_dump(mode="json", exclude_unset=True)
    authored = scenario.get("agent")
    if "context" not in agent and isinstance(authored, dict) and "context" in authored:
        agent["context"] = authored["context"]
    return {**scenario, "agent": agent}


def load_suite(path: Path) -> Suite:
    return Suite.model_validate(load_json(path))


def plan_suite(path: Path) -> SuitePlan:
    suite = load_suite(path)
    root = path.parent
    cells: list[PlannedCell] = []
    for arm in suite.arms:
        for case in suite.cases:
            scenario = _operator_scenario(
                _arm_scenario(load_json(root / case.scenario), arm), suite.operator_model
            )
            overrides = load_json(root / case.overrides) if case.overrides is not None else None
            variation = load_json(root / case.variation) if case.variation is not None else None
            config = VariationConfig.model_validate(variation) if variation is not None else None
            try:
                resolved = resolve_variant(scenario, overrides, config)
            except ValueError as error:
                raise ValueError(f"arm {arm.id} on case {case.id}: {error}") from error
            cells.append(
                PlannedCell(
                    arm.id, case.id, resolved.config.agent.kind, scenario, overrides, variation
                )
            )
    return SuitePlan(suite, tuple(cells))
