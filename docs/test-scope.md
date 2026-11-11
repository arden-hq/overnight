# Test scope

Tests protect the contracts whose failure would make an evaluation wrong or unsafe. Each risk below has one primary proof; add another test only for a distinct risk or a reproduced failure.

- Permissions and approvals: grants, policy revisions, exact approval binding, fail-closed denial.
- Privacy and isolation: checks, personas and evaluator state never reach observations; per-chat native state stays private.
- Wire boundaries: agent actions, command and harness protocols, MCP identity, steering correlation.
- Atomic state and data loss: failed calls never commit; existing evidence is never overwritten.
- Deterministic scheduling and grading: event ordering, dependencies, faults, variations, verdicts.
- Reproduced failures: a regression test for each real bug, at the owning contract.

Prefer representative shared-schema cases over per-field, per-default or per-example inventories. Do not test generic Pydantic or library mechanics, repeat a shared validator for every field that uses it, pin example-file shapes, or assert diagnostic wording. Fewer tests do not prove more; a deletion must leave the risk covered.

## Where tests live

| Area | Tests |
| --- | --- |
| World, services, policy | `tests/test_world.py`, `tests/test_services.py`, `tests/test_schema_agreement.py` |
| Runner, scenarios, grading | `tests/test_runtime.py`, `tests/test_integration.py`, `tests/test_evidence.py`, `tests/test_episode.py`, `tests/test_variations.py`, `tests/test_families.py` |
| Transports and harnesses | `tests/test_live_adapter.py`, `tests/test_harness_boundary.py`, `tests/test_adapter_authoring.py`, `tests/test_steering_transport.py`, `tests/test_feather.py`, `tests/test_provider_relay.py` |
| Suites and statistics | `tests/test_suite.py` |
| Training rollouts | `tests/test_rollout.py`; each trainer package in its own environment: `integrations/verifiers/test_contracts.py` ([verifiers](verifiers.md#tests)), `integrations/tinker/test_contracts.py` ([Tinker](tinker.md#tests)), `integrations/openenv/test_contracts.py` ([OpenEnv](openenv.md#tests)) |
| Evidence and workbench | `tests/test_provenance.py`, `tests/test_inspection.py`, `tests/test_workbench.py` |
| Connectors | `connectors/<name>/test_contracts.py`, `connectors/feather/test_scrub.py`, `connectors/arden/*.test.mjs` |
| Container isolation | `tests/test_container.py` (opt-in, see below) |

## Run

The connector tests import the connector packages, and Feather's also needs its extra. Install them once:

```sh
uv sync --extra acp --extra feather
uv pip install --python .venv/bin/python --no-deps -e ./connectors/opencode -e ./connectors/prime -e ./connectors/pi -e ./connectors/feather
```

Then run the checks with `.venv/bin`, since a later plain `uv sync` removes the editable connectors:

```sh
.venv/bin/pytest -q tests connectors/opencode/test_contracts.py connectors/prime/test_contracts.py connectors/pi/test_contracts.py connectors/feather/test_scrub.py
(cd connectors/arden && node --test)
.venv/bin/ruff check src tests examples scripts connectors integrations
.venv/bin/ruff format --check src tests examples scripts connectors integrations
.venv/bin/ty check src/overnight
(cd ui && npm run build && npm run check:api)
```

Each trainer package needs its own environment: verifiers pins an MCP version Overnight does not allow ([verifiers](verifiers.md#python-versions)), and tinker-cookbook needs torch. Its tests start Overnight's rollout worker from `OVERNIGHT_PYTHON`, against a fake engine or sampling client; the verifiers and Tinker suites need the Qwen3-0.6B tokenizer in the Hugging Face cache.

```sh
for package in verifiers tinker openenv; do
  uv venv --python 3.14 integrations/$package/.venv
  uv pip install --python integrations/$package/.venv/bin/python -e "integrations/$package[test]"
done
for package in verifiers tinker openenv; do
  OVERNIGHT_PYTHON=$PWD/.venv/bin/python integrations/$package/.venv/bin/python -m pytest -q integrations/$package/test_contracts.py
done
```

`tests/test_container.py` starts real Docker containers, so it is skipped unless `OVERNIGHT_CONTAINER_TESTS=1` is set and the `overnight-agent-base` image exists. Its leftover checks cover only containers and networks the test process started, so other Overnight runs on the host do not affect it. One test also needs internet access to reach its allowed host.

```sh
OVERNIGHT_CONTAINER_TESTS=1 .venv/bin/pytest -q tests/test_container.py
```

None of these call a model. Live model or native-harness runs are separate and need explicit authorization.
