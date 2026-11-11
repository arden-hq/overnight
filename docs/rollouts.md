# Training rollouts

`overnight.adapters.rollout` runs episodes for an outside trainer. The trainer sends a case and the endpoint of the model it trains. The worker runs the episode through the ordinary runner and an unchanged connector, with token capture on unless the request turns it off. It returns the run's `score` (the same score a suite reports) and every model call the agent's processes captured. The trainer stays outside Overnight.

Three trainers have a package that starts the worker for them. Each package starts one worker per episode with Overnight's interpreter, sends it one request with capture off, and records the policy's calls at an endpoint the trainer owns, which the request names as the policy:

| Trainer | Package | Policy endpoint and record |
| --- | --- | --- |
| A [verifiers](verifiers.md) consumer, such as prime-rl | `overnight-verifiers` | verifiers' interception endpoint for the rollout; the rollout's trace holds the calls and token ids |
| tinker-cookbook ([Tinker](tinker.md)) | `overnight-tinker` | A capture proxy per episode in the trainer's process; each call is sampled by the cookbook's policy and becomes one transition |
| TRL's async GRPO trainer ([OpenEnv](openenv.md)) | `overnight-openenv` | OpenEnv's capture proxy in the trainer's rollout process; it records each call with the engine's token ids and logprobs |

Another trainer calls the worker directly and either uses the worker's capture or records at its own endpoint.

```sh
.venv/bin/python -m overnight.adapters.rollout --root .local/rollouts < requests.jsonl
```

The worker is long-lived. It reads one JSON request per line and writes one JSON result per line, in order. A request that cannot run, for example an invalid request or a scenario that does not resolve, gives `{"id", "error"}` with no score or captures, and the worker goes on; treat it like an infrastructure failure. Each episode runs in a new directory under `--root`, which is removed afterwards unless `--keep` is set. Episodes run one at a time, so start several workers to run episodes in parallel. The process keeps its caches, such as the operation schemas, from one episode to the next. From Python, use `RolloutWorker(root).rollout(RolloutRequest(...))`.

## Environment

`--clean-env` restarts the worker with an allow list of its environment: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `TERM`, `TZ`, `DOCKER_HOST`, `DOCKER_CONTEXT` and `DOCKER_CONFIG` (`WORKER_ENVIRONMENT`), plus each name given with `--pass-env NAME`. The three trainer packages start the worker with it and pass the policy's key variable and the names their config lists (`pass_env`, or the verifiers harness's `env`), so a trainer's API keys, tokens and cloud credentials are not in the worker's environment or a host agent's. The restart replaces the environment the worker started with, which `ps eww` shows to processes of the same user. Pass network settings the episode needs the same way, for example `HTTPS_PROXY`, `NO_PROXY` or `SSL_CERT_FILE` on a host behind a proxy. Without `--clean-env` the worker keeps the environment it was given.

A host agent runs as the trainer's user, so the allow list is no boundary for it. It can still read, for example with `ps eww`, the environment any process of that user started with: the trainer's own, with every key the trainer was started with, and every other episode's worker and agent, each with its episode's policy key. With another episode's key a host agent can call that episode's policy endpoint and add calls to its record. A contained agent sees only its container's processes and its allow-listed environment ([isolation](harness-integration.md#isolation)). Keep real keys out of a trainer that runs host agents, or contain the agent.

## Request

| Field | Meaning |
| --- | --- |
| `id` | Episode id and directory name. Default: a random id |
| `scenario` | The scenario document, for example a generated [family case](scenario-families.md) |
| `overrides`, `variation` | Optional, as in a suite case |
| `agent` | Optional agent that replaces the case's own, as a suite arm's `agent` does |
| `policy` | `{"provider": {"base_url", "api_key_env", "reasoning_effort"?}, "model"}`: the trained model's OpenAI-compatible endpoint |
| `operator_model` | Optional [operator model](scenario-format.md#persona) that replaces a persona case's own |
| `operator_answers` | Optional recorded `operator_answer` entries to replay instead |
| `operator_key` | Optional key for `operator_model`, used instead of its `api_key_env` variable. It never enters an environment |
| `capture` | Default `true`. `false` runs the agent without token capture, for a trainer that records the policy's calls at its own endpoint; `captures` is then empty and no `CaptureMissing` failure is added |

The worker never reads harness options. The policy reaches the agent only through tokens in the agent's strings: `{policy.base_url}`, `{policy.api_key_env}` and `{policy.model}`. Write them where the connector takes its provider and model, for example inside the `--config-json` of an ACP connector:

```json
"options": {"provider": {"base_url": "{policy.base_url}", "api_key_env": "{policy.api_key_env}"}, "model": "{policy.model}", ...}
```

A request is refused when `policy` is given and the agent names no token, or when the agent names a token and `policy` is missing, so an episode never runs on a model nobody chose. Token values may not contain quotes, backslashes, spaces or braces, and the tokens are replaced in one pass. The policy's key variable must be set in the worker's environment. The operator model's key comes from `operator_key` or from its variable. A variable is removed from the worker's environment for the episode, so a host agent does not inherit it, but the environment the worker started with stays readable to processes of the same user, for example with `ps eww` on the worker's process; a variable the policy also names stays. `operator_key`, sent on the worker's input, keeps the operator's key out of every environment of the episode; the trainer packages send it so. A local engine such as vLLM accepts any key. An invalid request's error never repeats the request's values.

A persona case needs an operator. Pass `operator_model`, a live model that answers as the operator; a fixed local model is the usual choice ([suites](suites.md#fixed-operator-for-training)). Recorded answers fit only an agent that takes the recorded path. A trained policy leaves that path, and the replay then fails the run. A case with a scripted operator needs neither.

## Result

| Field | Meaning |
| --- | --- |
| `id`, `scenario_id` | The episode and its scenario |
| `verdict` | `passed`, `failed`, `unknown` or `invalid` |
| `score` | 0 to 1, or null for an invalid run ([scoring](suites.md#scoring)) |
| `failure` | Null, or `{kind, type, message}` when the episode is not a usable graded run. `kind` is `agent` or `infrastructure` |
| `policy_model` | The `policy.model` of the request, or null: the model or adapter name the trainer served this episode under |
| `captures` | One entry per agent process that captured calls: `source`, its capture file's path in the agent folder, and `calls`, its `CapturedCall` records in answer order |
| `provenance` | The run's provenance, including the operator model that actually answered |
| `usage`, `operator_usage` | The agent's and the operator's spend |

## Reward

A trainer reads `failure` before `score`:

| `failure` | Meaning | Reward |
| --- | --- | --- |
| null | A graded run | `score` |
| `kind: agent` | The agent under training caused the failure, for example its harness exited, sent a reply that is not a JSON action, broke the protocol, or asked the operator past its `max_calls` or `max_cost_usd` (`OperatorBudgetExceeded`). The verdict is `invalid` and `score` is null | 0 |
| `kind: infrastructure` | Anything else: the world, the runner, a container, a connector that could not start (a harness that ended or timed out before it answered `ready`), a captured policy call the engine did not answer, or captures the worker could not use | Drop the episode |

`kind` is the run's own failure kind, the one a suite uses to report `agent_failed` apart from `invalid` ([scoring](suites.md#scoring)), with one exception: the run records an exhausted operator budget as infrastructure, so a suite reports it `invalid`, and the worker reports it as the agent's. When an agent failure comes first, a later infrastructure failure replaces it; the first infrastructure failure, an exhausted operator budget included, is kept. The worker adds an infrastructure failure of its own when a capture file holds a line that is neither a `CapturedCall` nor an `UnansweredCall`, when it records an `UnansweredCall` (`PolicyCallUnanswered`, below), or when the agent's metered usage counts model calls but nothing was captured, for example because its connector does not pass `run_capture()` (Arden's does not). Usage that is unmeasured cannot show the latter. The `score` of such an episode is the run's grade, not a reward. An agent that can write its capture file can therefore turn its own failure into a dropped episode by corrupting the file.

How a harness's own failure is classified is the connector's choice: a refusal of the relay's own, such as its request cap (429, `native request budget exhausted`), reaches the harness, and a harness that exits on it is an agent failure; one that ends its turn on it gets a graded run. Each connector caps an episode's model requests: `max_requests` for Pi, Prime and OpenCode (default 12, at most 40; the open-harness suite uses 40), and `--max-requests` for Feather (default 12, at most 40). A request past the cap is refused, so set the cap in the agent document to the longest episode the trainer wants; the default of 12 is short for a multi-step case.

With capture, the relay records every policy call the engine did not answer with a usable completion as an `UnansweredCall` in the capture file ([token capture](harness-integration.md#token-capture)): an engine error status (any 4xx or 5xx), a call that failed, timed out or that the harness abandoned before the engine answered, a success without a completion, a completion without token ids (`capture_missing`), and a Responses request, which capture cannot serve (`capture_api`). The worker then drops the episode (`kind: infrastructure`, type `PolicyCallUnanswered`), whatever the harness did next: exited, ended its turn, or retried. This replaces an agent failure, and an earlier infrastructure failure of the run is kept. It holds for every connector that passes `run_capture()` to its relay. Without capture the worker does not see the trainer's engine: an engine fault that makes the harness exit is an agent failure there, and one the harness answers by ending its turn is a graded run. The trainer sees its engine's calls and drops those episodes itself. The three trainer packages apply one rule to time and engine faults: an episode in which a policy call failed at the engine, or was still unanswered when the episode ended (the harness gave up on it, or the deadline cut it), is dropped, whatever the agent did next. An agent that ran out of time with every policy call answered scores 0. A request the endpoint refuses as the agent's own (a 4xx) scores 0 under verifiers and drops the episode under Tinker and OpenEnv, whose proxies count it as a refusal or an engine fault ([verifiers](verifiers.md#reward), [Tinker](tinker.md#reward), [OpenEnv](openenv.md#reward)).

A process that runs again, for example after a process reset, or another chat in a [shared-world episode](shared-world-episodes.md) gets its own entry. A scripted agent makes no calls, so its `captures` is empty. An agent with model calls and no captures gets an infrastructure failure (see [reward](#reward)).

## Capture path

The worker calls `run_scenario(..., capture=True)`. Only then does the runner set `OVERNIGHT_CAPTURE_PATH` for each process agent and list the host file it assigned in `RunResult.capture_paths`, in start order. The host file is the `.capture.jsonl` path beside the process's provenance sidecar, for example `connector-generation-0.capture.jsonl` at the top of the agent folder. A host process writes there directly. A contained process writes to `/overnight/capture/calls.jsonl`, its own mount outside the agent folder, and the runner copies it to the host file when the container ends. The worker reads only the listed files, so a `*.capture.jsonl` the agent writes elsewhere is never read. The runner drops the variable from any other run's environment, so an evaluation never captures. Each connector passes `run_capture()` to its provider relay, which stores the calls there and takes the variable out of the connector's environment ([token capture](harness-integration.md#token-capture)). Capture needs a harness or persistent command agent. A one-shot command agent is refused.

Capture serves Chat Completions only. Pi, Prime and Feather use it. OpenCode with a configured endpoint uses the Responses API, so the relay refuses each of its calls under capture (`capture_api`) and the worker drops the episode.

A host run can read the scenario and its checks ([isolation](harness-integration.md#isolation)), so a policy trained on host rollouts can learn to read them. A contained agent reaches no local endpoint, because the egress proxy allows only public hosts, with one exception for training: when `policy.base_url` is on the host's loopback (`127.0.0.1`, `localhost` or `::1`, with a port) and the request's `agent` sets `container`, the worker forwards that one host port through the agent's egress proxy and gives the agent `http://egress:3129<path>` as `{policy.base_url}` ([policy port](harness-integration.md#isolation)). Without a port the request is refused. It can also rewrite its own capture file, which a host process can always reach. In a container the capture file is outside the agent's folder and working directory, but the agent's tools run as the same user as the relay, so a shell that looks for `/overnight/capture` can still rewrite it. Captured tokens are trustworthy for an agent that does not target them, not proof against one that does.
