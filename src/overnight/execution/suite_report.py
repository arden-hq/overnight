import random
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from math import comb
from typing import Literal

from pydantic import BaseModel, ConfigDict

from overnight.execution.suite import Suite

type AttemptOutcome = Literal[
    "passed", "failed", "agent_failed", "unknown", "invalid", "error", "missing"
]

SCORED: frozenset[str] = frozenset({"passed", "failed", "unknown"})
MIN_INTERVAL_CASES = 5
_ROUNDING = 12


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutcomeCounts(ReportModel):
    passed: int
    failed: int
    agent_failed: int
    unknown: int
    invalid: int
    error: int
    missing: int
    retries: int


class ArmSpend(ReportModel):
    """Everything an arm consumed, retries included. None means not measured."""

    seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    seconds_per_attempt: float | None = None
    cost_per_pass: float | None = None
    operator_calls: int | None = None
    operator_cost_usd: float | None = None


class CaseResult(ReportModel):
    case_id: str
    passes: int
    scored: int
    outcomes: list[AttemptOutcome]


class LeaderComparison(ReportModel):
    leader: str
    difference: float
    interval: tuple[float, float] | None
    tied: bool | None
    swap_flip: float | None


class ArmReport(ReportModel):
    arm_id: str
    ranked: bool
    mean: float | None
    interval: tuple[float, float] | None
    pass_all: float | None
    pass_any: float | None
    rerun_flip: float | None
    counts: OutcomeCounts
    versus_leader: LeaderComparison | None
    spend: ArmSpend
    cases: list[CaseResult]


class SuiteReport(ReportModel):
    suite_id: str
    attempts: int
    cases: int
    resamples: int
    bootstrap_seed: int
    leader: str | None
    complete: bool
    arms: list[ArmReport]


def _pass_values(outcomes: Sequence[AttemptOutcome]) -> list[int]:
    return [1 if outcome == "passed" else 0 for outcome in outcomes if outcome in SCORED]


def rerun_flip(values: Sequence[int]) -> float | None:
    total, passes = len(values), sum(values)
    if total < 2:
        return None
    return 2 * passes * (total - passes) / (total * (total - 1))


def swap_flip(left: Sequence[int], right: Sequence[int]) -> float:
    left_rate, right_rate = sum(left) / len(left), sum(right) / len(right)
    return left_rate * (1 - right_rate) + right_rate * (1 - left_rate)


def pass_all(values: Sequence[int], attempts: int) -> float:
    return comb(sum(values), attempts) / comb(len(values), attempts)


def pass_any(values: Sequence[int], attempts: int) -> float:
    return 1 - comb(len(values) - sum(values), attempts) / comb(len(values), attempts)


def _interval(samples: Sequence[float]) -> tuple[float, float]:
    rounded = [round(sample, _ROUNDING) for sample in samples]
    quantiles = statistics.quantiles(rounded, n=40, method="inclusive")
    return quantiles[0], quantiles[-1]


def _bootstrap(
    arms: Mapping[str, Sequence[Sequence[int]]], resamples: int, seed: int
) -> dict[str, list[float]]:
    rng = random.Random(f"overnight-suite-v1:{seed}")
    cases = len(next(iter(arms.values())))
    samples: dict[str, list[float]] = {arm: [] for arm in arms}
    for _ in range(resamples):
        draw = rng.choices(range(cases), k=cases)
        for arm, cells in arms.items():
            total = 0.0
            for case in draw:
                attempts = cells[case]
                total += sum(rng.choices(attempts, k=len(attempts))) / len(attempts)
            samples[arm].append(total / cases)
    return samples


def _with_rates(spend: ArmSpend, passes: int, ran: int) -> ArmSpend:
    return spend.model_copy(
        update={
            "seconds_per_attempt": None
            if spend.seconds is None or not ran
            else spend.seconds / ran,
            "cost_per_pass": spend.cost_usd / passes
            if spend.cost_usd is not None and passes
            else None,
        }
    )


def _cell_mean(
    cells: Sequence[Sequence[int]], metric: Callable[[Sequence[int], int], float], attempts: int
) -> float:
    return sum(metric(cell, attempts) for cell in cells) / len(cells)


def _flip_mean(cells: Sequence[Sequence[int]]) -> float | None:
    flips = [flip for flip in map(rerun_flip, cells) if flip is not None]
    return sum(flips) / len(flips) if flips else None


def _counts(outcomes: Iterable[AttemptOutcome], retries: int) -> OutcomeCounts:
    items = list(outcomes)
    return OutcomeCounts(
        passed=items.count("passed"),
        failed=items.count("failed"),
        agent_failed=items.count("agent_failed"),
        unknown=items.count("unknown"),
        invalid=items.count("invalid"),
        error=items.count("error"),
        missing=items.count("missing"),
        retries=retries,
    )


def summarize_suite(
    suite: Suite,
    outcomes: Mapping[tuple[str, str], Sequence[AttemptOutcome]],
    retries: Mapping[str, int] | None = None,
    spend: Mapping[str, ArmSpend] | None = None,
) -> SuiteReport:
    case_ids = [case.id for case in suite.cases]
    values = {
        arm.id: [_pass_values(outcomes[arm.id, case]) for case in case_ids] for arm in suite.arms
    }
    ranked = {
        arm: cells
        for arm, cells in values.items()
        if all(len(cell) == suite.attempts for cell in cells)
    }
    rates = {arm: [sum(cell) / len(cell) for cell in cells] for arm, cells in ranked.items()}
    means = {arm: sum(cases) / len(cases) for arm, cases in rates.items()}
    leader = max(means, key=lambda arm: means[arm], default=None)
    enough_cases = len(case_ids) >= MIN_INTERVAL_CASES
    samples = (
        _bootstrap(ranked, suite.resamples, suite.bootstrap_seed) if ranked and enough_cases else {}
    )

    reports: list[ArmReport] = []
    for arm in suite.arms:
        cells = values[arm.id]
        all_outcomes = [outcomes[arm.id, case] for case in case_ids]
        comparison = None
        if leader is not None and arm.id in ranked and arm.id != leader:
            interval = (
                _interval(
                    [
                        left - right
                        for left, right in zip(samples[arm.id], samples[leader], strict=True)
                    ]
                )
                if enough_cases
                else None
            )
            comparison = LeaderComparison(
                leader=leader,
                difference=means[arm.id] - means[leader],
                interval=interval,
                tied=None if interval is None else interval[0] <= 0 <= interval[1],
                swap_flip=sum(
                    swap_flip(mine, theirs)
                    for mine, theirs in zip(cells, values[leader], strict=True)
                )
                / len(cells),
            )
        statistics_known = arm.id in ranked
        reports.append(
            ArmReport(
                arm_id=arm.id,
                ranked=statistics_known,
                mean=means.get(arm.id),
                interval=_interval(samples[arm.id]) if arm.id in samples else None,
                pass_all=_cell_mean(cells, pass_all, suite.attempts) if statistics_known else None,
                pass_any=_cell_mean(cells, pass_any, suite.attempts) if statistics_known else None,
                rerun_flip=_flip_mean(cells) if statistics_known else None,
                counts=_counts(
                    (outcome for items in all_outcomes for outcome in items),
                    (retries or {}).get(arm.id, 0),
                ),
                versus_leader=comparison,
                spend=_with_rates(
                    (spend or {}).get(arm.id, ArmSpend()),
                    sum(sum(cell) for cell in cells),
                    sum(outcome != "missing" for items in all_outcomes for outcome in items),
                ),
                cases=[
                    CaseResult(
                        case_id=case,
                        passes=sum(cell),
                        scored=len(cell),
                        outcomes=list(items),
                    )
                    for case, cell, items in zip(case_ids, cells, all_outcomes, strict=True)
                ],
            )
        )
    return SuiteReport(
        suite_id=suite.id,
        attempts=suite.attempts,
        cases=len(case_ids),
        resamples=suite.resamples,
        bootstrap_seed=suite.bootstrap_seed,
        leader=leader,
        complete=all(outcome in SCORED for items in outcomes.values() for outcome in items),
        arms=reports,
    )


def render_report(report: SuiteReport) -> str:
    headers = (
        "arm",
        "mean",
        "95% interval",
        f"pass^{report.attempts}",
        "flip",
        "vs leader",
        "scored",
        "retries",
        "s/attempt",
        "$/pass",
    )
    rows = [headers]
    for arm in report.arms:
        rows.append(
            (
                arm.arm_id,
                _number(arm.mean),
                f"{_number(arm.interval[0])} to {_number(arm.interval[1])}"
                if arm.interval is not None
                else ("too few cases" if arm.ranked else "not ranked"),
                _number(arm.pass_all),
                _number(arm.rerun_flip),
                _versus(arm, report.leader),
                f"{_scored(arm.counts)} of {report.attempts * report.cases}",
                str(arm.counts.retries),
                _rate(arm.spend.seconds_per_attempt, 1),
                _rate(arm.spend.cost_per_pass, 4),
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    return "\n".join(
        "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _scored(counts: OutcomeCounts) -> int:
    return counts.passed + counts.failed + counts.unknown


def _rate(value: float | None, digits: int) -> str:
    return "unknown" if value is None else f"{value:.{digits}f}"


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _versus(arm: ArmReport, leader: str | None) -> str:
    if arm.arm_id == leader:
        return "leader"
    if arm.versus_leader is None:
        return "n/a"
    tied = arm.versus_leader.tied
    label = "no interval" if tied is None else "tied" if tied else "behind"
    return f"{label} {arm.versus_leader.difference:+.3f}"
