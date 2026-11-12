import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from overnight.domain.immutable import export_json
from overnight.domain.models import TraceEntry


class InspectionError(ValueError): ...


class InspectionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TraceQuery(InspectionContract):
    task_id: str | None = None
    chat_id: str | None = None
    actor_id: str | None = None
    invocation_id: str | None = None
    event_id: str | None = None
    kind: str | None = None
    time_from: int | None = None
    time_to: int | None = None
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> "TraceQuery":
        if (
            self.time_from is not None
            and self.time_to is not None
            and self.time_from > self.time_to
        ):
            raise ValueError("time_from must be less than or equal to time_to")
        return self


class TraceRecord(InspectionContract):
    line: int
    entry: TraceEntry


class TraceQueryResult(InspectionContract):
    matched: int
    returned: int
    records: tuple[TraceRecord, ...]


class TraceLink(InspectionContract):
    predecessor_line: int
    descendant_line: int
    relation: Literal["cause", "invocation", "event"]
    reference: str


class LinkIssue(InspectionContract):
    line: int
    relation: Literal["cause", "invocation", "event"]
    reference: str
    issue: Literal["missing", "ambiguous", "future"]
    candidate_lines: tuple[int, ...] = ()


class CausalNode(InspectionContract):
    line: int
    predecessor_lines: tuple[int, ...]
    descendant_lines: tuple[int, ...]


class TraceGraph(InspectionContract):
    links: tuple[TraceLink, ...]
    issues: tuple[LinkIssue, ...]
    nodes: tuple[CausalNode, ...]


class EvidenceFinding(InspectionContract):
    line: int
    sequence: int
    kind: str
    invocation_id: str | None = None
    status: str | None = None
    detail: str | None = None


class InspectionSummary(InspectionContract):
    trace_entries: int
    kinds: dict[str, int]
    outcomes: dict[str, int]
    failures: tuple[EvidenceFinding, ...]
    policy_denials: tuple[EvidenceFinding, ...]
    policy_pending: tuple[EvidenceFinding, ...]
    pending_facts: dict[str, JsonValue]
    evaluation_outcomes: JsonValue | None = None
    evaluation_collateral: JsonValue | None = None
    evaluation_claims: JsonValue | None = None
    evaluation_forbidden: JsonValue | None = None
    verdict: str | None = None
    provenance_available: bool


class InspectionReport(InspectionContract):
    trace_path: str
    query: TraceQuery
    results: TraceQueryResult
    graph: TraceGraph
    summary: InspectionSummary
    provenance: dict[str, JsonValue] | None = None
    native_evidence: Literal["separate and unsupported"] = "separate and unsupported"


def trace_lines(data: bytes) -> list[str]:
    """Split a trace file into lines, numbered from the first.

    A run appends to its trace while it runs, so a reader can see the last line half written:
    a last line with no newline that is not yet whole JSON is left out."""
    ended, newline, last = data.rpartition(b"\n")
    lines = (ended + newline).decode("utf-8").splitlines()
    if last.strip():
        try:
            json.loads(last)
        except ValueError:
            return lines
        lines.append(last.decode("utf-8"))
    return lines


def load_trace(path: Path) -> tuple[TraceRecord, ...]:
    try:
        lines = trace_lines(path.read_bytes())
    except (OSError, UnicodeError) as error:
        raise InspectionError(f"cannot read trace {path}: {error}") from error
    records: list[TraceRecord] = []
    for line_number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        try:
            entry = TraceEntry.model_validate_json(text)
        except ValidationError as error:
            detail = error.errors(include_url=False)[0]
            location = ".".join(str(part) for part in detail["loc"]) or "entry"
            raise InspectionError(
                f"invalid trace {path} line {line_number} at {location}: {detail['msg']}"
            ) from error
        records.append(TraceRecord(line=line_number, entry=entry))
    return tuple(records)


def query_trace(records: Sequence[TraceRecord], query: TraceQuery) -> TraceQueryResult:
    matches = tuple(record for record in records if _matches(record.entry, query))
    returned = matches[: query.limit] if query.limit is not None else matches
    return TraceQueryResult(matched=len(matches), returned=len(returned), records=returned)


def _matches(entry: TraceEntry, query: TraceQuery) -> bool:
    exact = (
        (query.task_id, entry.task_id),
        (query.chat_id, entry.chat_id),
        (query.actor_id, entry.actor_id),
        (query.invocation_id, entry.invocation_id),
        (query.event_id, entry.event_id),
        (query.kind, entry.kind),
    )
    if any(expected is not None and expected != actual for expected, actual in exact):
        return False
    if query.time_from is not None and entry.time < query.time_from:
        return False
    return query.time_to is None or entry.time <= query.time_to


def build_trace_graph(records: Sequence[TraceRecord]) -> TraceGraph:
    invocation_anchors = _anchors(records, "invocation", "invocation_id")
    event_anchors = _anchors(records, "event", "event_id")
    links: list[TraceLink] = []
    issues: list[LinkIssue] = []

    for record in records:
        entry = record.entry
        if entry.cause_id is not None:
            _link_reference(
                record.line,
                "cause",
                entry.cause_id,
                invocation_anchors.get(entry.cause_id, ()),
                links,
                issues,
            )
        if entry.invocation_id is not None and entry.kind in {"fault", "state_change", "result"}:
            _link_reference(
                record.line,
                "invocation",
                entry.invocation_id,
                invocation_anchors.get(entry.invocation_id, ()),
                links,
                issues,
            )
        if entry.event_id is not None and entry.kind != "event":
            _link_reference(
                record.line,
                "event",
                entry.event_id,
                event_anchors.get(entry.event_id, ()),
                links,
                issues,
            )

    unique_links_by_key = {
        (link.predecessor_line, link.descendant_line, link.relation, link.reference): link
        for link in links
    }
    unique_links = tuple(unique_links_by_key.values())
    predecessors: dict[int, set[int]] = defaultdict(set)
    descendants: dict[int, set[int]] = defaultdict(set)
    for link in unique_links:
        predecessors[link.descendant_line].add(link.predecessor_line)
        descendants[link.predecessor_line].add(link.descendant_line)
    nodes = tuple(
        CausalNode(
            line=record.line,
            predecessor_lines=tuple(sorted(predecessors[record.line])),
            descendant_lines=tuple(sorted(descendants[record.line])),
        )
        for record in records
    )
    return TraceGraph(
        links=unique_links,
        issues=tuple(issues),
        nodes=nodes,
    )


def _anchors(
    records: Sequence[TraceRecord], kind: str, field: Literal["invocation_id", "event_id"]
) -> dict[str, tuple[int, ...]]:
    found: dict[str, list[int]] = defaultdict(list)
    for record in records:
        value = getattr(record.entry, field)
        if record.entry.kind == kind and value is not None:
            found[value].append(record.line)
    return {key: tuple(value) for key, value in found.items()}


def _link_reference(
    descendant: int,
    relation: Literal["cause", "invocation", "event"],
    reference: str,
    candidates: tuple[int, ...],
    links: list[TraceLink],
    issues: list[LinkIssue],
) -> None:
    if len(candidates) != 1:
        issues.append(
            LinkIssue(
                line=descendant,
                relation=relation,
                reference=reference,
                issue="missing" if not candidates else "ambiguous",
                candidate_lines=candidates,
            )
        )
        return
    if candidates[0] >= descendant:
        issues.append(
            LinkIssue(
                line=descendant,
                relation=relation,
                reference=reference,
                issue="future",
                candidate_lines=candidates,
            )
        )
        return
    links.append(
        TraceLink(
            predecessor_line=candidates[0],
            descendant_line=descendant,
            relation=relation,
            reference=reference,
        )
    )


def summarize_trace(
    records: Sequence[TraceRecord],
    verdict: Mapping[str, JsonValue] | None = None,
    provenance: Mapping[str, JsonValue] | None = None,
) -> InspectionSummary:
    kinds: dict[str, int] = defaultdict(int)
    outcomes: dict[str, int] = defaultdict(int)
    failures: list[EvidenceFinding] = []
    denials: list[EvidenceFinding] = []
    pending: list[EvidenceFinding] = []
    for record in records:
        entry = record.entry
        kinds[entry.kind] += 1
        payload = export_json(entry.payload)
        if not isinstance(payload, dict):
            continue
        status = payload.get("status")
        status_text = status if isinstance(status, str) else None
        if entry.kind == "result" and status_text is not None:
            outcomes[status_text] += 1
            if status_text in {"failed", "denied"}:
                failures.append(_finding(record, status_text, payload.get("error")))
        if entry.kind == "policy_check":
            decision = payload.get("decision")
            if decision == "denied":
                denials.append(_finding(record, "denied", None))
            elif decision == "pending":
                pending.append(_finding(record, "pending", None))
    pending_facts: dict[str, JsonValue] = {}
    verdict_text: str | None = None
    if verdict is not None:
        value = verdict.get("verdict")
        verdict_text = value if isinstance(value, str) else None
        for key in ("pending_events", "pending_operator_steps", "pending_approvals"):
            if key in verdict:
                pending_facts[key] = verdict[key]
    return InspectionSummary(
        trace_entries=len(records),
        kinds=dict(sorted(kinds.items())),
        outcomes=dict(sorted(outcomes.items())),
        failures=tuple(failures),
        policy_denials=tuple(denials),
        policy_pending=tuple(pending),
        pending_facts=pending_facts,
        evaluation_outcomes=verdict.get("outcomes") if verdict is not None else None,
        evaluation_collateral=verdict.get("collateral") if verdict is not None else None,
        evaluation_claims=verdict.get("claims") if verdict is not None else None,
        evaluation_forbidden=(
            verdict.get("forbidden_trace_sequences") if verdict is not None else None
        ),
        verdict=verdict_text,
        provenance_available=provenance is not None,
    )


def _finding(record: TraceRecord, status: str, detail: object) -> EvidenceFinding:
    return EvidenceFinding(
        line=record.line,
        sequence=record.entry.sequence,
        kind=record.entry.kind,
        invocation_id=record.entry.invocation_id,
        status=status,
        detail=detail if isinstance(detail, str) else None,
    )


def inspect_trace(
    trace_path: Path,
    query: TraceQuery,
    *,
    verdict: Mapping[str, JsonValue] | None = None,
    provenance: Mapping[str, JsonValue] | None = None,
) -> InspectionReport:
    records = load_trace(trace_path)
    return InspectionReport(
        trace_path=str(trace_path),
        query=query,
        results=query_trace(records, query),
        graph=build_trace_graph(records),
        summary=summarize_trace(records, verdict, provenance),
        provenance=dict(provenance) if provenance is not None else None,
    )
