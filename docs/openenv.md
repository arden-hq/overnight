# OpenEnv and TRL

`integrations/openenv` is an installable package, `overnight-openenv`, that runs generated scenario families as OpenEnv loop-owning sessions for TRL's async GRPO trainer (`HarnessAsyncGRPOTrainer` in `trl.experimental.async_grpo.openenv_harness`). Overnight keeps its runner, world, connectors, checks and score. OpenEnv's capture proxy records the policy's calls with the engine's token ids and logprobs, outside the agent. TRL samples from its vLLM engine, trains on the captured calls and syncs weights back into that engine.

Core Overnight does not depend on OpenEnv or TRL, and the package does not import Overnight. Each episode starts Overnight's [rollout worker](rollouts.md) as a subprocess with Overnight's own interpreter, named by `OVERNIGHT_PYTHON` (or the factory's `python`).

## Install

The package requires Python 3.12 to 3.14 and `openenv>=0.12,<0.13`. Install it in the trainer's environment, next to TRL:

```sh
uv venv --python 3.14 integrations/openenv/.venv
uv pip install --python integrations/openenv/.venv/bin/python -e "integrations/openenv[test]"
```

TRL's async GRPO trainer needs `trl` with its experimental async GRPO module, `torch`, `transformers`, `datasets` and a vLLM server; install them in that environment for training. The adapter itself imports only OpenEnv.

## Run

Serve the policy with token ids and processed logprobs, as TRL's Harbor example does:

```sh
VLLM_SERVER_DEV_MODE=1 vllm serve <model> --port 8000 \
  --enable-auto-tool-choice --tool-call-parser <parser> \
  --logprobs-mode processed_logprobs --return-tokens-as-token-ids \
  --weight-transfer-config '{"backend":"nccl"}'
```

To train a LoRA adapter, also set `VLLM_ALLOW_RUNTIME_LORA_UPDATING=1` and pass `--enable-lora --max-lora-rank` (at least the adapter's rank, from vLLM's allowed values) and `--max-loras` (at least `max_staleness + 2`), as in [TRL's LoRA notes](https://huggingface.co/docs/trl/async_grpo_trainer#lora). Each weight sync then loads the adapter from disk instead of over NCCL. Start the server with its own environment's `bin` on `PATH`: vLLM's FlashInfer sampler builds its kernel with `ninja` on the first request that samples with TRL's policy, and an engine that cannot find it dies.

A host agent document is the same in every episode, so give an ACP connector a relative `evidence` path, such as `"native"`: it is resolved inside the episode's own agent folder ([run evidence](evidence.md)). A fixed absolute path is refused from the second episode on, as a connector that could not start.

Then build the trainer with a factory over a generated family:

```python
from functools import partial
from datasets import Dataset
from overnight_openenv import OvernightSessionFactory
from trl.experimental.async_grpo import AsyncGRPOConfig
from trl.experimental.async_grpo.openenv_harness import HarnessAsyncGRPOTrainer

factory = partial(
    OvernightSessionFactory,
    ".local/families/order-resolution",
    llm_url="http://127.0.0.1:8000",
    model="<model>",
    agent="agent.json",
    operator_model={
        "provider": {"base_url": "<operator endpoint>/v1", "api_key_env": "OPERATOR_KEY"},
        "model": "<fixed operator model>",
    },
)
trainer = HarnessAsyncGRPOTrainer(
    model="<model>",
    harness_session_factory=factory,
    train_dataset=Dataset.from_list(factory().prompt_rows()),
    args=AsyncGRPOConfig(
        vllm_server_base_url="http://127.0.0.1:8000", top_p=1.0, top_k=-1, num_generations=8, ...
    ),
)
trainer.train()
```

`OVERNIGHT_PYTHON` names Overnight's interpreter, and the operator's key variable must be set. TRL pickles the factory into its rollout process and calls it there with `sampling=`, its full-vocabulary policy at the trainer's temperature. With a LoRA, TRL sets the factory's `model` to the current adapter name before each session, so an episode is served by one adapter.

## Cases

`family` is a folder written by `overnight generate` ([scenario families](scenario-families.md)). The first use runs Overnight's split check (`python -m overnight.families.split`) against the `held_out` folders, by default the repository's `examples`; when the family overlaps them, no case loads. `prompt_rows()` gives one dataset row per case, with the case id as the prompt. TRL passes that prompt to `create`, which reads the case back by its id; a prompt that names no case of the family, a hand-written case included, fails the session. The agent never sees the prompt: Overnight's runner sends it the case's own messages.

## Episode

`create` mints a capture session on the proxy and returns an `OvernightSession`. `wait_for_completion` starts one rollout worker and sends it one request:

| Request field | Value |
| --- | --- |
| `id` | TRL's rollout id; the episode runs in its own folder under `root` (default: a new temporary folder per process), removed afterwards unless `keep` is set |
| `scenario` | The case's scenario |
| `agent` | The factory's `agent` document. It must name `{policy.base_url}` and `{policy.api_key_env}`, and usually `{policy.model}`, as in [rollouts](rollouts.md#request) |
| `policy` | The proxy's URL, the variable `OVERNIGHT_POLICY_KEY` that holds the capture session id, and the factory's `model` |
| `operator_model`, `operator_key` | For a persona case, the factory's `operator_model` and its key |
| `capture` | `false`: the proxy is the record |

The capture session id is the agent's API key: the proxy files every call under it, and refuses a key it did not mint. The proxy runs in the trainer's rollout process on a loopback port, with its session routes closed by a random admin key. It rewrites each call's model to the session's model, pins the trainer's sampling policy (`temperature`, `top_p=1`, no truncation), asks the engine for token ids and logprobs, and refuses constrained sampling (`tool_choice` that forces a tool, grammars, `response_format`) under a training policy. `max_model_calls` adds the proxy's per-episode call budget beside the connector's own request cap ([rollouts](rollouts.md#reward)); past it, the proxy answers a stop message that is never recorded. The engine is probed once per engine and model; an engine that cannot return token ids fails every session.

## Reward

| Outcome | TRL rollout |
| --- | --- |
| Graded, `failure` null, with captured calls | `wait_for_completion` returns 0; reward = the run's `score` |
| `failure.kind: agent`, including an exhausted operator budget | Returns 1; reward 0 |
| The episode passes `agent_timeout_s` (default 1800, setup and grading included) with every policy call answered | The worker is interrupted; reward 0. Raises `TimeoutError`, TRL's timed-out path, when the capture is trainable, and returns 1 otherwise |
| Any policy call failed at the engine (5xx, 429, timeout, transport error, a lost sampling policy), whatever the agent did next | Raises `OvernightError`: unscorable |
| A policy call still unanswered when the episode ended: the harness gave up on it, or the deadline cut it | Unscorable |
| A graded episode whose capture TRL cannot train on, for example with a fatal capture finding | Unscorable |
| `failure.kind: infrastructure`, a worker `error`, a worker that exits without a result, an ungraded result, or a graded episode with no call through the proxy | Unscorable |

A return of 1 is TRL's failed agent: when its capture cannot be trained on, for example because the agent failed before calling the policy, TRL drops the rollout instead of stopping training. TRL raises `CaptureContractError`, which stops training, for a capture it cannot train on from a session that returned 0 or timed out. The session reads its capture as TRL does before it answers, so a graded episode with such a capture, for example a fatal capture finding (tokens or logprobs that fail validation, or a reply the proxy could not ingest), is unscorable instead, and a timed-out one returns 1. With the timed-out path, TRL passes `timed_out` to a `rollout_reward_fn`. An unscorable rollout has no reward and is left out of its group's baseline; TRL waits before the next session and stops after 10 × `max_concurrent_rollouts` unscorable sessions in a row. `verify()` returns the reward as `env_reward`, with Overnight's result (episode, verdict, score, failure, policy model, usage) in `artifacts["overnight"]` and `overnight_passed` in `metrics`.

The proxy counts every failed engine answer as a fault, including a 400, 413 or 422 that answers the agent's own request; those episodes are dropped, not scored 0, so a policy that sends malformed requests is never penalized for them, and many in a row stop training. A prompt over the context length is not a fault: the proxy answers it with its stop message. The proxy counts each session's requests still being answered and those whose client disconnected before the answer was sent. A call the harness gave up on while the engine still worked, for example at its relay's timeout while vLLM pauses for a weight sync, counts even when the engine answers later, and a call the deadline cuts is open when the deadline passes; either makes the episode unscorable, since the engine, not the agent, had the turn. So an agent scores 0 for time only when it ran out of time with every policy call answered. OpenEnv caches an engine probe that failed as eval-only, so one failed probe makes every later session on that engine and model unscorable until the process restarts.

## Token record

`fetch_training_trace` exports the capture session and returns OpenEnv's `TrainingTrace`: for each agent call, the engine's prompt ids, sampled ids, logprobs and a loss mask over prompt and completion. Calls are linked by token prefix; retries are discarded, and toolless side calls (titles, summaries) are left out when the agent's calls carry tools. Nothing is re-tokenized. The proxy accepts Chat Completions, Responses, Anthropic Messages and Google generateContent and sends Chat Completions to the engine.

## Operator

`operator_model` is required, as Overnight's `OperatorModel`: a fixed model that answers as a persona case's operator, never the policy under training. Overnight's operator transport calls its endpoint directly, so its calls never reach the proxy or the trained trace. Its key is read from its variable (`OPENROUTER_API_KEY` for the `openrouter` provider) and sent on the worker's input, never in the worker's environment ([secrets](#secrets) for what a host agent can still read).

## Secrets

The worker starts with `--clean-env` ([environment](rollouts.md#environment)): its environment is the worker's allow list, the factory's `pass_env` names and `OVERNIGHT_POLICY_KEY`. The operator's and the engine's keys and any other trainer secret are not in the worker's or the agent's environment. The engine's key (`engine_api_key_env`) is sent only by the proxy. A host agent runs as the same user, so it can still read the environment the trainer process started with (`ps eww`), including the operator's and the engine's keys and any token the trainer holds, and every other episode's worker and agent environment, which holds that episode's capture session id. With another episode's session id a host agent can add calls to that episode's trained record. Keep real keys out of a trainer that runs host agents, for example with a local operator engine that accepts any key, or run the agent in its container, whose environment is an allowlist and whose processes are its own; a contained agent's key adds calls only to its own record.

## Concurrency

Each session has its own capture key, worker process and episode folder, so episodes in flight never share a record. TRL runs `max_concurrent_rollouts` sessions at once on threads of its rollout process, all on one proxy. Each episode costs a worker, the agent's processes and, for a contained agent, a container ([isolation](harness-integration.md#isolation)); set the bound to what the host can run. When TRL stops, it closes live sessions: the worker is interrupted, and what is still running 20 seconds later is killed.

## Contained agents

The proxy listens on the host's loopback. For an agent that sets `container`, the rollout worker forwards that one port through the agent's egress proxy and gives the agent `http://egress:3129/v1` ([verifiers](verifiers.md#contained-agents) describes the same path and its limits: Docker Desktop only, and images rebuilt on `overnight-agent-base`).

## Tests

`integrations/openenv/test_contracts.py` runs against a fake vLLM-style engine with no live model, GPU or TRL install. Its `trl_rollout` follows the loop-owning branch of TRL's `_HarnessRolloutLoop._run_session`. It covers: cases only after the split check, and no hand-written case; the required operator model; a captured call with token ids, logprobs and mask, the score as reward, the pinned sampling policy, no secret in the agent's environment and no operator call on the policy's engine; agent failures scoring 0 or dropped, never a contract error; an engine fault after the harness recovered; a timed-out episode scoring 0 on TRL's timed-out path, and an episode whose harness gave up on a call the engine still worked on being unscorable; a graded episode with a fatal capture finding being unscorable; and four concurrent episodes keeping their own calls.

```sh
OVERNIGHT_PYTHON=$PWD/.venv/bin/python integrations/openenv/.venv/bin/python -m pytest -q integrations/openenv/test_contracts.py
```

Not yet run: a contained agent, a native harness other than Pi (Prime, Feather, OpenCode) through the proxy, and a LoRA adapter swap during an episode.
