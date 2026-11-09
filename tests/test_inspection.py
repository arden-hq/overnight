import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from click.testing import CliRunner

from overnight.cli import cli
from overnight.execution.inspection import (
    InspectionError,
    TraceQuery,
    build_trace_graph,
    inspect_trace,
    load_trace,
    query_trace,
)


def _entry(sequence: int, kind: str, **fields: object) -> dict[str, object]:
    return {
        "sequence": sequence,
        "time": fields.pop("time", sequence),
        "kind": kind,
        "payload": fields.pop("payload", {}),
        **fields,
    }


def _write_trace(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def test_query_validates_trace_and_combines_exact_filters(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            _entry(0, "invocation", actor_id="agent", task_id="one", invocation_id="call"),
            _entry(
                1,
                "result",
                actor_id="agent",
                task_id="one",
                invocation_id="call",
                payload={"status": "failed", "error": "offline"},
            ),
            _entry(2, "result", actor_id="vendor", task_id="two", invocation_id="other"),
        ],
    )

    records = load_trace(trace)
    result = query_trace(
        records,
        TraceQuery(task_id="one", actor_id="agent", kind="result", time_from=1, limit=1),
    )

    assert result.matched == 1
    assert result.records[0].line == 2
    payload = result.records[0].entry.payload
    assert isinstance(payload, Mapping)
    assert payload["status"] == "failed"


def test_malformed_trace_reports_line_and_does_not_skip(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(_entry(0, "first")) + "\n" + '{"sequence":"wrong"}\n', encoding="utf-8"
    )

    with pytest.raises(InspectionError, match=r"line 2"):
        load_trace(trace)


def test_graph_preserves_duplicate_rows_and_qualifies_links(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            _entry(0, "invocation", invocation_id="root"),
            _entry(1, "result", invocation_id="root"),
            _entry(2, "policy_check", invocation_id="child", cause_id="root"),
            _entry(3, "invocation", invocation_id="child", cause_id="root"),
            _entry(4, "result", invocation_id="child", cause_id="missing"),
            _entry(5, "invocation", invocation_id="duplicate"),
            _entry(6, "invocation", invocation_id="duplicate"),
            _entry(7, "result", invocation_id="duplicate"),
            _entry(8, "event", event_id="wake", invocation_id="wake-call"),
            _entry(9, "result", event_id="wake", invocation_id="wake-call"),
        ],
    )

    graph = build_trace_graph(load_trace(trace))

    assert any(
        link.predecessor_line == 1 and link.descendant_line == 3 and link.relation == "cause"
        for link in graph.links
    )
    assert any(issue.line == 5 and issue.reference == "missing" for issue in graph.issues)
    duplicate = next(issue for issue in graph.issues if issue.line == 8)
    assert duplicate.issue == "ambiguous"
    assert duplicate.candidate_lines == (6, 7)
    assert any(
        link.predecessor_line == 9 and link.descendant_line == 10 and link.relation == "event"
        for link in graph.links
    )
    assert len(graph.nodes) == 10


def test_graph_rejects_future_predecessor_and_handles_long_chains(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    chain = [
        _entry(index, "invocation", invocation_id=f"c{index}", cause_id=f"c{index - 1}")
        for index in range(2, 2000)
    ]
    _write_trace(
        trace,
        [
            _entry(0, "invocation", invocation_id="c0", cause_id="c1"),
            _entry(1, "invocation", invocation_id="c1", cause_id="c0"),
            *chain,
        ],
    )

    graph = build_trace_graph(load_trace(trace))

    assert [issue.line for issue in graph.issues] == [1]
    assert graph.issues[0].issue == "future"
    assert len(graph.links) == 1999
    assert all(link.predecessor_line < link.descendant_line for link in graph.links)


def test_policy_checks_do_not_require_later_execution_anchor(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            _entry(
                0,
                "policy_check",
                invocation_id="pending",
                payload={"decision": "pending"},
            ),
            _entry(
                1,
                "policy_check",
                invocation_id="denied",
                payload={"decision": "denied"},
            ),
            _entry(2, "invocation", invocation_id="pending"),
            _entry(3, "result", invocation_id="pending"),
        ],
    )

    graph = build_trace_graph(load_trace(trace))

    assert not any(issue.line in {1, 2} for issue in graph.issues)
    assert not any(link.descendant_line in {1, 2} for link in graph.links)
    assert all(link.predecessor_line < link.descendant_line for link in graph.links)


def test_report_summarizes_failures_policy_and_saved_pending_facts(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            _entry(
                0,
                "policy_check",
                invocation_id="ask",
                payload={"decision": "pending"},
            ),
            _entry(
                1,
                "policy_check",
                invocation_id="blocked",
                payload={"decision": "denied"},
            ),
            _entry(
                2,
                "result",
                invocation_id="failed",
                payload={"status": "failed", "error": "offline"},
            ),
        ],
    )

    report = inspect_trace(
        trace,
        TraceQuery(),
        verdict={
            "verdict": "unknown",
            "outcomes": [True, False],
            "pending_events": ["later"],
            "pending_approvals": ["ask"],
        },
        provenance={"backend": {"available": True}},
    )

    assert report.summary.outcomes == {"failed": 1}
    assert report.summary.failures[0].detail == "offline"
    assert report.summary.policy_denials[0].invocation_id == "blocked"
    assert report.summary.policy_pending[0].invocation_id == "ask"
    assert report.summary.pending_facts == {
        "pending_events": ["later"],
        "pending_approvals": ["ask"],
    }
    assert report.summary.evaluation_outcomes == [True, False]
    assert report.summary.provenance_available is True
    assert report.provenance == {"backend": {"available": True}}


def test_cli_accepts_run_folder_and_returns_json_without_traceback(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    _write_trace(run / "trace.jsonl", [_entry(0, "result", actor_id="agent")])
    (run / "verdict.json").write_text('{"verdict":"passed"}', encoding="utf-8")

    result = CliRunner().invoke(cli, ["inspect", str(run), "--actor", "agent"])

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["results"]["returned"] == 1
    assert output["summary"]["verdict"] == "passed"
    assert output["native_evidence"] == "separate and unsupported"

    (run / "trace.jsonl").write_text("not json\n", encoding="utf-8")
    failure = CliRunner().invoke(cli, ["inspect", str(run)])
    assert failure.exit_code == 1
    assert "line 1" in failure.output
    assert "Traceback" not in failure.output
