import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from overnight.domain.contracts import Scenario
from overnight.domain.models import json_equal, json_matches
from overnight.domain.values import JsonObject, at_path
from overnight.execution.evaluation import NOT_EFFECTS, holds
from overnight.services.operations import ServiceBoundary

type Layer = Literal["outcome", "collateral", "claims", "forbidden"]
type Lane = Literal["operator", "agent", "tools", "people", "services", "views", "system"]

LAYERS: tuple[tuple[Layer, str, str], ...] = (
    ("outcome", "outcomes", "outcomes"),
    ("collateral", "collateral", "collateral"),
    ("claims", "claims", "claims"),
    ("forbidden", "forbidden", "forbidden_trace_sequences"),
)
VIEW_KINDS = frozenset({"observation", "agent_observation"})
_LANES: Mapping[str, Lane] = {
    "operator_message": "operator",
    "operator_approval": "operator",
    "operator_status": "operator",
    "operator_request": "operator",
    "operator_answer": "operator",
    "operator_failure": "operator",
    "agent_wait": "agent",
    "agent_report": "agent",
    "agent_done": "agent",
    "agent_idle": "agent",
    "notification_delivered": "agent",
    "policy_check": "tools",
    "invocation": "tools",
    "result": "tools",
    "fault": "tools",
    "event": "people",
    "state_change": "services",
    "notification": "services",
    "reminder_fired": "services",
    "observation": "views",
    "agent_observation": "views",
}
_LABEL_LIMIT = 120


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckView(ApiModel):
    layer: Layer
    index: int
    task_id: str | None = None
    label: str | None
    text: str
    predicate: str
    source: str
    passed: bool | None
    evidence_lines: list[int]


class TaskView(ApiModel):
    task_id: str
    chat_id: str | None
    message: str | None
    status: str | None
    verdict: str | None
    completed_at: int | None
    steps: int | None
    checks: list[CheckView]


class TimelineItem(ApiModel):
    line: int
    sequence: int | None
    time: int | None
    lane: Lane
    row: str
    kind: str
    label: str
    actor_id: str | None
    invocation_id: str | None
    event_id: str | None
    task_id: str | None
    chat_id: str | None
    status: str | None
    view: bool


class Pending(ApiModel):
    events: JsonValue
    operator_steps: JsonValue
    approvals: JsonValue


class ProvenanceSummary(ApiModel):
    model: JsonValue
    connector: JsonValue
    connector_status: str | None
    adapter_kind: str | None
    adapter_identity: str | None
    backend_commit: str | None
    scenario_sha256: str | None
    seed: int | None


class RunFailure(ApiModel):
    kind: str | None
    retryable: bool | None
    type: str | None
    message: str | None


class RunDetail(ApiModel):
    id: str
    scenario_id: str | None
    schema_current: bool
    schema_error: str | None
    verdict: str | None
    run_status: str | None
    failure: RunFailure | None
    rejected_actions: int
    agent_kind: str | None
    checks: list[CheckView]
    tasks: list[TaskView]
    pending: Pending | None
    provenance: ProvenanceSummary | None
    variation: JsonValue
    scenario: JsonValue
    final_state: JsonValue
    trace_entries: int
    kinds: dict[str, int]
    timeline: list[TimelineItem]
    issues: list[str]


class OperationView(ApiModel):
    name: str
    description: str
    read_only: bool
    input_schema: JsonObject


class ServiceView(ApiModel):
    id: str
    kind: str
    operations: list[OperationView]
    state: JsonValue


class ScenarioCheckSet(ApiModel):
    checks: list[CheckView]
    tasks: dict[str, list[CheckView]]


@dataclass(frozen=True)
class RunArtifacts:
    scenario: JsonValue
    trace: Sequence[tuple[int, JsonValue]] = ()
    verdict: JsonValue = None
    final_state: JsonValue = None
    provenance: JsonValue = None
    task_outcomes: JsonValue = None
    variation: JsonValue = None
    issues: Sequence[str] = field(default_factory=tuple)


def _obj(value: JsonValue | object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue | object) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _str(value: JsonValue | object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: JsonValue | object) -> int | None:
    return value if type(value) is int else None


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _LABEL_LIMIT else text[: _LABEL_LIMIT - 1] + "…"


def schema_status(scenario: JsonValue) -> tuple[bool, str | None]:
    try:
        Scenario.model_validate(scenario)
    except ValidationError as error:
        detail = error.errors(include_url=False)[0]
        location = ".".join(str(part) for part in detail["loc"]) or "scenario"
        return False, f"{location}: {detail['msg']}"
    return True, None


def render_path(path: JsonValue) -> str:
    text = ""
    for part in _list(path):
        if type(part) is int:
            text += f"[{part}]"
        else:
            text += ("." if text else "") + str(part)
    return text or "<root>"


def _flatten(match: JsonObject, prefix: str = "") -> list[str]:
    parts: list[str] = []
    for key, value in match.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            parts.extend(_flatten(value, name))
        else:
            rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            parts.append(f"{name}={rendered}")
    return parts


def render_predicate(layer: Layer, check: JsonObject) -> str:
    if layer == "forbidden":
        return "no trace entry with " + ", ".join(_flatten(_obj(check.get("match"))))
    if layer == "claims":
        required = render_predicate("outcome", _obj(check.get("requires")))
        return f"a report matching {check.get('pattern')} needs {required}"
    path = render_path(check.get("path"))
    op = check.get("op")
    if op == "eq":
        body = f"{path} = {json.dumps(check.get('value'), sort_keys=True, ensure_ascii=False)}"
    elif op in {"exists", "unchanged"}:
        body = f"{path} {op}"
    elif op == "before":
        first = ", ".join(_flatten(_obj(check.get("first"))))
        body = f"{first} before " + ", ".join(_flatten(_obj(check.get("then"))))
    else:
        body = json.dumps(check, sort_keys=True, ensure_ascii=False)
    source = _str(check.get("source")) or "state"
    return body if source == "state" else f"{source.replace('_', ' ')}: {body}"


@dataclass(frozen=True)
class _TraceIndex:
    entries: tuple[tuple[int, JsonObject], ...]
    line_by_sequence: Mapping[int, int]
    last_state_change: Mapping[str, int]
    lines_by_invocation: Mapping[str, tuple[int, ...]]

    @classmethod
    def build(cls, trace: Sequence[tuple[int, JsonValue]]) -> "_TraceIndex":
        entries = tuple((line, row) for line, row in trace if isinstance(row, dict))
        by_sequence: dict[int, int] = {}
        last_change: dict[str, int] = {}
        by_invocation: dict[str, list[int]] = {}
        for line, row in entries:
            if (sequence := _int(row.get("sequence"))) is not None:
                by_sequence.setdefault(sequence, line)
            kind = row.get("kind")
            if kind == "state_change" and (
                service := _str(_obj(row.get("payload")).get("service_id"))
            ):
                last_change[service] = line
            invocation = _str(row.get("invocation_id"))
            if invocation is not None and kind not in NOT_EFFECTS:
                by_invocation.setdefault(invocation, []).append(line)
        return cls(
            entries,
            by_sequence,
            last_change,
            {key: tuple(value) for key, value in by_invocation.items()},
        )


def _matches_value(check: JsonObject, evidence: JsonValue) -> bool:
    try:
        value = at_path(
            evidence, [part for part in _list(check.get("path")) if isinstance(part, str | int)]
        )
    except KeyError:
        return False
    op = check.get("op")
    return op not in {"eq", "match"} or holds(str(op), value, check.get("value"))


def _state_lines(path: list[JsonValue], index: _TraceIndex) -> list[int]:
    if len(path) >= 2 and path[0] == "services" and isinstance(path[1], str):
        line = index.last_state_change.get(path[1])
        return [line] if line is not None else []
    if len(path) >= 2 and path[0] == "results" and isinstance(path[1], str):
        return list(index.lines_by_invocation.get(path[1], ()))
    return []


def _reports(index: _TraceIndex) -> list[int]:
    return [line for line, row in index.entries if row.get("kind") == "agent_report"]


def _check_lines(
    layer: Layer, check: JsonObject, passed: JsonValue, index: _TraceIndex
) -> list[int]:
    if layer == "forbidden":
        sequences = [_int(sequence) for sequence in _list(passed)]
        return sorted(
            {
                index.line_by_sequence[s]
                for s in sequences
                if s is not None and s in index.line_by_sequence
            }
        )
    if layer == "claims":
        return _reports(index) + _check_lines("outcome", _obj(check.get("requires")), passed, index)
    source = _str(check.get("source")) or "state"
    if source == "state":
        return _state_lines(_list(check.get("path")), index)
    if source == "agent_report":
        return [
            line
            for line, row in index.entries
            if row.get("kind") == "agent_report" and _matches_value(check, _obj(row.get("payload")))
        ]
    if check.get("op") == "before":
        patterns = (check.get("first"), check.get("then"))
        return [
            line
            for line, row in index.entries
            if row.get("kind") not in NOT_EFFECTS
            and any(json_matches(row, pattern) for pattern in patterns)
        ]
    return [
        line
        for line, row in index.entries
        if row.get("kind") not in NOT_EFFECTS and _matches_value(check, row)
    ]


def _passed(layer: Layer, value: JsonValue) -> bool | None:
    if layer == "forbidden":
        return not value if isinstance(value, list) else None
    return value if isinstance(value, bool) else None


def check_views(
    checks: JsonValue,
    verdict: JsonValue,
    index: _TraceIndex | None = None,
    *,
    task_id: str | None = None,
) -> list[CheckView]:
    views: list[CheckView] = []
    for layer, check_key, verdict_key in LAYERS:
        results = _list(_obj(verdict).get(verdict_key))
        for position, raw in enumerate(_list(_obj(checks).get(check_key))):
            check = _obj(raw)
            result = results[position] if position < len(results) else None
            predicate = render_predicate(layer, check)
            label = _str(check.get("label"))
            views.append(
                CheckView(
                    layer=layer,
                    index=position,
                    task_id=task_id,
                    label=label,
                    text=label or predicate,
                    predicate=predicate,
                    source=_str(check.get("source"))
                    or ("trace" if layer == "forbidden" else "state"),
                    passed=_passed(layer, result) if verdict is not None else None,
                    evidence_lines=_check_lines(layer, check, result, index) if index else [],
                )
            )
    return views


def _task_views(scenario: JsonObject, outcomes: JsonValue, index: _TraceIndex) -> list[TaskView]:
    by_id = {_str(_obj(item).get("task_id")): _obj(item) for item in _list(outcomes)}
    views: list[TaskView] = []
    for raw in _list(scenario.get("tasks")):
        task = _obj(raw)
        task_id = _str(task.get("id"))
        if task_id is None:
            continue
        outcome = by_id.get(task_id)
        verdict = outcome.get("verdict") if outcome is not None else None
        views.append(
            TaskView(
                task_id=task_id,
                chat_id=_str(task.get("chat_id")),
                message=_str(task.get("message")),
                status=_str(outcome.get("status")) if outcome else None,
                verdict=_str(_obj(verdict).get("verdict")),
                completed_at=_int(outcome.get("completed_at")) if outcome else None,
                steps=_int(outcome.get("steps")) if outcome else None,
                checks=check_views(task.get("checks"), verdict, index, task_id=task_id),
            )
        )
    return views


def _row(lane: Lane, kind: str, row: JsonObject, payload: JsonObject) -> str:
    if lane == "tools":
        return _str(row.get("invocation_id")) or _str(row.get("actor_id")) or kind
    if lane == "services":
        return _str(payload.get("service_id")) or "unknown"
    if lane == "system":
        return kind
    return _str(row.get("actor_id")) or lane


def _status(kind: str, payload: JsonObject) -> str | None:
    if kind == "policy_check":
        return _str(payload.get("decision"))
    if kind == "operator_approval":
        return _str(payload.get("decision"))
    if kind == "operator_answer":
        return _str(_obj(payload.get("answer")).get("decision")) or "reply"
    if kind == "operator_failure":
        return "error"
    if kind in {"result", "event"}:
        return _str(payload.get("status"))
    if kind == "operator_status":
        return _str(payload.get("agent_status"))
    if kind == "fault" or kind.endswith("_error"):
        return "error" if kind.endswith("_error") else "fault"
    return None


_PAYLOAD_LABELS: Mapping[str, tuple[str, str]] = {
    "operator_message": ("", "message"),
    "agent_report": ("", "message"),
    "state_change": ("", "service_id"),
    "fault": ("fault ", "fault_id"),
    "operator_approval": ("", "decision"),
    "operator_status": ("status: ", "agent_status"),
    "notification": ("", "summary"),
    "notification_delivered": ("delivered via ", "via"),
    "reminder_fired": ("", "reminder_id"),
}


_OPERATOR_KINDS = frozenset({"operator_request", "operator_answer", "operator_failure"})


def _operator_label(kind: str, payload: JsonObject) -> str:
    if kind == "operator_request":
        if payload.get("kind") == "approval":
            return f"asked: {payload.get('service_id')}.{payload.get('operation')}"
        return f"asked: {_str(payload.get('message')) or 'report'}"
    if kind == "operator_answer":
        answer = _obj(payload.get("answer"))
        return _str(answer.get("message")) or _str(answer.get("decision")) or "no reply"
    return f"operator failed: {_str(payload.get('error')) or ''}".rstrip(": ")


def _label(kind: str, row: JsonObject, payload: JsonObject) -> str:
    if kind in {"invocation", "policy_check"}:
        operation = f"{payload.get('service_id')}.{payload.get('operation')}"
        decision = _str(payload.get("decision"))
        return operation if decision is None else f"{operation}: {decision}"
    if kind == "result":
        error = _str(payload.get("error"))
        status = _str(payload.get("status")) or "result"
        return f"{status}: {error}" if error else status
    if kind == "event":
        return _str(row.get("event_id")) or "event"
    if kind == "agent_wait":
        until = payload.get("until")
        return "wait" if until is None else f"wait until {until}"
    if kind == "agent_done":
        return "done"
    if kind == "agent_idle":
        return "idle"
    if kind in _OPERATOR_KINDS:
        return _operator_label(kind, payload)
    if kind in _PAYLOAD_LABELS:
        prefix, key = _PAYLOAD_LABELS[kind]
        value = payload.get(key)
        return prefix + (value if isinstance(value, str) else kind)
    if kind.endswith("_error"):
        return f"{kind}: {_str(payload.get('error')) or ''}".rstrip(": ")
    return kind


def timeline_items(trace: Sequence[tuple[int, JsonValue]]) -> list[TimelineItem]:
    items: list[TimelineItem] = []
    for line, raw in trace:
        row = _obj(raw)
        kind = _str(row.get("kind")) or "unknown"
        payload = _obj(row.get("payload"))
        lane = _LANES.get(kind, "system")
        items.append(
            TimelineItem(
                line=line,
                sequence=_int(row.get("sequence")),
                time=_int(row.get("time")),
                lane=lane,
                row=_row(lane, kind, row, payload),
                kind=kind,
                label=_short(_label(kind, row, payload)),
                actor_id=_str(row.get("actor_id")),
                invocation_id=_str(row.get("invocation_id")),
                event_id=_str(row.get("event_id")),
                task_id=_str(row.get("task_id")),
                chat_id=_str(row.get("chat_id")),
                status=_status(kind, payload),
                view=kind in VIEW_KINDS,
            )
        )
    return items


def provenance_summary(provenance: JsonValue) -> ProvenanceSummary | None:
    if not isinstance(provenance, dict):
        return None
    adapter = _obj(provenance.get("adapter"))
    scenario = _obj(provenance.get("scenario"))
    return ProvenanceSummary(
        model=provenance.get("model"),
        connector=provenance.get("connector"),
        connector_status=_str(provenance.get("connector_status")),
        adapter_kind=_str(adapter.get("kind")),
        adapter_identity=_str(adapter.get("identity")),
        backend_commit=_str(_obj(provenance.get("backend")).get("git_commit")),
        scenario_sha256=_str(scenario.get("sha256")),
        seed=_int(scenario.get("seed")),
    )


def _pending(verdict: JsonValue) -> Pending | None:
    if not isinstance(verdict, dict):
        return None
    return Pending(
        events=verdict.get("pending_events", []),
        operator_steps=verdict.get("pending_operator_steps", []),
        approvals=verdict.get("pending_approvals", []),
    )


def _failure(verdict: JsonObject | None) -> RunFailure | None:
    failure = verdict.get("failure") if verdict else None
    if not isinstance(failure, dict):
        return None
    retryable = failure.get("retryable")
    return RunFailure(
        kind=_str(failure.get("kind")),
        retryable=retryable if isinstance(retryable, bool) else None,
        type=_str(failure.get("type")),
        message=_str(failure.get("message")),
    )


def build_run_detail(run_id: str, artifacts: RunArtifacts) -> RunDetail:
    scenario = _obj(artifacts.scenario)
    schema_current, schema_error = schema_status(artifacts.scenario)
    index = _TraceIndex.build(artifacts.trace)
    verdict = _obj(artifacts.verdict) if artifacts.verdict is not None else None
    kinds = Counter(_str(row.get("kind")) or "unknown" for _, row in index.entries)
    return RunDetail(
        id=run_id,
        scenario_id=_str(scenario.get("id")),
        schema_current=schema_current,
        schema_error=schema_error,
        verdict=_str(verdict.get("verdict")) if verdict else None,
        run_status=_str(verdict.get("run_status")) if verdict else None,
        failure=_failure(verdict),
        rejected_actions=_int(verdict.get("rejected_actions")) or 0 if verdict else 0,
        agent_kind=_str(_obj(scenario.get("agent")).get("kind")),
        checks=check_views(scenario.get("checks"), verdict, index),
        tasks=_task_views(scenario, artifacts.task_outcomes, index),
        pending=_pending(verdict),
        provenance=provenance_summary(artifacts.provenance),
        variation=artifacts.variation,
        scenario=artifacts.scenario,
        final_state=artifacts.final_state,
        trace_entries=len(artifacts.trace),
        kinds=dict(sorted(kinds.items())),
        timeline=timeline_items(artifacts.trace),
        issues=list(artifacts.issues),
    )


def scenario_checks(scenario: JsonValue) -> ScenarioCheckSet:
    document = _obj(scenario)
    tasks = {
        task_id: check_views(task.get("checks"), None, task_id=task_id)
        for task in map(_obj, _list(document.get("tasks")))
        if (task_id := _str(task.get("id"))) is not None
    }
    return ScenarioCheckSet(checks=check_views(document.get("checks"), None), tasks=tasks)


def service_views(scenario: JsonValue, catalog: Mapping[str, ServiceBoundary]) -> list[ServiceView]:
    views: list[ServiceView] = []
    for raw in _list(_obj(scenario).get("services")):
        service = _obj(raw)
        kind = _str(service.get("kind")) or ""
        boundary = catalog.get(kind)
        operations = (
            [
                OperationView(
                    name=operation.name,
                    description=operation.description,
                    read_only=operation.read_only,
                    input_schema=operation.input_schema,
                )
                for operation in boundary.operations.values()
            ]
            if boundary is not None
            else []
        )
        views.append(
            ServiceView(
                id=_str(service.get("id")) or "",
                kind=kind,
                operations=operations,
                state=service.get("state"),
            )
        )
    return views


class ComparedCheck(ApiModel):
    layer: Layer
    index: int
    task_id: str | None
    text: str
    in_a: bool
    in_b: bool
    a: bool | None
    b: bool | None
    differs: bool
    definition_changed: bool


class TraceStep(ApiModel):
    line: int
    sequence: int | None
    time: int | None
    kind: str
    label: str


class Divergence(ApiModel):
    """The first position where the two runs' non-view trace entries differ."""

    position: int
    fields: list[str]
    a: TraceStep | None
    b: TraceStep | None


class RunComparison(ApiModel):
    a: str
    b: str
    scenario_a: str | None
    scenario_b: str | None
    checks: list[ComparedCheck]
    compared_entries: int
    divergence: Divergence | None


_COMPARED_FIELDS = ("kind", "actor", "operation", "args", "result")


def _signature(row: JsonObject) -> dict[str, JsonValue]:
    payload = _obj(row.get("payload"))
    return {
        "kind": row.get("kind"),
        "actor": row.get("actor_id"),
        "operation": [payload.get("service_id"), payload.get("operation")],
        "args": payload.get("args"),
        "result": [payload.get(key) for key in ("status", "decision", "value", "error")],
    }


def _step(line: int, row: JsonObject) -> TraceStep:
    kind = _str(row.get("kind")) or "unknown"
    return TraceStep(
        line=line,
        sequence=_int(row.get("sequence")),
        time=_int(row.get("time")),
        kind=kind,
        label=_short(_label(kind, row, _obj(row.get("payload")))),
    )


def first_divergence(
    a: Sequence[tuple[int, JsonValue]], b: Sequence[tuple[int, JsonValue]]
) -> tuple[int, Divergence | None]:
    def stream(trace: Sequence[tuple[int, JsonValue]]) -> list[tuple[int, JsonObject]]:
        return [
            (line, row)
            for line, row in trace
            if isinstance(row, dict) and row.get("kind") not in VIEW_KINDS
        ]

    left, right = stream(a), stream(b)
    for position in range(max(len(left), len(right))):
        if position >= len(left) or position >= len(right):
            present = left if position < len(left) else right
            step = _step(*present[position])
            return min(len(left), len(right)), Divergence(
                position=position,
                fields=["presence"],
                a=step if present is left else None,
                b=step if present is right else None,
            )
        (line_a, row_a), (line_b, row_b) = left[position], right[position]
        sig_a, sig_b = _signature(row_a), _signature(row_b)
        fields = [key for key in _COMPARED_FIELDS if not json_equal(sig_a[key], sig_b[key])]
        if fields:
            return position, Divergence(
                position=position, fields=fields, a=_step(line_a, row_a), b=_step(line_b, row_b)
            )
    return len(left), None


type _CheckKey = tuple[str | None, str, int]


def _check_definitions(scenario: JsonValue) -> dict[_CheckKey, JsonObject]:
    document = _obj(scenario)
    sources: list[tuple[str | None, JsonValue]] = [(None, document.get("checks"))]
    for raw in _list(document.get("tasks")):
        task = _obj(raw)
        if (task_id := _str(task.get("id"))) is not None:
            sources.append((task_id, task.get("checks")))
    found: dict[_CheckKey, JsonObject] = {}
    for task_id, checks in sources:
        for layer, check_key, _ in LAYERS:
            for position, raw in enumerate(_list(_obj(checks).get(check_key))):
                definition = {key: value for key, value in _obj(raw).items() if key != "label"}
                found[(task_id, layer, position)] = definition
    return found


def compare_checks(a: RunDetail, b: RunDetail) -> list[ComparedCheck]:
    def keyed(detail: RunDetail) -> dict[tuple[str | None, str, int], CheckView]:
        views = [*detail.checks, *(check for task in detail.tasks for check in task.checks)]
        return {(view.task_id, view.layer, view.index): view for view in views}

    left, right = keyed(a), keyed(b)
    definitions_a, definitions_b = _check_definitions(a.scenario), _check_definitions(b.scenario)
    order = list(left) + [key for key in right if key not in left]
    compared: list[ComparedCheck] = []
    for key in order:
        view_a, view_b = left.get(key), right.get(key)
        shown = view_a or view_b
        assert shown is not None
        passed_a = view_a.passed if view_a else None
        passed_b = view_b.passed if view_b else None
        compared.append(
            ComparedCheck(
                layer=shown.layer,
                index=shown.index,
                task_id=shown.task_id,
                text=shown.text,
                in_a=view_a is not None,
                in_b=view_b is not None,
                a=passed_a,
                b=passed_b,
                differs=view_a is None or view_b is None or passed_a != passed_b,
                definition_changed=view_a is not None
                and view_b is not None
                and not json_equal(definitions_a.get(key), definitions_b.get(key)),
            )
        )
    return compared


def compare_runs(
    a: RunDetail,
    trace_a: Sequence[tuple[int, JsonValue]],
    b: RunDetail,
    trace_b: Sequence[tuple[int, JsonValue]],
) -> RunComparison:
    compared, divergence = first_divergence(trace_a, trace_b)
    return RunComparison(
        a=a.id,
        b=b.id,
        scenario_a=a.scenario_id,
        scenario_b=b.scenario_id,
        checks=compare_checks(a, b),
        compared_entries=compared,
        divergence=divergence,
    )
