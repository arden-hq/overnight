import json

from overnight import cli
from overnight.execution.qa import AuthoredProbe, ProbeFile, check_scenario, check_suite
from overnight.families import generate, order_resolution


def _answers(tmp_path, case):
    path = tmp_path / f"{case.id}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in case.operator_answers))
    return path


def test_generated_cases_pass_qa_and_reject_their_wrong_agents(tmp_path):
    def first(refund, outage):
        facts = map(order_resolution.sample, range(100))
        return next(
            seed
            for seed, fact in enumerate(facts)
            if (fact.refund, fact.outage) == (refund, outage)
        )

    for seed in (first("full", True), first("partial", False)):
        case = generate(order_resolution.FAMILY, seed)
        assert case == generate(order_resolution.FAMILY, seed)
        report = check_scenario(
            case.id,
            case.scenario,
            operator_answers=_answers(tmp_path, case),
            authored=case.probes,
        )
        assert report.status == "ok", report
        wrong = {probe.probe: probe.verdict for probe in report.probes[-len(case.probes) :]}
        assert set(wrong.values()) == {"failed"}, wrong


def test_qa_reports_a_wrong_agent_that_passes(tmp_path):
    case = generate(order_resolution.FAMILY, 0)
    solver = AuthoredProbe(
        name="solves it", agent=case.scenario["agent"], operator_answers=case.operator_answers
    )
    report = check_scenario(
        case.id, case.scenario, operator_answers=_answers(tmp_path, case), authored=[solver]
    )
    assert report.status == "defect"
    assert not report.probes[-1].ok


def test_qa_reports_a_wrong_agent_whose_recorded_answers_no_longer_fit(tmp_path):
    case = generate(order_resolution.FAMILY, 3)
    probe = next(probe for probe in case.probes if len(probe.operator_answers) > 1)
    stale = probe.model_copy(update={"operator_answers": probe.operator_answers[:-1]})
    report = check_scenario(
        case.id, case.scenario, operator_answers=_answers(tmp_path, case), authored=[stale]
    )
    assert report.status == "defect"


def test_a_wrong_agent_may_do_one_task_of_an_episode_right():
    def send(subject):
        args = {"to": ["operator"], "subject": subject, "body": subject}
        call = {"service_id": "mail", "operation": "send_message", "args": args}
        return {"kind": "invoke", "id": f"send-{subject}", **call}

    def task(name):
        sent = [{"from": "agent", "subject": name}]
        outcome = {"op": "match", "path": ["services", "mail", "state", "messages"], "value": sent}
        return {"id": name, "chat_id": name, "message": name, "checks": {"outcomes": [outcome]}}

    def agent(*subjects):
        return {"kind": "scripted", "actions": [*map(send, subjects), {"kind": "done"}]}

    scenario = {
        "id": "two-tasks",
        "start_time": 0,
        "horizon": 20,
        "max_steps": 40,
        "tasks": [task("a"), task("b")],
        "actors": [
            {"id": "operator", "role": "operator"},
            {
                "id": "agent",
                "role": "agent",
                "read_services": ["mail"],
                "operations": {"mail": ["send_message"]},
            },
        ],
        "services": [{"id": "mail", "kind": "mail", "state": {"messages": []}}],
        "agent": agent("a", "b"),
        "operator": {"policy": {"default": "allow", "rules": []}, "steps": []},
        "checks": {},
    }
    only_a = AuthoredProbe(name="does only task a", agent=agent("a"))
    report = check_scenario("two-tasks", scenario, authored=[only_a])
    assert report.status == "ok", report


def test_suite_qa_proves_a_generated_case_with_its_answers_and_probes(tmp_path):
    case = generate(order_resolution.FAMILY, 0)
    (tmp_path / "case.json").write_text(json.dumps(case.scenario))
    (tmp_path / "probes.json").write_text(ProbeFile(probes=case.probes).model_dump_json())
    _answers(tmp_path, case).rename(tmp_path / "answers.jsonl")
    suite = {
        "id": "generated",
        "attempts": 1,
        "arms": [{"id": "authored"}],
        "cases": [
            {
                "id": case.id,
                "scenario": "case.json",
                "operator_answers": "answers.jsonl",
                "probes": "probes.json",
            }
        ],
    }
    (tmp_path / "suite.json").write_text(json.dumps(suite))
    [report] = check_suite(tmp_path / "suite.json")
    assert report.status == "ok", report
    assert [probe.probe for probe in report.probes[-len(case.probes) :]] == [
        probe.name for probe in case.probes
    ]


def test_generate_leaves_no_directory_when_a_write_fails(tmp_path, monkeypatch):
    written = []

    def fail_second(path, value):
        if written:
            raise OSError(28, "No space left on device")
        written.append(path)
        path.write_text(json.dumps(value))

    monkeypatch.setattr(cli, "_write_json", fail_second)
    out = tmp_path / "new" / "cases"
    assert cli.main(["generate", "order-resolution", "--out", str(out)]) == 1
    assert written and list(tmp_path.iterdir()) == []


def test_suite_validate_rejects_an_unreadable_qa_file(tmp_path):
    case = generate(order_resolution.FAMILY, 0)
    (tmp_path / "case.json").write_text(json.dumps(case.scenario))
    (tmp_path / "probes.json").write_text(json.dumps({"probes": "none"}))
    suite = {
        "id": "generated",
        "attempts": 1,
        "arms": [{"id": "authored"}],
        "cases": [{"id": case.id, "scenario": "case.json", "probes": "probes.json"}],
    }
    (tmp_path / "suite.json").write_text(json.dumps(suite))
    assert cli.main(["suite", "validate", str(tmp_path / "suite.json")]) == 3
    suite["cases"][0] |= {"probes": None, "operator_answers": "missing.jsonl"}
    (tmp_path / "suite.json").write_text(json.dumps(suite))
    assert cli.main(["suite", "validate", str(tmp_path / "suite.json")]) == 3
