import json
from pathlib import Path

import pytest

from overnight.domain.contracts import ScenarioError
from overnight.execution.adapters import ScriptedAdapter
from overnight.execution.runtime import run_scenario
from overnight.execution.scenario import resolve_scenario

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def scenario():
    return json.loads((EXAMPLES / "catering-chaos.json").read_text())


def test_catering_episode_recovers_without_collateral_or_private_observations():
    result = run_scenario(resolve_scenario(scenario()))
    assert result.verdict["verdict"] == "passed"
    assert result.final_state["time"] == 8
    assert result.final_state["results"]["cancel-first"]["status"] == "failed"
    assert result.final_state["results"]["cancel-retry"]["status"] == "succeeded"
    assert result.final_state["results"]["refund-mail-call"]["time"] == 7
    observations = [
        row["payload"]
        for row in result.trace
        if row["kind"] == "observation" and row["actor_id"] == "agent"
    ]
    assert observations
    assert "SECRET-" not in json.dumps(observations)
    assert any(row["kind"] == "operator_status" for row in result.trace)


def test_operator_denial_preserves_order_and_fails_outcome():
    document = scenario()
    for step in document["operator"]["steps"]:
        if step["kind"] == "approve":
            step["kind"] = "deny"
    result = run_scenario(resolve_scenario(document))
    assert result.verdict["verdict"] == "failed"
    assert result.final_state["services"]["shop"]["state"]["orders"][0]["status"] == "confirmed"


def test_resolved_overrides_do_not_mutate_base_and_same_seed_repeats_trace():
    document = scenario()
    overrides = json.loads((EXAMPLES / "catering-overrides.json").read_text())
    resolved = resolve_scenario(document, overrides)
    first = run_scenario(resolved)
    second = run_scenario(resolved)
    assert first.verdict["verdict"] == "passed"
    assert first.trace == second.trace
    assert first.final_state["services"]["shop"]["state"]["catalog"][0]["price_cents"] == 2500
    assert document["services"][3]["state"]["catalog"][0]["price_cents"] == 2000


def test_agent_cannot_supply_operator_identity():
    document = scenario()
    forged = {**document["agent"]["actions"][0], "actor_id": "operator"}
    result = run_scenario(resolve_scenario(document), ScriptedAdapter([forged]))
    assert result.verdict["rejected_actions"] >= 1
    assert result.final_state["services"]["chat"]["state"]["conversations"][0]["messages"] == []


def test_resolved_service_composition_survives_execution_and_caller_changes():
    from overnight.services.mail import Mail
    from overnight.services.shop import Shop

    document = scenario()
    mail = next(service for service in document["services"] if service["kind"] == "mail")
    mail["kind"] = "private-mail"
    from overnight.services.catalog import BUILTIN_SERVICES

    definitions = {**BUILTIN_SERVICES, "private-mail": Mail()}
    resolved = resolve_scenario(document, service_definitions=definitions)
    definitions["private-mail"] = Shop()
    result = run_scenario(resolved)
    assert result.verdict["verdict"] == "passed"
    assert result.final_state["services"][mail["id"]]["kind"] == "private-mail"


def test_service_composition_rejects_unknown_kinds_and_invalid_state():
    import pytest

    from overnight.domain.contracts import ScenarioError
    from overnight.services.catalog import BUILTIN_SERVICES

    document = scenario()
    document["services"][0]["kind"] = "unregistered"
    with pytest.raises(ScenarioError, match="unknown service kind"):
        resolve_scenario(document)
    document["services"][0]["kind"] = "private-mail"
    document["services"][0]["state"] = {"messages": [{"id": "invalid"}]}
    with pytest.raises(ScenarioError, match="validation errors"):
        resolve_scenario(
            document,
            service_definitions={**BUILTIN_SERVICES, "private-mail": BUILTIN_SERVICES["mail"]},
        )


def _renamed(document, prefix):
    actions = document["agent"]["actions"]
    names = {action["id"]: f"{prefix}{action['id']}" for action in actions if "id" in action}
    text = json.dumps(actions)
    for old, new in names.items():
        text = text.replace(f'"{old}"', f'"{new}"')
    return json.loads(text)


def test_catering_grades_what_happened_not_the_agent_invocation_ids():
    document = scenario()
    document["agent"]["actions"] = _renamed(document, "my-")
    assert run_scenario(resolve_scenario(document)).verdict["verdict"] == "passed"

    buy = {
        "kind": "invoke",
        "id": "spare",
        "service_id": "shop",
        "operation": "buy",
        "args": {"item_id": "meal"},
        "task_id": "catering",
        "chat_id": "main",
    }
    document["agent"]["actions"].insert(-2, buy)
    attempted = run_scenario(resolve_scenario(document)).verdict
    assert attempted["verdict"] == "failed"
    assert attempted["forbidden_trace_sequences"] != [[]]


def test_an_unsent_draft_does_not_count_as_the_coordinator_email():
    document = scenario()
    for action in document["agent"]["actions"]:
        if action.get("id") == "notify":
            action["operation"] = "save_draft"
    assert run_scenario(resolve_scenario(document)).verdict["verdict"] == "failed"


@pytest.mark.parametrize(
    "check",
    [
        {"op": "eq", "path": ["results", "notify", "status"], "value": "succeeded"},
        {"op": "match", "path": ["results"], "value": {"notify": {"status": "succeeded"}}},
        {"source": "trace", "op": "eq", "path": ["invocation_id"], "value": "notify"},
    ],
)
def test_checks_cannot_name_an_agent_invocation(check):
    document = scenario()
    document["checks"]["outcomes"].append(check)
    with pytest.raises(ScenarioError, match="no event makes"):
        resolve_scenario(document)


def test_events_cannot_read_an_agent_result():
    document = scenario()
    document["events"][0]["invocation"]["args"]["body"] = {
        "$ref": {"result": "ask-vendor", "path": ["id"]}
    }
    with pytest.raises(ScenarioError, match="no event makes"):
        resolve_scenario(document)
