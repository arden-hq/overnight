from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from overnight.domain.values import JsonObject
from overnight.domain.vocabulary import RunStatus, Verdict
from overnight.execution.adapters import ScriptedAdapter
from overnight.execution.operator import OperatorTransport, QaTransport, RecordedTransport
from overnight.execution.runtime import RunResult, run_scenario
from overnight.execution.scenario import load_json
from overnight.execution.suite import Suite, load_suite
from overnight.execution.variations import VariationConfig, resolve_variant

LOCAL_ORACLE_KINDS = frozenset({"command", "persistent_command"})
FALSE_CLAIM = "The task is complete."
SHAM_AGENTS: tuple[tuple[str, JsonObject, bool], ...] = (
    ("quits", {"kind": "scripted", "actions": [{"kind": "done"}]}, False),
    ("idles", {"kind": "scripted", "actions": []}, True),
    ("waits", {"kind": "scripted", "actions": []}, True),
    (
        "claims",
        {"kind": "scripted", "actions": [{"kind": "report", "message": FALSE_CLAIM}]},
        True,
    ),
)


class WaitsThenDone(ScriptedAdapter):
    def __init__(self, last_turn: int) -> None:
        super().__init__([])
        self.last_turn = last_turn

    def respond(self, observation: JsonObject) -> JsonObject:
        time = observation["time"]
        assert isinstance(time, int)
        return (
            {"kind": "done"}
            if time >= self.last_turn
            else {"kind": "wait", "until": self.last_turn}
        )


class QaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProbeResult(QaModel):
    probe: str
    verdict: str | None
    ok: bool
    rejections: int = 0
    unproven: bool = False
    note: str | None = None


class AuthoredProbe(QaModel):
    """A wrong agent written for one scenario, with the operator answers of its recorded run."""

    name: str
    agent: JsonObject
    operator_answers: list[JsonObject] = []


class ProbeFile(QaModel):
    probes: list[AuthoredProbe]


class ScenarioQa(QaModel):
    case_id: str
    status: Literal["ok", "defect", "no_oracle"]
    probes: list[ProbeResult]


def _has_persona(scenario: JsonObject) -> bool:
    operator = scenario.get("operator")
    return isinstance(operator, dict) and operator.get("persona") is not None


def _sham_operator(scenario: JsonObject) -> OperatorTransport | None:
    """A sham probe's persona gets no help: no replies and every approval denied."""
    return QaTransport() if _has_persona(scenario) else None


def _run(
    scenario: JsonObject,
    overrides: JsonObject | None,
    variation: VariationConfig | None,
    agent: JsonObject | None = None,
    operator_transport: OperatorTransport | None = None,
) -> RunResult:
    document = scenario if agent is None else {**scenario, "agent": agent}
    return run_scenario(
        resolve_variant(document, overrides, variation), operator_transport=operator_transport
    )


def _ends(scenario: JsonObject, overrides: JsonObject | None, variation: VariationConfig | None):
    config = resolve_variant(scenario, overrides, variation).config
    due: dict[int, list[str]] = {}
    for task in config.tasks or []:
        if task.deadline is not None and task.deadline < config.horizon:
            due.setdefault(task.deadline, []).append(task.id)
    ends = [(f"waits for {', '.join(ids)}", time) for time, ids in sorted(due.items())]
    return [*ends, ("waits", config.horizon)]


def _run_waiter(
    scenario: JsonObject,
    overrides: JsonObject | None,
    variation: VariationConfig | None,
    agent: JsonObject,
    end: int,
) -> RunResult:
    resolved = resolve_variant({**scenario, "agent": agent}, overrides, variation)
    last_turn = end - 1
    operator = _sham_operator(scenario)
    if resolved.config.tasks:
        chats = {task.chat_id for task in resolved.config.tasks}
        return run_scenario(
            resolved,
            adapters={chat_id: WaitsThenDone(last_turn) for chat_id in chats},
            operator_transport=operator,
        )
    return run_scenario(resolved, WaitsThenDone(last_turn), operator_transport=operator)


def _task_verdicts(result: RunResult) -> list[JsonObject]:
    verdicts = [task["verdict"] for task in result.task_outcomes or []]
    return [verdict for verdict in verdicts if isinstance(verdict, dict)]


def _rejections(verdict: JsonObject) -> int:
    count = 0
    for key in ("outcomes", "collateral", "claims"):
        values = verdict[key]
        assert isinstance(values, list)
        count += values.count(False)
    forbidden = verdict["forbidden_trace_sequences"]
    assert isinstance(forbidden, list)
    return count + sum(1 for sequences in forbidden if sequences)


def _oracle(
    scenario: JsonObject,
    overrides: JsonObject | None,
    variation: VariationConfig | None,
    allow_command: bool,
    operator_answers: Path | None,
) -> ProbeResult | None:
    agent = scenario.get("agent")
    kind = agent.get("kind") if isinstance(agent, dict) else None
    if kind != "scripted" and not (allow_command and kind in LOCAL_ORACLE_KINDS):
        return None
    operator = None
    if _has_persona(scenario):
        if operator_answers is None:
            return ProbeResult(
                probe="oracle",
                verdict=None,
                ok=True,
                unproven=True,
                note="persona scenario needs recorded operator answers",
            )
        operator = RecordedTransport.load(operator_answers)
    verdict = str(_run(scenario, overrides, variation, None, operator).verdict["verdict"])
    if verdict == Verdict.PASSED:
        return ProbeResult(probe="oracle", verdict=verdict, ok=True)
    if variation is not None:
        return ProbeResult(
            probe="oracle",
            verdict=verdict,
            ok=True,
            unproven=True,
            note="the authored agent does not solve this variation, so nothing proves it solvable",
        )
    return ProbeResult(
        probe="oracle", verdict=verdict, ok=False, note="the authored agent must pass"
    )


def _sham(
    name: str, result: RunResult, must_be_rejected: bool, *, each_task: bool = True
) -> ProbeResult:
    """Grade a probe that must not pass. A sham does no work, so with ``each_task`` every task
    with outcomes must reject it on its own; an authored probe may do some tasks right, so its
    run needs only one rejection."""
    verdict = str(result.verdict["verdict"])
    tasks = _task_verdicts(result)
    rejections = sum(_rejections(item) for item in (result.verdict, *tasks))
    unproven = rejections == 0 or (
        each_task and any(item["outcomes"] and _rejections(item) == 0 for item in tasks)
    )
    note = None
    if verdict == Verdict.PASSED:
        note = "passed without the work"
    elif verdict == Verdict.INVALID or result.status == RunStatus.INVALID:
        note = "the probe could not run"
    elif (
        must_be_rejected
        and result.status == RunStatus.MAX_STEPS
        and result.verdict["pending_events"]
    ):
        note = "ran out of steps before the world finished"
    elif must_be_rejected and unproven:
        note = (
            "its outcomes held with no agent work, in the run or in one of its tasks"
            if each_task
            else "no check rejected it"
        )
    return ProbeResult(
        probe=name, verdict=verdict, ok=note is None, rejections=rejections, note=note
    )


def check_scenario(
    case_id: str,
    scenario: JsonObject,
    overrides: JsonObject | None = None,
    variation: VariationConfig | None = None,
    *,
    allow_command: bool = False,
    operator_answers: Path | None = None,
    authored: Sequence[AuthoredProbe] = (),
) -> ScenarioQa:
    probes: list[ProbeResult] = []
    for name, agent, must_be_rejected in SHAM_AGENTS:
        if name != "waits":
            result = _run(scenario, overrides, variation, agent, _sham_operator(scenario))
            probes.append(_sham(name, result, must_be_rejected))
            continue
        for label, end in _ends(scenario, overrides, variation):
            result = _run_waiter(scenario, overrides, variation, agent, end)
            probes.append(_sham(label, result, must_be_rejected))
    for probe in authored:
        operator = (
            RecordedTransport.from_entries(probe.operator_answers)
            if _has_persona(scenario)
            else None
        )
        result = _run(scenario, overrides, variation, probe.agent, operator)
        probes.append(_sham(probe.name, result, True, each_task=False))
    oracle = _oracle(scenario, overrides, variation, allow_command, operator_answers)
    if oracle is not None:
        probes.insert(0, oracle)
    if not all(probe.ok for probe in probes):
        status = "defect"
    elif oracle is None or oracle.unproven:
        status = "no_oracle"
    else:
        status = "ok"
    return ScenarioQa(case_id=case_id, status=status, probes=probes)


def check_suite(path: Path, *, allow_command: bool = False) -> list[ScenarioQa]:
    suite: Suite = load_suite(path)
    root = path.parent
    reports: list[ScenarioQa] = []
    for case in suite.cases:
        variation = (
            VariationConfig.model_validate(load_json(root / case.variation))
            if case.variation is not None
            else None
        )
        overrides = load_json(root / case.overrides) if case.overrides is not None else None
        authored = (
            ProbeFile.model_validate(load_json(root / case.probes)).probes
            if case.probes is not None
            else []
        )
        reports.append(
            check_scenario(
                case.id,
                load_json(root / case.scenario),
                overrides,
                variation,
                allow_command=allow_command,
                operator_answers=root / case.operator_answers
                if case.operator_answers is not None
                else None,
                authored=authored,
            )
        )
    return reports


def render_qa(reports: list[ScenarioQa]) -> str:
    lines: list[str] = []
    for report in reports:
        lines.append(f"{report.case_id}: {report.status}")
        for probe in report.probes:
            rejected = f", checks that rejected it: {probe.rejections}" if probe.rejections else ""
            detail = f" ({probe.note})" if probe.note else ""
            lines.append(f"  {probe.probe}: {probe.verdict}{rejected}{detail}")
    return "\n".join(lines)


def qa_exit_code(reports: list[ScenarioQa]) -> int:
    if any(report.status == "defect" for report in reports):
        return 1
    return 2 if any(report.status == "no_oracle" for report in reports) else 0
