# Overnight

Overnight evaluates agents in a simulated workplace. A scenario defines people, permissions, starting data, timed events, and faults across mail, chat, calendar, shop, and records. The evaluator checks the final state and trace for task outcomes, forbidden actions, and collateral changes.

The agent can be a script, a command, or a native harness. Native harnesses keep their own agent loop. Overnight owns the world, clock, and grading.

## Run a scripted example

Install Python 3.14 and [uv](https://docs.astral.sh/uv/). From this checkout:

```sh
uv tool install ".[acp]"
overnight validate examples/catering-chaos.json
overnight run examples/catering-chaos.json --out .local/runs/catering-chaos
overnight inspect .local/runs/catering-chaos --kind result --limit 5
```

Catering exercises an approval, a delayed vendor reply, and a retry after an outage. It runs without an account or a model. Use a fresh output directory for each run. The directory contains the resolved scenario, trace, final state, verdict, and provenance. [Run evidence](docs/evidence.md)

To replay a persona operator without calling a model, use the recorded answers for the [three-day offsite](docs/harness-integration.md#replay-the-offsite) or [shared-world episode](docs/shared-world-episodes.md#examples).

## Compare agents in a suite

```sh
overnight suite qa examples/suites/catering.json
overnight suite run examples/suites/catering.json --out .local/suites/catering
```

QA checks that the oracle can solve the case and that agents which skip the work fail its checks. A suite runs each agent setup against each case and reports scores and, with enough cases, paired confidence intervals. [Suites and scoring](docs/suites.md)

The [open-harness suite](docs/harness-integration.md#run-the-open-harness-suite) runs Pi, OpenCode, and Prime in containers. Running it requires a provider key and calls live models. The [connector guide](docs/harness-integration.md#connectors) covers all five supported harnesses, including Arden and Feather.

## Open the workbench

With Node.js 22.12 or newer, run:

```sh
cd ui
npm ci
npm run dev
```

Open [localhost:5173](http://localhost:5173) to edit scenarios, start scripted runs, and inspect or compare timelines. The workbench binds its API to loopback. For command, harness, or persona runs, start the API with `overnight serve --allow-live` and the UI with `npm run dev:web`. Replay recorded operator answers from the CLI and open the saved run.

## Reference

- [Scenario format and exit codes](docs/scenario-format.md), [services](docs/services.md), and [scenario variations](docs/scenario-variations.md)
- [Generated case families](docs/scenario-families.md) and [adapter authoring](docs/adapter-authoring.md)
- [Training rollouts](docs/rollouts.md), [Verifiers](docs/verifiers.md), [OpenEnv and TRL](docs/openenv.md), and [Tinker](docs/tinker.md)

## Develop

```sh
uv sync --extra acp
.venv/bin/pytest -q
.venv/bin/ruff check src tests examples scripts connectors integrations
.venv/bin/ruff format --check src tests examples scripts connectors integrations
.venv/bin/ty check src/overnight
```

The [test guide](docs/test-scope.md) lists the connector, trainer, container, and UI checks. After changing a workbench API model, run `npm run gen:api` in `ui/` and verify the generated schema with `npm run check:api`.

[Apache-2.0](LICENSE)
