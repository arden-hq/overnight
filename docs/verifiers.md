# verifiers environment

`integrations/verifiers` is an installable package, `overnight-verifiers`, that makes generated scenario families a [verifiers](https://github.com/PrimeIntellect-ai/verifiers) v1 environment. A trainer that consumes verifiers environments, such as prime-rl, loads it by its taskset id `overnight-verifiers`. Overnight keeps its runner, world, connectors, checks and score. verifiers supplies the model endpoint of each rollout, records the policy's calls and token ids on the rollout's trace, and talks to the trainer.

Core Overnight does not depend on verifiers, and the package does not import Overnight. Each rollout starts Overnight's [rollout worker](rollouts.md) as a subprocess with Overnight's own interpreter, named by `OVERNIGHT_PYTHON` (or the `python` option of the taskset and the harness).

## Python versions

Overnight needs Python 3.14. verifiers 0.4 supports Python 3.11 to 3.14 and pins `mcp==2.2.0`, which Overnight's `mcp<2` does not allow, so the two live in separate environments. The package requires Python 3.12 to 3.14 and `verifiers>=0.4.1,<0.5`; install it in the trainer's environment:

```sh
uv venv --python 3.14 integrations/verifiers/.venv
uv pip install --python integrations/verifiers/.venv/bin/python -e "integrations/verifiers[test]"
```

## Run

```sh
export OVERNIGHT_PYTHON=$PWD/.venv/bin/python
overnight generate order-resolution --count 200 --seed 0 --out .local/families/order-resolution
vf-run overnight-verifiers \
  --env.taskset.family .local/families/order-resolution \
  --env.agent.harness.agent agent.json \
  --env.operator.model <fixed operator model> \
  -m <policy model> --client.base-url <engine>/v1 --client.api-key-var <KEY_VARIABLE> \
  -r 8 -c 16
```

A trainer sets the same keys in its own config, for example prime-rl's `[[orchestrator.train.source]]` with `env.taskset.id = "overnight-verifiers"`, `env.taskset.family`, `env.agent.harness.agent` and `env.operator.model`. From Python, `load_environment(family, agent=...)` returns the env.

## Rows

`--env.taskset.family` is a folder written by `overnight generate` ([scenario families](scenario-families.md)). Loading it first runs Overnight's split check (`python -m overnight.families.split`, the same check as `scripts/check_split.py`) against `--env.taskset.held-out` folders, by default the repository's `examples`. When the family overlaps the held-out cases, loading fails and no row is produced. Each row carries the case's scenario, family and seed. Nothing else can be loaded: the hand-written examples and suites are never rows.

## Rollout

One verifiers rollout is one Overnight episode. The harness `overnight-verifiers` sends the worker one request:

| Request field | Value |
| --- | --- |
| `id` | The verifiers trace id; the episode runs in its own folder under `--env.agent.harness.root` (default: a new temporary folder per process), removed afterwards unless `keep` is set |
| `scenario` | The row's scenario |
| `agent` | The JSON document at `--env.agent.harness.agent`. It names the policy with `{policy.base_url}`, `{policy.api_key_env}` and `{policy.model}`, as in [rollouts](rollouts.md#request). Without it, the case's own scripted agent runs and no model is called |
| `policy` | The rollout's interception endpoint, reached through the harness's loopback forwarder (below), the variable `OVERNIGHT_POLICY_KEY` that holds the rollout's secret, and the rollout's model name |
| `operator_model` | For a persona case, the operator seat's endpoint and model, plus the bounds in `--env.agent.harness.operator-model` |
| `operator_key` | For a persona case, the operator seat's secret |
| `capture` | `false`: the trace is the record |

The worker starts with `--clean-env` ([environment](rollouts.md#environment)): its environment is the worker's allow list, the variables of the harness's `env` and `OVERNIGHT_POLICY_KEY`. Other variables of the trainer's environment, such as `HF_TOKEN` or `AWS_*`, reach neither the worker nor the agent. Overnight decides how the agent runs: on the host, or in its container. verifiers sees one host subprocess, so both seats use the `subprocess` runtime and the env refuses any other. verifiers never starts a harness itself.

The worker is stopped with an interrupt when verifiers cancels the rollout, for example at a timeout, so the runner removes the episode's processes and containers; what is still running 20 seconds later is killed.

## Reward

| Outcome | verifiers rollout |
| --- | --- |
| Graded, `failure` null | Reward `overnight_score` = the run's `score` |
| `failure.kind: agent`, including an exhausted operator budget ([rollouts](rollouts.md#reward)) | Reward 0, a normal completed rollout |
| verifiers' agent deadline (`--env.agent.timeout.rollout`) expires with every policy call answered and no engine fault recorded: stop `agent_timeout` | Reward 0: read as the policy being slow or looping |
| An engine fault recorded on any of the policy's calls (`trace.calls[].error`): a 5xx, 408 or 429, a client timeout or a transport error | The rollout errors, whatever the agent did next, even when its harness retried and went on |
| A policy call the harness got no answer to: it gave up on it, for example at its client's timeout, whether or not the engine answered later, or the call was still open when the episode ended or the agent's deadline cut it | The rollout errors |
| `failure.kind: infrastructure`, including any operator model failure, a worker `error`, a worker that exits without a result, or a rollout that ends without a result for another reason | The rollout errors |

An errored rollout has `has_error` set, and a trainer drops it, as prime-rl does; verifiers' retry rules (`--env.agent.retries`) can rerun it. An engine fault says nothing about the policy, so it never scores: an engine that restarts mid-step must not hand a group its advantages. A 4xx other than 408 and 429 answers the agent's own request, for example a prompt over the context length, so a harness that exits on it is an agent failure (0). The operator seat's engine faults reach Overnight's operator transport and fail the run as infrastructure. An engine call is recorded only when it ends, verifiers' client sets no read timeout by default (`--client.timeout.read`), and the interception server keeps a turn running when its client disconnects, so a retry can coalesce onto it: a call the harness gave up on that the engine answers later is recorded as answered. The trace therefore cannot show which calls the harness waited out. The agent reaches its interception endpoint through a forwarder the harness serves on the host's loopback for the rollout. It forwards every request without per-hop headers (the interception endpoint checks the rollout's secret itself) and counts each policy call (a `POST`) that is still open or whose client left before its whole answer was written. Any such call when the worker returns, or when the deadline cancels the rollout, drops it, so an engine that hangs never scores 0, and a harness that abandons a call on purpose drops its own failed rollout instead of scoring 0. Everything in the episode counts against the agent's deadline, including the operator's model, container start and grading, so a rollout timed out by any of them also scores 0; give the deadline room for them. Dropping on any recorded fault also drops rollouts whose harness recovered, and long episodes make more calls, so a flaky engine drops long episodes more often. A graded result without a score also errors instead of scoring 0. The metric `overnight_passed` is 1 when the verdict is `passed`, the strict pass beside the partial-credit score. `trace.info["overnight"]` keeps the episode id, scenario id, verdict, score, failure, the policy model name and the agent's and operator's usage.

## Operator

A persona case's operator is a second verifiers seat, `operator`, marked untrainable, as prime-envs' Tau environments mark their user simulator. For each persona episode the env opens an interaction on that seat for the whole episode and hands its endpoint and secret to Overnight as the `operator_model`. Overnight's operator transport calls it as it calls any operator model, so the operator's calls land on the operator's own trace, never on the agent's. A case with a scripted operator opens no operator seat.

The operator seat needs its model: `--env.operator.model`, and `--env.operator.client.*` for another endpoint. Loading the env without it fails, since verifiers would otherwise answer with the run's model, the policy under training. The env checks it when it loads, not the config, because `vf-run` builds a default config to read its options. The operator seat's traces carry no reward, so `vf-run`'s summary line, which averages every trace, shows the agent's mean reward diluted by them; read the agent seat's `overnight_score`. With the run's client, the operator is served by the trainer's engine under that model name, so name a model the engine serves frozen.

## Token record

With a training client (`--client.type train`), verifiers renders each request to token ids itself, calls the engine's `/inference/v1/generate`, and stores the prompt and sampled token ids, the loss mask and the logprobs on the trace's message nodes. The worker runs with capture off, so the agent's provider relay forwards each call unchanged, through the harness's forwarder, to the interception endpoint. Only Chat Completions calls produce tokens; verifiers refuses Responses and Anthropic Messages under a training client.

The relay's own capture ([rollouts](rollouts.md#capture-path)) stays for trainers that call the worker directly.

## Policy version

verifiers records the model name of every call (`trace.calls[].model`) and the agent's configured model, and Overnight's result names the policy model it was given (`policy_model`, also in `trace.info["overnight"]`). With a LoRA trainer that is the adapter name the trainer served. Token-level policy versions are the engine's: prime-rl's inference server labels sampled spans with `weight_versions` on the trace nodes, and its orchestrator masks tokens older than `max_staleness`. Overnight adds no version of its own.

## Concurrency

Each rollout starts its own worker process and episode folder, so rollouts in flight never share an episode. Their number is the trainer's in-flight bound: `-c` (`--max-concurrent`) for `vf-run`, which defaults to 128, and the orchestrator's concurrency band in prime-rl. Each episode costs a worker process, the agent's processes and, for a contained agent, a container with its own CPU and memory limits ([isolation](harness-integration.md#isolation)), so set the bound to what the host can run. `--env.max-concurrent-agents` stays 1: the operator seat holds no slot while the agent runs.

## Contained agents

The interception server and the harness's forwarder listen on the host's loopback. A contained agent cannot reach them: its only exit is the egress proxy, which allows HTTPS `CONNECT` to public hosts. For a training rollout whose policy endpoint is on loopback and whose agent sets `container`, the worker opens one more door, for that episode only ([isolation](harness-integration.md#isolation)): the proxy forwards its policy port to the forwarder's host port, and the agent's `{policy.base_url}` becomes `http://egress:3129/v1`. This works with Docker Desktop, where `host.docker.internal` reaches the host's loopback. On Linux, Docker's host gateway does not reach loopback, so contained rollouts there are unsupported.

The policy port is served by the egress proxy inside the agent's image. `overnight-agent-base` carries it; the Pi, OpenCode and Prime images must be rebuilt on that base before they can run contained training rollouts. Until then their proxy does not start, and each such rollout errors after the proxy's start timeout.

The secret in the agent's environment is the rollout's own. The operator's secret is sent on the worker's input (`operator_key`) and never enters an environment, so neither a contained agent nor a host agent can read it, including from its ancestors' environments. A host agent can still read the trainer's own environment and other rollouts' secrets ([environment](rollouts.md#environment)).

## Tests

`integrations/verifiers/test_contracts.py` runs against a fake engine with no live model: rows and the split check, a scripted case scoring 1.0 on two rollouts, the policy's calls with token ids on the trainable seat (needs the Qwen3-0.6B tokenizer in the Hugging Face cache), agent and infrastructure failures, an engine fault against a refused request, a policy call the engine never answered (given up by the harness or cut by the deadline), a timed-out rollout and an exhausted operator budget, the required operator model, and concurrent rollouts. With `OVERNIGHT_CONTAINER_TESTS=1` it also runs a contained agent against its interception session.

```sh
OVERNIGHT_PYTHON=$PWD/.venv/bin/python integrations/verifiers/.venv/bin/python -m pytest -q integrations/verifiers/test_contracts.py
```
