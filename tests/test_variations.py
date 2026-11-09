import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from overnight.domain.contracts import Event, FaultSpec, Invocation
from overnight.execution.scenario import construct_world, load_json, resolve_scenario
from overnight.execution.variations import (
    BackgroundSchedule,
    VariationConfig,
    apply_variation,
)

ROOT = Path(__file__).parents[1]


@pytest.fixture
def scenario():
    return resolve_scenario(load_json(ROOT / "examples/catering-chaos.json")).config


def candidate(body: str = "Routine support update") -> Event:
    return Event(
        id="template-event",
        invocation=Invocation(
            id="template-call",
            actor_id="vendor",
            service_id="chat",
            operation="send_message",
            args={"conversation_id": "support", "body": body},
        ),
    )


def config(**updates) -> VariationConfig:
    values = {
        "id": "noise",
        "seed": 17,
        "start": 2,
        "end": 12,
        "background": [
            BackgroundSchedule(id="chat", count=5, candidates=[candidate(), candidate("FYI")])
        ],
    }
    values.update(updates)
    return VariationConfig.model_validate(values)


def test_seeded_background_is_stable_bounded_and_does_not_mutate(scenario):
    before = scenario.model_dump(mode="json")
    random.seed(991)
    expected_global = random.random()
    random.seed(991)

    first = apply_variation(scenario, config())
    second = apply_variation(scenario, config())

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert scenario.model_dump(mode="json") == before
    assert random.random() == expected_global
    generated = first.events[len(scenario.events) :]
    assert len(generated) == 5
    assert all(2 <= event.at <= 12 for event in generated if event.at is not None)
    assert len({event.id for event in first.events}) == len(first.events)
    assert len({event.invocation.id for event in first.events}) == len(first.events)
    resolve_scenario(first.model_dump(mode="json", by_alias=True))


def test_authored_events_and_faults_append_without_changing_rules(scenario):
    adversarial = candidate("Stale policy: refunds are unavailable").model_copy(
        update={"id": "stale", "at": 8}
    )
    adversarial = adversarial.model_copy(
        update={"invocation": adversarial.invocation.model_copy(update={"id": "stale-call"})}
    )
    outage = FaultSpec(
        id="read-outage",
        service_id="chat",
        operation="read_messages",
        start=5,
        end=9,
        error="temporary outage",
    )

    varied = apply_variation(
        scenario,
        config(background=[], adversarial_events=[adversarial], outage_faults=[outage]),
    )

    assert varied.events[:-1] == scenario.events
    assert varied.faults[:-1] == scenario.faults
    assert varied.operator == scenario.operator
    assert varied.checks == scenario.checks
    assert varied.actors == scenario.actors


def test_existing_resolver_rejects_bad_generated_targets(scenario):
    bad = candidate().model_copy(
        update={"invocation": candidate().invocation.model_copy(update={"service_id": "missing"})}
    )
    varied = apply_variation(
        scenario,
        config(background=[BackgroundSchedule(id="bad", count=1, candidates=[bad])]),
    )

    with pytest.raises(ValueError, match="unknown service or operation"):
        resolve_scenario(varied.model_dump(mode="json", by_alias=True))


def test_unauthorized_authored_event_is_denied_without_state_mutation(scenario):
    unauthorized = Event(
        id="unauthorized-message",
        invocation=Invocation(
            id="unauthorized-message-call",
            actor_id="courier",
            service_id="chat",
            operation="send_message",
            args={"conversation_id": "support", "body": "Untrusted update"},
        ),
        at=2,
    )
    varied = apply_variation(
        scenario,
        config(background=[], adversarial_events=[unauthorized]),
    )
    resolved = resolve_scenario(varied.model_dump(mode="json", by_alias=True))
    world = construct_world(
        resolved.config,
        service_definitions=resolved.service_definitions,
    )
    before = world.snapshot_for_evaluator()["services"]

    results = world.advance(2)

    assert [(result.id, result.status) for result in results] == [
        ("unauthorized-message-call", "denied")
    ]
    assert world.snapshot_for_evaluator()["services"] == before


def test_rejects_ambiguous_templates_collisions_and_out_of_horizon(scenario):
    with pytest.raises(ValidationError, match="cannot set dependencies"):
        BackgroundSchedule(
            id="bad",
            count=1,
            candidates=[candidate().model_copy(update={"dependencies": ("prior",)})],
        )

    collision = candidate().model_copy(
        update={
            "id": scenario.events[0].id,
            "invocation": candidate().invocation.model_copy(update={"id": "unique-call"}),
            "at": 8,
        }
    )
    with pytest.raises(ValueError, match="event ID collision"):
        apply_variation(scenario, config(background=[], adversarial_events=[collision]))

    with pytest.raises(ValueError, match="variation window outside"):
        apply_variation(scenario, config(start=0, end=scenario.horizon + 1))

    late_fault = FaultSpec(
        id="late",
        service_id="chat",
        operation="read_messages",
        start=scenario.horizon + 1,
    )
    with pytest.raises(ValueError, match="outage start outside"):
        apply_variation(scenario, config(background=[], outage_faults=[late_fault]))


def test_command_agent_omitted_timeout_survives_variation_resolution(scenario):
    payload = scenario.model_dump(mode="json", by_alias=True, exclude_unset=True)
    payload["agent"] = {"kind": "persistent_command", "command": ["python", "agent.py"]}
    command_scenario = resolve_scenario(payload).config
    varied = apply_variation(command_scenario, config())
    resolved = resolve_scenario(varied.model_dump(mode="json", by_alias=True, exclude_unset=True))
    assert resolved.config.agent.kind == "persistent_command"
