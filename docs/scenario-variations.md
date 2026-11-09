# Scenario variations

`overnight.execution.variations` adds bounded noise and authored adversity to an
existing `Scenario`. It compiles to the same `Event`, `Invocation`, and `FaultSpec`
objects used by ordinary scenarios. The world keeps one event queue and one fault
engine.

```python
from pathlib import Path

from overnight.execution.scenario import load_json, resolve_scenario
from overnight.execution.variations import VariationConfig, apply_variation

base = resolve_scenario(load_json(Path("examples/catering-chaos.json"))).config
config = VariationConfig.model_validate(
    load_json(Path("examples/catering-adversarial-variation.json"))
)
varied = apply_variation(base, config)
resolved = resolve_scenario(varied.model_dump(mode="json", by_alias=True, exclude_unset=True))
```

The final `resolve_scenario` call is required. It validates actor role, service and
operation targets, event arguments, and cross-references. The world checks the
event actor's operation grants at dispatch; an unauthorized event produces a denied
result without mutating service state. Variation authoring does not expand an
actor's grants, change execution policy, or change outcome and collateral checks.

## Format

A variation has an ID, deterministic seed, inclusive `start` and `end`, and three
bounded lists:

- `background` schedules sample explicitly authored event candidates. Each schedule
  emits 1–64 events; one variation emits at most 128. Candidates are ordinary
  `Event` values, but scheduling owns their time and generated IDs, so candidates
  cannot set `at`, `dependencies`, or `delay`. Conditions and invocation payloads
  remain authored data.
- `adversarial_events` contains complete ordinary events. Authors write the exact
  misleading, stale, or conflicting service payload; Overnight does not infer
  intent or generate text.
- `outage_faults` contains ordinary bounded `FaultSpec` values. Existing fault
  matching, windows, probability, and remaining-count behavior applies.

Background times and candidate choices use a private random generator derived from
the variation seed and schedule ID. Results repeat for the same inputs and do not
consume application-global random state. Generated IDs use
`variation:<variation-id>:<schedule-id>:<index>:event|invocation`; any collision
with existing or authored IDs is rejected. Existing events and faults stay first
and unchanged.

The variation window, authored event times, and outage windows must fit the base
scenario horizon. `apply_variation` reconstructs the scenario before appending, so
the caller's scenario is unchanged. Unknown targets go through normal scenario
resolution; the variation transform has no permission validator of its own.

[`catering-adversarial-variation.json`](../examples/catering-adversarial-variation.json)
combines background support traffic, stale and misleading vendor messages, and
temporary chat/shop outages against the existing catering scenario. Its scheduled
activity falls within the scripted base run's exercised window.

## CLI

```sh
overnight validate examples/catering-chaos.json --variation examples/catering-adversarial-variation.json
overnight run examples/catering-chaos.json --variation examples/catering-adversarial-variation.json --out .local/runs/varied-catering
```

The CLI resolves overrides first, compiles the variation, and resolves the final
scenario again. It saves the validated variation as `variation.json` beside the
compiled `resolved-scenario.json`. Generated schedules are available in the
compiled scenario; a run ending earlier does not prove every future event ran.
