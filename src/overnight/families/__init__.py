"""Scenario families: code that samples a hidden true record and emits a whole case from it.

A case is a scenario with its checks and a scripted oracle, the operator answers that let
`overnight qa` prove the oracle without a model, and wrong agents that the checks must reject.
Code writes every check; none comes from a model.
"""

from collections.abc import Callable
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, JsonValue

from overnight.domain.values import JsonObject
from overnight.domain.vocabulary import Verdict
from overnight.execution.operator import OperatorTransport
from overnight.execution.qa import AuthoredProbe
from overnight.execution.runtime import RunResult, run_scenario
from overnight.execution.scenario import resolve_scenario
from overnight.families import order_resolution


class FamilyError(Exception):
    pass


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    family: str
    seed: int
    facts: JsonObject
    scenario: JsonObject
    operator_answers: list[JsonObject]
    probes: list[AuthoredProbe]


class WrongAgent(NamedTuple):
    """A scripted agent that must not pass, the check group that must reject it and the
    operator that answers it."""

    name: str
    actions: list[JsonValue]
    rejected_by: str
    operator: OperatorTransport


class Built(NamedTuple):
    facts: JsonObject
    scenario: JsonObject
    operator: OperatorTransport
    wrong_agents: list[WrongAgent]


type Build = Callable[[str, int], Built]


def _order_resolution(case_id: str, seed: int) -> Built:
    facts = order_resolution.sample(seed)
    return Built(
        facts.model_dump(mode="json"),
        order_resolution.scenario(case_id, seed, facts),
        order_resolution.OracleAnswers(facts),
        [
            WrongAgent(
                probe.name,
                probe.actions,
                probe.rejected_by,
                order_resolution.OracleAnswers(facts, careless=probe.careless_operator),
            )
            for probe in order_resolution.probes(facts)
        ],
    )


FAMILIES: dict[str, Build] = {order_resolution.FAMILY: _order_resolution}
SEEDS = range(10_000)
"""Seeds a case id can name; each id spells its seed in four digits."""


def _answers(result: RunResult) -> list[JsonObject]:
    recorded = [entry["payload"] for entry in result.trace if entry["kind"] == "operator_answer"]
    return [payload for payload in recorded if isinstance(payload, dict)]


def _rejected(verdict: JsonObject, group: str) -> bool:
    values = verdict["forbidden_trace_sequences" if group == "forbidden" else group]
    assert isinstance(values, list)
    return any(values) if group == "forbidden" else False in values


def generate(family: str, seed: int) -> Case:
    """Build one case and record the operator answers of its runs. The oracle must pass, and
    each wrong agent must be rejected by the check group it targets."""
    if seed not in SEEDS:
        raise FamilyError(f"seed {seed} is outside {SEEDS.start} to {SEEDS.stop - 1}")
    case_id = f"{family}-{seed:04d}"
    built = FAMILIES[family](case_id, seed)
    result = run_scenario(resolve_scenario(built.scenario), operator_transport=built.operator)
    if result.verdict["verdict"] != Verdict.PASSED:
        raise FamilyError(f"{case_id}: the oracle did not pass: {result.verdict}")
    probes: list[AuthoredProbe] = []
    for wrong in built.wrong_agents:
        agent: JsonObject = {"kind": "scripted", "actions": wrong.actions}
        run = run_scenario(
            resolve_scenario({**built.scenario, "agent": agent}), operator_transport=wrong.operator
        )
        if run.verdict["verdict"] == Verdict.PASSED or not _rejected(
            run.verdict, wrong.rejected_by
        ):
            raise FamilyError(
                f"{case_id}: the {wrong.rejected_by} checks did not reject the agent that "
                f"{wrong.name}: {run.verdict}"
            )
        probes.append(AuthoredProbe(name=wrong.name, agent=agent, operator_answers=_answers(run)))
    return Case(
        id=case_id,
        family=family,
        seed=seed,
        facts=built.facts,
        scenario=built.scenario,
        operator_answers=_answers(result),
        probes=probes,
    )
