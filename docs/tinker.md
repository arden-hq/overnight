# Tinker environment

`integrations/tinker` is an installable package, `overnight-tinker`, that makes generated scenario families RL environments for [tinker-cookbook](https://github.com/thinking-machines-lab/tinker-cookbook)'s training loop. Overnight keeps its runner, world, connectors, checks and score. tinker-cookbook samples the policy, records each action's tokens and logprobs, and trains.

Core Overnight does not depend on tinker-cookbook, and the package does not import Overnight. Each episode starts Overnight's [rollout worker](rollouts.md) as a subprocess with Overnight's own interpreter, named by `OVERNIGHT_PYTHON` (or the dataset builder's `python`).

## Install

tinker-cookbook 0.6 supports Python 3.11 and later and needs torch, so the package lives in its own environment:

```sh
uv venv --python 3.14 integrations/tinker/.venv
uv pip install --python integrations/tinker/.venv/bin/python -e "integrations/tinker[test]"
```

## Run

```sh
export OVERNIGHT_PYTHON=$PWD/.venv/bin/python TINKER_API_KEY=...
overnight generate order-resolution --count 200 --seed 0 --out .local/families/order-resolution
integrations/tinker/.venv/bin/python -m overnight_tinker.train \
  family=.local/families/order-resolution agent=agent.json operator_model=operator.json \
  model_name=Qwen/Qwen3-8B log_path=.local/tinker/run-1
```

`overnight_tinker.train` builds a cookbook `rl.train.Config` and calls `rl.train.main`. Its options are `chz` fields: `group_size`, `groups_per_batch`, `learning_rate`, `lora_rank`, `max_tokens`, `temperature`, `renderer_name`, `held_out`, `rollout_timeout_seconds` (default 1800), `sampling_turn_timeout_seconds`, `pass_env`, `root`, `keep`, `max_steps` and the logging options. A trainer with its own config uses `OvernightDatasetBuilder` as its `dataset_builder` instead.

`agent.json` is an Overnight agent document that names the policy with `{policy.base_url}`, `{policy.api_key_env}` and `{policy.model}`, as in [rollouts](rollouts.md#request). `operator.json` is an [operator model](scenario-format.md#persona) with a compatible provider, for example `{"provider": {"base_url": "http://127.0.0.1:8001/v1", "api_key_env": "OPERATOR_KEY"}, "model": "<fixed operator model>"}`.

## Cases

`family` is a folder written by `overnight generate` ([scenario families](scenario-families.md)). Building the dataset first runs Overnight's split check (`python -m overnight.families.split`, the same check as `scripts/check_split.py`) against the `held_out` folders, by default the repository's `examples`. When the family overlaps the held-out cases, building fails and no case loads. Only the family's own scenario files load: the hand-written examples and suites are never training cases. Each case becomes one `EnvGroupBuilder` with `group_size` episodes; a batch holds `groups_per_batch` cases.

## Episode

One cookbook `Env` is one Overnight episode, and the agent's harness drives it. When the cookbook asks the env for its first observation, the env starts:

- tinker-cookbook's capture proxy app (`tinker_cookbook.capture.proxy.app.make_app`) on `127.0.0.1` at a free port, with a secret of its own as its auth token. It renders each chat request to tokens with the run's renderer (`renderer_name`, by default the model's recommended one).
- one rollout worker, sent one request: the case's scenario, `agent.json`, the policy `{"provider": {"base_url": "http://127.0.0.1:<port>/v1", "api_key_env": "OVERNIGHT_POLICY_KEY"}, "model": <model_name>}`, `capture: false`, and for a persona case `operator_model` with `operator_key`.

The proxy's sampling client is the env itself. A harness call becomes the env's next observation (the rendered prompt, with the renderer's stop condition and the call's own stop strings), the cookbook samples it with the step's policy, and the env's `step` answers the waiting call with those tokens. So each policy call is one `Transition` sampled by the trainer, and the trajectory has the policy's calls in the order the harness made them. Calls the harness makes at once are answered one at a time. The trainer's `max_tokens` and `temperature` apply; the harness's own are ignored.

When the worker returns, the env's last step ends the episode with its reward. An episode in which the policy made no call (a case's scripted agent, or a harness that failed first) ends at its first observation with the cookbook's `InitialObservationOverflow`: no training tokens, but its reward counts in the group.

## Reward

| Outcome | Cookbook result |
| --- | --- |
| Graded, `failure` null | Reward = the run's `score`, on the last transition; metric `overnight/passed` is 1 for a `passed` verdict |
| `failure.kind: agent`, including an exhausted operator budget ([rollouts](rollouts.md#reward)) | Reward 0, metric `overnight/agent_failure` 1 |
| `rollout_timeout_seconds` expires while the env waits for the harness, every policy call answered | Reward 0, metric `stop/rollout_timeout`; the trajectory keeps its calls |
| The deadline passes while the sampler works on a call | The env raises `OvernightError` when the action arrives |
| One sampling call runs past `sampling_turn_timeout_seconds` (default: the rollout timeout) | The cookbook raises `SamplingTurnTimeoutError` |
| The policy's sampling raises (an engine fault, timeout or lost connection, or a prompt over the context window) | The trajectory fails in the cookbook's loop |
| The proxy refused or failed any harness call, or the harness gave up on a call before the sampler answered it | The env raises `OvernightError` |
| `failure.kind: infrastructure`, a worker `error`, a worker that exits without a result, or a graded run without a score | The env raises `OvernightError` |

A failed trajectory is dropped by the run's rollout strategy. `overnight_tinker.train` sets `rollout_error_tolerance=MinViableGroup()`: it retries a failed episode while fewer than three quarters of the group have survived (at most 3 retries), keeps a group with at least three quarters, and drops it otherwise. With the cookbook's default for most models, `FailFast`, one dropped episode stops the run, so a trainer with its own config sets a tolerant strategy. A proxy refusal drops the episode because it says nothing about the policy: the proxy refuses fields it cannot serve faithfully (penalties, `response_format`, `logprobs`, forced `tool_choice`, `n` other than 1, and stop strings under a renderer with token-id stops, such as Qwen3's), so a harness that sends them drops every episode. A prompt over the model's context window fails in the policy's sampling and is dropped, never scored, so a policy whose episodes outgrow the window loses them from training instead of scoring 0; size `max_tokens` and the case length to the window.

The env, not the cookbook, keeps the episode's deadline, so the episode scores 0 for time only when the agent had the turn: the env waited for the harness's next call with every call answered. A deadline that falls while the sampler works, and a call the harness gave up on while it was queued or sampled (its client disconnected, which cancels the call), say nothing about the policy and drop the episode; the sampled action of such a call never reaches a trajectory. The deadline counts everything else in the episode, including the operator's model, container start and grading; give it room for them. The worker and its harness are stopped when the env's own wait fails or is cancelled, and otherwise at the latest at the group's cleanup; a trajectory that failed in the policy's sampling holds its worker until then.

## Operator

`operator_model` is required: the dataset builder refuses to build without it, or when the variable named by its provider's `api_key_env` is unset in the trainer's environment. The operator is a fixed model at its own endpoint. Overnight's operator transport calls it directly, so its calls never pass the proxy and never enter a trajectory. The key is read in the trainer's process and sent on the worker's input (`operator_key`), never in an environment.

## Token record

The trajectory is the cookbook's own record: each transition holds the prompt tokens the proxy rendered, and the sampled tokens with their logprobs from the trainer's policy. The cookbook's `trajectory_to_data` merges consecutive calls into one datum when a prompt extends the previous prompt and action, and starts a new datum otherwise. Whether a harness's calls extend each other depends on the renderer; Qwen3's default renderer drops earlier reasoning from history, so each call becomes its own datum.

The tokens are the proxy's rendering of the chat the harness sent, with the run's renderer, not the harness's own template or provider's. Calls the harness makes through the policy endpoint for itself, such as a title or a summary, are policy calls and enter the trajectory. Overnight's relay capture is off; the worker's `captures` is empty.

A rollout uses one sampling client from start to end, so an episode never mixes policy versions. In async training the cookbook tags each group with the step that sampled it and bounds how stale it may be.

## Concurrency and secrets

Each episode has its own proxy, port, secret, worker process and episode folder under `root` (default: a new temporary folder per process), so episodes in flight never share calls. A call to another episode's port without its secret is refused. Concurrency is the cookbook's: `groups_per_batch` groups of `group_size` episodes run at once in synchronous training, and up to `groups_per_batch * (max_steps_off_policy + 1)` groups in async training. Each episode costs a worker, the agent's processes and, for a contained agent, a container, so size these to the host.

The worker starts with `--clean-env` ([environment](rollouts.md#environment)): its environment is the worker's allow list, the names in `pass_env`, and `OVERNIGHT_POLICY_KEY`, the episode's proxy secret. `TINKER_API_KEY`, the operator's key and other trainer secrets are not in the worker's or the agent's environment. A host agent runs as the trainer's user, so it can still read the trainer's environment and other episodes' workers' environments (for example with `ps eww`), and with another episode's secret call that episode's proxy; host agents are no boundary ([isolation](harness-integration.md#isolation)). A contained agent sees only its own episode's secret.

## Contained agents

The proxy listens on the host's loopback with a port, so a contained agent reaches it through the worker's forwarded policy port ([verifiers](verifiers.md#contained-agents) describes the same path), with the same limits: Docker Desktop only, and agent images rebuilt on `overnight-agent-base`.

## Unproven

Synchronous training on the Tinker service has run with Pi on the host through the proxy, multi-call episodes included; Pi's requests pass the proxy unrefused. Not yet run: Feather or Prime through the proxy, a contained agent, and async training. A harness that streams with features the proxy does not serve, or sends stop strings, is expected to drop its episodes until its agent document turns them off.

When each policy call is its own datum, as under Qwen3's and Qwen3.5's default renderers (see [token record](#token-record)), and the harness resends its growing history with each call, the trained tokens grow with the square of an episode's calls: a long episode's prompt is trained once per call. Bound the episode's calls with the connector's request cap.

## Tests

`integrations/tinker/test_contracts.py` runs the cookbook's own group rollout entry point (`do_group_rollout_and_filter_constant_reward`) with a fake sampling client, a fake operator engine and a process agent that calls the policy through Overnight's provider relay: the split check and the required operator, two concurrent episodes whose sampled tokens and logprobs form their trajectories and reach training data with the score as reward, an agent failure scoring 0, an unstartable agent, a proxy refusal and an engine fault dropping their groups, a call the harness gave up on and a deadline during sampling dropping theirs while an agent out of time with its calls answered scores 0, and a scripted case scoring without tokens. It needs the Qwen3-0.6B tokenizer in the Hugging Face cache.

```sh
OVERNIGHT_PYTHON=$PWD/.venv/bin/python integrations/tinker/.venv/bin/python -m pytest -q integrations/tinker/test_contracts.py
```
