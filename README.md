# Overnight

Overnight evaluates agents in a simulated world.

A scenario sets up an operator, the agent under test, other people and a set of services: mail, chat, calendar, shop and records. It gives each service its starting state and sets permissions, scripted events and faults. The agent works through those services over simulated time. Overnight then grades the outcome, forbidden actions and collateral damage from the final state and an append-only trace.

The agent can be a script, a plain command, or a native harness. A harness keeps its own agent loop. Overnight owns the world, the policy and the grading.

## Install

You need Python 3.14 and [uv](https://docs.astral.sh/uv/). From a checkout, install the `overnight` command:

```sh
uv tool install ".[acp]"
```

The examples below run from the checkout. The workbench UI also needs Node: `cd ui && npm install`. To work on Overnight itself, see [development](#development).

## Run a scripted example

Scripted examples call no model and need no account.

```sh
overnight validate examples/catering-chaos.json
overnight run examples/catering-chaos.json --out .local/runs/catering-chaos
overnight inspect .local/runs/catering-chaos --kind result --limit 5
```

`catering-chaos` uses five services. It has delayed replies, operator approvals, an outage and a retry, dependent events and protected unrelated data. `run` writes the resolved scenario, trace, final state, verdict and provenance into a new directory. It refuses a directory that already exists.

You can change the setup with `--overrides examples/catering-overrides.json`. You can add seeded noise and adversity with `--variation examples/catering-adversarial-variation.json`.

Several tasks can share one world. `examples/shared-world-chaos.json` runs three chats against one world with a scripted oracle. Its operator is an AI persona with a brief per task; here it replays authored answers instead of calling a model:

```sh
overnight run examples/shared-world-chaos.json \
  --operator-answers examples/shared-world-chaos-operator.jsonl \
  --out .local/runs/shared-world-chaos
```

See [shared-world episodes](docs/shared-world-episodes.md).

## Run a suite

A suite runs several agent setups (arms) on several scenarios (cases). It repeats attempts and reports paired confidence intervals.

```sh
overnight suite validate examples/suites/catering.json
overnight suite qa examples/suites/catering.json
overnight suite run examples/suites/catering.json --out .local/suites/catering
```

See [suites](docs/suites.md).

## Run the open-harness suite

The open-harness suite runs Pi, OpenCode and Prime on the same hard cases. Each harness runs in its own container. The container cannot read the scenario or its checks, and it can reach only the model provider.

1. Build the images from the repository root. Start with the base image:

   ```sh
   docker build -f containers/agent-base/Dockerfile -t overnight-agent-base .
   docker build -f connectors/pi/Dockerfile -t overnight-pi .
   ```

   OpenCode and Prime build from a checkout of their source. Their READMEs give the commands: [OpenCode](connectors/opencode/README.md#container), [Prime](connectors/prime/README.md#image).

2. Write the suite file. This step runs no model.

   ```sh
   uv run scripts/prepare_open_harness_suite.py --out .local/suites/open/suite.json
   overnight suite validate .local/suites/open/suite.json
   ```

   Every arm runs GPT-6 Luna through OpenRouter and reads `OPENROUTER_API_KEY`. To use another OpenAI-compatible endpoint, pass `--base-url`, `--api-key-env` (the name of the variable holding its key) and, if the endpoint needs one, `--reasoning-effort` ([model providers](docs/harness-integration.md#model-providers)). The operator persona of the live cases uses the same provider and model. `--arm` and `--case` pick a subset.

3. Run it. This calls a real model and spends money.

   ```sh
   overnight suite run .local/suites/open/suite.json --out .local/suites/open/out
   ```

See [harness integration](docs/harness-integration.md) for the cases, isolation and budget bounds.

## Workbench UI

```sh
overnight serve      # API on 127.0.0.1:4174
cd ui && npm run dev:web       # UI on http://localhost:5173
cd ui && npm run dev           # both at once
```

The workbench lists scenarios from `examples/` and saved runs from `.local/runs/`. It shows a run on a timeline with its checks and evidence. It edits scenarios, starts and stops scripted runs, and compares two runs at their first difference.

The API binds to 127.0.0.1 only. It refuses other Host headers and cross-site writes. Saves are validated before they are written. By default only scripted runs start; `serve --allow-live` also allows command and harness agents, which may call a live model. Stopping a run, or the server, ends the run and every agent session it started.

After changing an API response model, run `npm run gen:api` in `ui/` to regenerate the TypeScript types. `npm run check:api` verifies them.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | `passed` |
| 1 | `failed`: a check failed |
| 2 | `unknown`: a bound was reached or work is still pending |
| 3 | `invalid`: the scenario is invalid, or the run failed before it could be graded |
| 4 | Runtime or infrastructure error |

## Connectors

A native harness runs as `agent.kind: "harness"`. It talks to Overnight over one authenticated MCP tool server and a typed lifecycle protocol. Each connector is a separate package under `connectors/`.

| Connector | Native runtime | Notes |
| --- | --- | --- |
| [Pi](connectors/pi/README.md) | Pi over ACP | Runs in the `overnight-pi` image; no scheduler |
| [OpenCode](connectors/opencode/README.md) | Pinned OpenCode over ACP | Runs in the `overnight-opencode` image; native children, steering |
| [Prime](connectors/prime/README.md) | Prime Agent over ACP | Runs in the `overnight-prime` image; native jobs through a pinned evaluation-runtime patch |
| [Arden](connectors/arden/README.md) | Arden server and scheduler | Runs on the host; native Automations on the world clock |
| [Feather](connectors/feather/README.md) | Pinned Feather SDK | Runs on the host; `uv sync --extra feather` |

`scripts/` prepares harness cases without running a model. Native runs call real models and need provider keys in the environment. A harness runs in a container, or on the host only when its scenario sets `run_on_host: true`. See [isolation](docs/harness-integration.md#isolation).

## Architecture

- The operator is the user: it sends messages, answers the agent's reports, decides approvals and controls execution policy. Live cases play it with an AI persona and a private brief; examples without a model use a scripted one.
- The agent proposes operations through an adapter. The runner binds its identity.
- The world owns services, people, state, time, faults and events. Service handlers are pure and do no I/O.
- Adapters own subprocesses, transport and file I/O.
- One trace supports outcome, forbidden-action and collateral checks. Evaluator state never reaches the agent through the protocol, and a contained agent cannot read it from disk.

`src/overnight/` has five packages. Imports flow toward `domain`:

- `domain`: contracts and values
- `services`: typed service operations, state and handlers
- `world`: state, events, faults and access control
- `execution`: scenario setup, agent transport, operator policy, runtime and evaluation
- `adapters`: connector authoring, the ACP host, per-chat dispatch and the workbench API

`cli.py` composes them.

## Documentation

- [Scenario format](docs/scenario-format.md): scenario fields, events, policy, agent protocol, checks and verdicts
- [Services](docs/services.md): service kinds, operations and visibility
- [Suites](docs/suites.md): repeated runs, arm comparison and statistics
- [Run evidence](docs/evidence.md): artifacts, provenance and `overnight inspect`
- [Shared-world episodes](docs/shared-world-episodes.md): several tasks in one world
- [Scenario variations](docs/scenario-variations.md): seeded noise and authored adversity
- [Harness integration](docs/harness-integration.md): control protocol, capabilities and connectors
- [Adapter authoring](docs/adapter-authoring.md): writing a connector, steering
- [Test scope](docs/test-scope.md): what tests cover and how to run checks

## Development

```sh
uv sync --extra acp
.venv/bin/pytest -q
.venv/bin/ruff check src tests examples scripts connectors
.venv/bin/ruff format --check src tests examples scripts connectors
.venv/bin/ty check src/overnight
```

None of these call a model. See [test scope](docs/test-scope.md) for connector and UI checks, and [AGENTS.md](AGENTS.md) for contributor rules.

## License

Apache-2.0. See [LICENSE](LICENSE).
