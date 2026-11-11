import pytest
from pydantic import ValidationError

from overnight.domain.contracts import Checks, JsonObject
from overnight.execution.evaluation import evaluate
from overnight.execution.scenario import resolve_scenario


def test_resolved_checks_make_legacy_evidence_sources_explicit():
    scenario = {
        "id": "sources",
        "start_time": 0,
        "horizon": 1,
        "max_steps": 1,
        "actors": [
            {"id": "operator", "role": "operator"},
            {"id": "agent", "role": "agent"},
        ],
        "services": [{"id": "mail", "kind": "mail", "state": {"messages": []}}],
        "agent": {"kind": "scripted", "actions": [{"kind": "done"}]},
        "operator": {"policy": {"default": "deny", "rules": []}, "steps": []},
        "checks": {
            "outcomes": [{"op": "exists", "path": ["services", "mail"]}],
            "forbidden": [{"match": {"kind": "invocation"}}],
        },
    }

    checks = resolve_scenario(scenario).to_dict()["checks"]

    assert checks["outcomes"][0]["source"] == "state"
    assert checks["forbidden"][0]["source"] == "trace"


@pytest.mark.parametrize(
    "check",
    [
        {"source": "trace", "op": "unchanged", "path": ["kind"]},
        {"source": "agent_report", "op": "unchanged", "path": ["message"]},
        {"source": "other", "op": "exists", "path": ["kind"]},
        {"source": "state", "op": "before", "first": {"kind": "a"}, "then": {"kind": "b"}},
    ],
)
def test_checks_reject_unsupported_evidence_sources(check):
    with pytest.raises(ValidationError):
        Checks.model_validate({"outcomes": [check]})


def test_forbidden_checks_reject_non_trace_sources():
    with pytest.raises(ValidationError):
        Checks.model_validate(
            {"forbidden": [{"source": "agent_report", "match": {"kind": "agent_report"}}]}
        )


def test_trace_checks_select_paths_within_trace_entries():
    checks = {
        "outcomes": [
            {"source": "trace", "op": "eq", "path": ["payload", "operation"], "value": "send"},
            {"source": "trace", "op": "exists", "path": ["actor_id"]},
        ],
    }
    trace = [
        {"sequence": 0, "kind": "invocation", "actor_id": "agent", "payload": {"operation": "send"}}
    ]

    result = evaluate(checks, {}, trace, "done")

    assert result["outcomes"] == [True, True]
    assert result["verdict"] == "passed"


def test_agent_report_checks_use_only_canonical_report_payloads():
    checks = {
        "outcomes": [{"source": "agent_report", "op": "eq", "path": ["message"], "value": "done"}],
    }
    unrelated_trace = [
        {"sequence": 0, "kind": "result", "payload": {"message": "done"}},
        {"sequence": 1, "kind": "agent_report", "payload": {"message": "done"}},
    ]

    assert evaluate(checks, {}, unrelated_trace, "done")["outcomes"] == [False]
    report = {"sequence": 2, "kind": "agent_report", "payload": {"time": 4, "message": "done"}}
    assert evaluate(checks, {}, [*unrelated_trace, report], "done")["outcomes"] == [True]


def test_forbidden_trace_partial_matching_remains_authoritative():
    checks = {"forbidden": [{"match": {"kind": "invocation", "payload": {"operation": "buy"}}}]}
    trace = [{"sequence": 7, "kind": "invocation", "payload": {"operation": "buy", "args": {}}}]

    result = evaluate(checks, {}, trace, "done")

    assert result["forbidden_trace_sequences"] == [[7]]
    assert result["verdict"] == "failed"


def test_trace_outcomes_ignore_denied_attempts_that_forbidden_checks_still_see():
    checks = {
        "outcomes": [
            {"source": "trace", "op": "eq", "path": ["payload", "operation"], "value": "send"}
        ],
        "forbidden": [{"match": {"payload": {"operation": "send"}}}],
    }
    attempt = {"invocation_id": "s1", "payload": {"operation": "send"}}
    trace = [
        {"sequence": 0, "kind": "policy_check", **attempt},
        {"sequence": 1, "kind": "invocation", **attempt},
        {"sequence": 2, "kind": "result", "invocation_id": "s1", "payload": {"status": "denied"}},
        {"sequence": 3, "kind": "agent_observation", "payload": {"operation": "send"}},
    ]

    result = evaluate(checks, {}, trace, "done")

    assert result["outcomes"] == [False]
    assert result["forbidden_trace_sequences"] == [[0, 1, 3]]
    succeeded = {**trace[2], "payload": {"status": "succeeded"}}
    assert evaluate(checks, {}, [*trace[:2], succeeded], "done")["outcomes"] == [True]


def test_an_order_check_needs_the_first_effect_before_the_earliest_second_one():
    def call(sequence: int, operation: str, status: str = "succeeded") -> list[JsonObject]:
        invocation_id = f"call-{sequence}"
        return [
            {
                "sequence": sequence,
                "kind": "invocation",
                "invocation_id": invocation_id,
                "payload": {"operation": operation},
            },
            {
                "sequence": sequence + 1,
                "kind": "result",
                "invocation_id": invocation_id,
                "payload": {"status": status},
            },
        ]

    def approve_then_pay(trace: list[JsonObject]) -> bool:
        check = {
            "source": "trace",
            "op": "before",
            "first": {"kind": "invocation", "payload": {"operation": "approve"}},
            "then": {"kind": "invocation", "payload": {"operation": "pay"}},
        }
        return evaluate({"outcomes": [check]}, {}, trace, "done")["outcomes"] == [True]

    assert approve_then_pay([*call(0, "approve"), *call(2, "pay")])
    assert not approve_then_pay([*call(0, "pay"), *call(2, "approve")])
    # A later, properly ordered payment does not excuse the early one.
    assert not approve_then_pay([*call(0, "pay"), *call(2, "approve"), *call(4, "pay")])
    assert not approve_then_pay(call(0, "approve"))
    assert not approve_then_pay(call(0, "pay"))
    # Like every outcome trace check, it counts only calls that succeeded.
    assert not approve_then_pay([*call(0, "approve", "denied"), *call(2, "pay")])
    assert approve_then_pay([*call(0, "pay", "denied"), *call(2, "approve"), *call(4, "pay")])


def test_match_finds_list_members_wherever_they_sit():
    checks = {
        "outcomes": [
            {"op": "match", "path": ["mail"], "value": [{"to": ["boss"], "draft": False}]}
        ],
        "forbidden": [{"match": {"payload": {"args": {"to": ["courier"]}}}}],
    }
    snapshot = {
        "mail": [{"to": ["boss", "ops"], "draft": True}, {"to": ["ops", "boss"], "draft": False}]
    }
    trace = [
        {"sequence": 4, "kind": "policy_check", "payload": {"args": {"to": ["boss", "courier"]}}}
    ]

    result = evaluate(checks, snapshot, trace, "done")

    assert result["outcomes"] == [True]
    assert result["forbidden_trace_sequences"] == [[4]]
    drafts_only = {"mail": [{"to": ["boss"], "draft": True}]}
    assert evaluate(checks, drafts_only, [], "done")["outcomes"] == [False]


def test_a_denied_reuse_of_a_succeeded_id_is_not_outcome_evidence():
    checks = {
        "outcomes": [
            {"source": "trace", "op": "eq", "path": ["payload", "operation"], "value": "send"}
        ]
    }
    trace = [
        {
            "sequence": 0,
            "kind": "policy_check",
            "invocation_id": "x",
            "payload": {"decision": "allowed", "operation": "list"},
        },
        {"sequence": 1, "kind": "result", "invocation_id": "x", "payload": {"status": "succeeded"}},
        {
            "sequence": 2,
            "kind": "policy_check",
            "invocation_id": "x",
            "payload": {"decision": "denied", "operation": "send"},
        },
    ]

    assert evaluate(checks, {}, trace, "done")["outcomes"] == [False]
