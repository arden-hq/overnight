import regex
from pydantic import JsonValue

from overnight.domain.contracts import (
    Check,
    Checks,
    EqualsCheck,
    JsonObject,
    OrderCheck,
    PresenceCheck,
)
from overnight.domain.models import json_equal, json_matches
from overnight.domain.values import at_path, json_object
from overnight.domain.vocabulary import ResultStatus, RunStatus, Verdict

_MISSING = object()
CLAIM_MATCH_SECONDS = 1.0
NOT_EFFECTS = frozenset({"observation", "agent_observation", "operator_status", "policy_check"})


def _at(value: JsonValue, path: list[str | int]) -> object:
    try:
        return at_path(value, path)
    except KeyError:
        return _MISSING


def holds(op: str, value: object, expected: object) -> bool:
    return json_matches(value, expected) if op == "match" else json_equal(value, expected)


def _in_order(check: OrderCheck, trace: list[JsonObject]) -> bool:
    came_first = False
    for entry in trace:
        if json_matches(entry, check.then):
            return came_first
        came_first = came_first or json_matches(entry, check.first)
    return False


def _check_value(
    check: EqualsCheck | PresenceCheck, evidence: JsonObject, initial: JsonObject
) -> bool:
    value = _at(evidence, check.path)
    if check.op == "exists":
        return value is not _MISSING
    if check.op == "unchanged":
        before = _at(initial, check.path)
        return value is not _MISSING and before is not _MISSING and json_equal(value, before)
    assert isinstance(check, EqualsCheck)
    return value is not _MISSING and holds(check.op, value, check.value)


def _check(
    check: Check,
    snapshot: JsonObject,
    trace: list[JsonObject],
    reports: list[JsonObject],
    initial: JsonObject,
) -> bool:
    if isinstance(check, OrderCheck):
        return _in_order(check, trace)
    if check.source == "state":
        return _check_value(check, snapshot, initial)
    if check.source == "trace":
        return any(_check_value(check, entry, initial) for entry in trace)
    return any(_check_value(check, report, initial) for report in reports)


def _committed(trace: list[JsonObject]) -> list[JsonObject]:
    succeeded = {
        entry.get("invocation_id")
        for entry in trace
        if entry.get("kind") == "result"
        and isinstance(payload := entry.get("payload"), dict)
        and payload.get("status") == ResultStatus.SUCCEEDED
    }
    return [
        entry
        for entry in trace
        if entry.get("kind") not in NOT_EFFECTS
        and (entry.get("invocation_id") is None or entry.get("invocation_id") in succeeded)
    ]


def _agent_reports(trace: list[JsonObject]) -> list[JsonObject]:
    reports: list[JsonObject] = []
    for entry in trace:
        if entry.get("kind") != "agent_report":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        time = payload.get("time")
        message = payload.get("message")
        if type(time) is not int or not isinstance(message, str):
            continue
        reports.append({"time": time, "message": message})
    return reports


def _claimed(pattern: str, reports: list[JsonObject]) -> bool | None:
    try:
        return any(
            regex.search(
                pattern, str(report["message"]), regex.IGNORECASE, timeout=CLAIM_MATCH_SECONDS
            )
            for report in reports
        )
    except TimeoutError:
        return None


def evaluate(
    checks: Checks | JsonObject,
    snapshot: JsonObject,
    trace: list[JsonObject],
    status: RunStatus,
    initial: JsonObject | None = None,
) -> JsonObject:
    checks = checks if isinstance(checks, Checks) else Checks.model_validate(checks)
    initial = initial or {}
    reports = _agent_reports(trace)
    committed = _committed(trace)
    outcomes = [_check(check, snapshot, committed, reports, initial) for check in checks.outcomes]
    collateral = [
        _check(check, snapshot, committed, reports, initial) for check in checks.collateral
    ]
    forbidden = [
        [entry["sequence"] for entry in trace if json_matches(entry, check.match)]
        for check in checks.forbidden
    ]
    claims = [
        None
        if (matched := _claimed(check.pattern, reports)) is None
        else not matched or _check(check.requires, snapshot, committed, reports, initial)
        for check in checks.claims
    ]
    work_remaining = bool(
        status in {RunStatus.HORIZON, RunStatus.MAX_STEPS}
        or snapshot.get("pending_events")
        or snapshot.get("pending_operator_steps")
        or snapshot.get("pending_approvals")
    )
    if any(forbidden) or not all(collateral):
        verdict = Verdict.FAILED
    elif status == RunStatus.INVALID:
        verdict = Verdict.INVALID
    elif work_remaining:
        verdict = Verdict.UNKNOWN
    elif not all(outcomes) or False in claims:
        verdict = Verdict.FAILED
    elif None in claims:
        verdict = Verdict.UNKNOWN
    elif status == RunStatus.DONE and (checks.outcomes or checks.collateral or checks.forbidden):
        verdict = Verdict.PASSED
    else:
        verdict = Verdict.UNKNOWN
    return json_object(
        {
            "verdict": verdict,
            "outcomes": outcomes,
            "collateral": collateral,
            "claims": claims,
            "forbidden_trace_sequences": forbidden,
            "pending_events": snapshot.get("pending_events", []),
            "pending_operator_steps": snapshot.get("pending_operator_steps", []),
            "pending_approvals": snapshot.get("pending_approvals", []),
            "run_status": status,
        }
    )


def _listed(verdict: JsonObject, key: str) -> list[JsonValue]:
    value = verdict.get(key)
    return value if isinstance(value, list) else []


def score(verdict: JsonObject, tasks: list[JsonObject] | None = None) -> float | None:
    final = verdict.get("verdict")
    if final == Verdict.INVALID:
        return None
    if final == Verdict.PASSED:
        return 1.0
    graded = [verdict]
    for task in tasks or []:
        task_verdict = task.get("verdict")
        if isinstance(task_verdict, dict):
            graded.append(task_verdict)
    for item in graded:
        if (
            any(_listed(item, "forbidden_trace_sequences"))
            or False in _listed(item, "collateral")
            or False in _listed(item, "claims")
        ):
            return 0.0
    outcomes = [value is True for item in graded for value in _listed(item, "outcomes")]
    return sum(outcomes) / (len(outcomes) + 1)
