# Harness integration

A harness agent (`agent.kind: "harness"`) keeps its own native agent loop: conversation, tools, children and scheduling. Overnight keeps the world, actor binding, execution policy, event queue and grading. The two sides meet at two contracts: a control protocol for the session lifecycle and one authenticated MCP server for world tools. To write a new connector, see [adapter authoring](adapter-authoring.md).

## Owners

| Boundary | Owner |
| --- | --- |
| Catalog-generated world tools, authenticated MCP transport | `execution/tool_server.py` (official MCP SDK; no state resources) |
| Typed control protocol and capability checks | `execution/harness.py` over the persistent JSONL transport |
| Shared ACP host, per-chat dispatch and receipts | `adapters/` |
| MCP bridge for non-Python consumers | `execution/tool_sidecar.py` |
| Native launch, tool binding and lifecycle | The connector under `connectors/<name>/` |

Every MCP call returns to the runner for authorization and dispatch; the tool server never grants authority. Trusted run, tool-call and chat IDs travel outside model-authored arguments. Identical arguments do not identify a call or reuse another call's approval.

## Control protocol

The runner sends `{"kind":"hello","version":1,"required":{...}}`; the connector answers `{"kind":"ready","version":1,"capabilities":{...}}`. A scenario lists what it needs in `agent.required`:

| Requirement | Meaning |
| --- | --- |
| `simulated_clock` (default true) | The harness reports native wakes and follows world time |
| `chat_isolation` (default true) | Native state is private to one Overnight chat |
| `cancel` (default true) | Pending native work can be retired |
| `reset_scope` | `none`, `process`, `chat` or `external` reset support |
| `steering` | Active-turn steering ([control](adapter-authoring.md#active-turn-control)) |

A missing capability fails before any world event or operator message.

Each observe request carries the bound `chat_id` and filtered observation. The response carries the action plus `status: {busy, pending, next_wake, delivered}`. Busy work blocks clock advancement, pending obligations block completion, and a future wake joins the earliest-event selection up to the horizon. `delivered` lists the notification ids the harness put in a native prompt during this exchange; they must come from this observation's `notifications`, and the world never offers them to the chat again.

A harness's `done` means its native turn ended, not that its request is finished: the chat becomes idle. The runner does not guess what an idle harness wants. It observes the chat again only when a notification for it exists (another actor's mail or message to the agent, a change to something the agent can see, a due reminder, an operator message) or when the harness's native wake comes. While every chat is idle or blocked the runner moves the world from one scheduled time to the next; once nothing scheduled can reach an idle chat, its task completes as `done`. Idle time never costs steps. A `done` delivers every notification of that observation. A command agent's `done` is its own declaration and ends its task at once.

Every agent, harness or not, can wait and schedule follow-ups through the world's `system` service: `current_time`, `wait_for_notification`, `set_reminder`, `list_reminders` and `cancel_reminder` ([services](services.md#system)). It is served through the same MCP catalog as the other world tools. A `wait_for_notification` call blocks like an approval: its receipt stays `waiting` and the MCP call stays open until a notification arrives or the timeout comes, and its result lists the notifications it delivered.

Reset, cancel and close receive explicit acknowledgements. Cancellation happens between observation exchanges; it does not preempt active inference, and the exchange deadline terminates owned processes. Neither cancellation nor disconnect undoes a dispatched world effect, and lost replies are never blindly replayed. Reset promises chat-state reset, not deletion of external files, global memories or service data.

In an episode, each adapter is bound to one chat before startup and receives a logical chat ID plus a bounded directory namespace for private native state. Sharing one harness adapter across chats is rejected. Scenario world data stays shared.

## Isolation

A native harness with file or shell tools can read anything its process can read, including the scenario's checks. A harness agent therefore sets `container`, or sets `run_on_host: true` to run on the host knowingly. The run's `provenance.json` records `isolation` as `{"kind": "container", "image", "image_id"}` or `{"kind": "host"}`.

With `container`, the runner starts the agent command with `docker run` instead of directly. The world, policy and checks stay on the host. The container holds the agent command and the tool server it starts, so binds on 127.0.0.1 inside it work unchanged. It talks to the runner only over stdin and stdout, and it sees:

- its image, which must already be built locally; images are never pulled;
- one fresh agent folder at `/overnight/agent`, which is also its working directory and `HOME`, and holds `provenance.json` and its usage sidecar. It is created under the system temp folder with a new name, because Docker Desktop cannot see a folder recreated where one was just deleted. When the container ends it is copied to the run's `agent/` folder;
- the listed `mounts`, read-only unless `read_only: false`. Overnight refuses a mount that does not exist, overlaps the repository, or contains the home folder, the current working directory or the system temp folder;
- only the host variables named in `env`, such as a provider key, plus Overnight's own variables.

The container runs as the host user, never root, with all capabilities dropped, `no-new-privileges`, and the `pids`, `memory` and `cpus` limits (defaults 512, `4g`, 2). It never joins a bridge network, because on Docker Desktop a bridge reaches the host's own 127.0.0.1 services, such as the workbench, through the host address.

With no `allow_hosts`, the container has no network. With `allow_hosts`, such as `["openrouter.ai"]`, the agent joins its own internal network, which has no route to the host or the internet. Its only exit is a proxy container on that network (`overnight.execution.egress_proxy`, started from the agent's image). The proxy accepts only HTTPS `CONNECT` to port 443 of a listed host; a name with a leading dot also allows its subdomains. It resolves the name itself, refuses it unless every address is public, and connects to the address it checked. The agent gets `HTTPS_PROXY`, `HTTP_PROXY` and `NODE_USE_ENV_PROXY=1`. Each decision is kept in `egress.log` in the agent folder.

Training adds one exit, never set for an evaluation or a suite: a run started by the [rollout worker](rollouts.md#capture-path) whose policy endpoint is on the host's loopback (`run_scenario(..., policy_port=PORT)`) starts the proxy even without `allow_hosts` and opens its policy port, 3129. The proxy passes each connection there, unread, to `host.docker.internal:PORT` and nowhere else, and logs it in `egress.log`. The agent reaches it at `http://egress:3129` directly (`egress` joins `NO_PROXY`); the CONNECT port still refuses the host. Anything in the container can use that port, and so can any container on Docker's default bridge, where the proxy also sits; it carries only the rollout's own model endpoint, whose calls need the rollout's secret. With Docker Desktop, `host.docker.internal` reaches the host's loopback; with Docker on Linux it does not, and the forward fails. Only harness and persistent command agents take a policy port.

A provider key named in `env` is inside the container, in the environment of the connector process, and everything in the container runs as the same user. A native agent with a shell can therefore read the key, from its own environment or the connector's `/proc/<pid>/environ`, and call the provider directly through the allowed host. A connector's request and cost caps, such as Prime's relay limits, bound a cooperative agent, not an adversarial one; the container keeps the agent away from the scenario and the host, not from its own model key.

Stopping a run removes its container, proxy and network. Those of a killed run are removed when a suite stops or when the next contained run starts. A container shares the host kernel, so it is not a virtual-machine boundary.

```json
"container": {"image": "overnight-agent-base", "mounts": [{"source": "/abs/agent-code", "target": "/code"}], "env": ["OPENROUTER_API_KEY"], "allow_hosts": ["openrouter.ai"]}
```

`containers/agent-base/Dockerfile` builds Python with Overnight's code and no scenarios, examples or runs. Connector images build on it, and must keep `python3` with Overnight installed, because the proxy runs from the agent's image. Run `docker build -f containers/agent-base/Dockerfile -t overnight-agent-base .` from the repository root; `.dockerignore` keeps everything but code out of the build. Connectors whose image does not exist yet set `run_on_host: true`, which records `isolation: host`. A contained ACP connector takes its adapter config inline with `python3 -m overnight.adapters.acp --config-json '<json>'`, so nothing from the host needs mounting. The ACP host passes the proxy variables on to the native process it starts.

| Connector | Image |
| --- | --- |
| [Pi](../connectors/pi/README.md) | `connectors/pi/Dockerfile` → `overnight-pi` |
| [OpenCode](../connectors/opencode/README.md) | `connectors/opencode/Dockerfile` → `overnight-opencode` |
| [Prime](../connectors/prime/README.md) | `connectors/prime/Dockerfile` → `overnight-prime` |

## Connectors

| Connector | Transport | Scheduling | Questions to the operator | Children | Reset |
| --- | --- | --- | --- | --- | --- |
| [Arden](../connectors/arden/README.md) | JSONL host over production Arden | World `system` tools; native Automation scheduler on the world clock | Native durable question API, reported to the operator | Native delegation | Fresh database, runtime and workspace |
| [Feather](../connectors/feather/README.md) | Shared bridge, Feather SDK | World `system` tools; connector `schedule_wake` | Connector `ask_operator` | SDK subagents (`prod` profile) | Fresh history, children and process scopes |
| [Pi](../connectors/pi/README.md) | Shared ACP host | World `system` tools | Operator messages as notifications | Extension-supplied only | Fresh ACP session |
| [Prime](../connectors/prime/README.md) | Shared ACP host | World `system` tools; native jobs/heartbeat via pinned runtime patch | Operator messages as notifications, native messaging | Native factory/messaging | Native session and kernel replacement |
| [OpenCode](../connectors/opencode/README.md) | Shared ACP host, pinned OpenCode | World `system` tools | Operator messages as notifications | Native child sessions | Fresh ACP session |

Every connector's question reaches the runner as a report: an ACP connector's turn final text, Feather's `ask_operator`, or, for Arden, the final text of a completed native run and each new actionable native question (its prompt, detail and choice labels). The operator's next message in that chat answers Arden's sole actionable question through the native question API. The operator answers each report in the asking chat, and decides each approval when it is requested ([operator](scenario-format.md#requests-and-answers)). An answer is an operator message, so it reaches the agent like any other notification. A harness blocked inside a gated call cannot read messages, so the operator never holds a call open to ask a question; it denies and asks in the chat.

OpenCode supports active-turn steering. Connectors without a simulated clock declare `simulated_clock: false`, so cases that need native scheduling fail preflight; they can still wait and set reminders through the world's `system` tools. Arden and Feather read operator messages from `messages` and do not prompt `notifications` natively; the notifications of an observation count as delivered when they answer `done`.

## Run the common hard cases

`examples/catering-harness.json` is a five-service case written as a plain user request: a vendor who replies later, an approval, an outage and retry, protected unrelated data and follow-up cleanup. Its operator is a persona that answers the agent's questions and approvals from a brief, so running it calls the operator model too. It runs Pi in its image by default. `scripts/prepare_open_harness_suite.py` writes a suite that runs Pi, OpenCode and Prime, each in its image, on this case, its overrides and variation, the shared-world episode, the three-day offsite case and the native capability fixtures, without running a model:

```sh
uv run scripts/prepare_open_harness_suite.py --out .local/suites/open/suite.json
overnight suite run .local/suites/open/suite.json --out .local/suites/open/out
```

Every arm runs GPT-6 Luna through OpenRouter by default; `--base-url`, `--api-key-env` and `--reasoning-effort` select another endpoint ([model providers](#model-providers)). The operator persona of the catering, shared-world and offsite cases uses the same provider and model (the suite's `operator_model`). The native fixtures keep a scripted operator: a fixed decision, and no replies to reports. The arms use conservative per-million-token prices ($0.20 input, $0.75 output) as budget bounds; other routes need their own verified rates. Each arm's connector allows at most 40 model requests (`max_requests`), and its request-size and output limits at those rates must fit `max_cost_usd` ($3), so a run's spend is bounded before any request is sent, not only by timeouts. The suite gives Pi its own default native tools (`read`, `bash`, `edit`, `write`) inside the container, as OpenCode and Prime keep theirs; the Pi connector's default profile, meant for host runs, enables world tools only. Pi, OpenCode and Prime reach the provider only through the shared loopback relay (`overnight.adapters.provider_relay`), which holds the key, forwards Chat Completions and Responses requests (streaming or not), replaces the output bound, and refuses an oversized request (413), any request after the cap (429, `native request budget exhausted`), non-standard or stored-context fields, image, file or audio parts and hosted tools, before paying for them. Each refusal is a `provider_rejected` record in the connector's native evidence (`prime.jsonl`, Pi's and OpenCode's `provider.jsonl`), and the harness gets the refusal as a provider error; its retries are refused the same way. When the harness disconnects before the engine answers, the relay ends its own call to the engine and adds a `provider_abandoned` record, so the engine's side sees that the call was given up on. These are request bounds, not provider billing. Feather's model client uses the same relay in its own process, with `--max-requests` (default 12) and `--max-request-bytes` beside `--max-tokens`, its rates and `--max-cost`, and records to `provider.jsonl` in its evidence. `scripts/prepare_harness_catering.py` binds the same case to Arden or Feather on the host.

`examples/offsite-week.json` (suite case `offsite-week`) runs over three simulated days, 259200 seconds. The clock jumps between scheduled times, so the length costs model requests only when the agent is woken. The user asks for a team offsite: a quote from the venue within the hour, the day booked in the shop if it fits the budget, the team calendar kept right, one record and one summary mail at the end.

| Time (s) | What happens |
| --- | --- |
| 0 | The user's request. The agent must ask the venue by 3600 |
| 36000, 86400 | Noise: a newsletter mail and an automated chat status |
| 64800 | A colleague mails that the team can only make Friday |
| 72000 | The venue quotes the price. Thursday is taken; Friday is on another group's option for 9 more hours, then on sale for 8 hours. The venue sends no further message |
| 104400 | The other group's order is cancelled, so Friday has stock. Nothing notifies the agent |
| 133200 | A waiting-list actor buys Friday if it is still in stock |
| 187200 | The colleague mails the final headcount, which the record and the summary need |

Nothing wakes the agent between 86400 and 187200, so it passes only if it comes back on its own in the eight-hour window, through a reminder or a timed wait from the `system` service; the request does not say so. The purchase needs the operator's approval, and the persona approves only the venue day once the agent has told it the price. Two outcome checks use `by` ([checks](scenario-format.md#checks)): the venue was asked by 3600 and Friday was bought by 133200. A `before` check requires the summary to follow the headcount mail. An agent that misses a time keeps partial credit for the outcomes it met ([scoring](suites.md#scoring)).

`examples/offsite-week-oracle.json` is the same world with a scripted oracle in place of the harness, and `examples/offsite-week-operator.jsonl` holds its authored operator answers, so the case is proven without a model:

```sh
overnight run examples/offsite-week-oracle.json \
  --operator-answers examples/offsite-week-operator.jsonl \
  --out .local/runs/offsite-week-oracle
overnight qa examples/offsite-week-oracle.json \
  --operator-answers examples/offsite-week-operator.jsonl
```

The lapse is a purchase by another actor, because a shop item has stock but no sale window. The events that follow the venue's quote wait for the agent's first message to the venue, so a run in which the agent never asks ends `unknown`, not `failed`.

A passing world verdict does not show which native features ran, such as a scheduled wake or a subagent. Inspect the connector's native evidence beside the run as well.

### Model providers

The Pi, OpenCode and Prime connectors take one `provider` option, defined in `overnight.adapters.provider`. It is `"openrouter"` (the default, key in `OPENROUTER_API_KEY`) or an OpenAI-compatible endpoint:

```json
"provider": {"base_url": "https://api.example.test/v1", "api_key_env": "EXAMPLE_API_KEY", "reasoning_effort": "none"}
```

`api_key_env` names the variable that holds the key; the key never enters configuration or evidence. `reasoning_effort` is optional. When set, every request states that effort instead of the harness's own reasoning controls, for endpoints that serve tool calls only at a fixed effort:

- Pi declares the model reasoning-capable and maps thinking `off` to that effort. Its Chat Completions client points at the connector's relay, which posts to `{base_url}/chat/completions` as Prime's does.
- OpenCode uses its OpenAI-compatible Responses package with `reasoningEffort` set. A configured endpoint always uses the Responses package, which honors OpenCode's `strict: false`, so optional tool fields such as a subagent `sessionID` stay optional. The package points at the connector's relay, which posts to `{base_url}/responses` with `max_output_tokens` and sets `reasoning.effort`.
- Prime's relay posts to `{base_url}/chat/completions` with `max_completion_tokens`, drops the OpenRouter-only `reasoning` and `usage` fields, and sends `reasoning_effort`.

The case scripts (`prepare_open_harness_suite.py`, `prepare_acp_probe.py`, `prepare_opencode_native.py`) take `--base-url`, `--api-key-env` and `--reasoning-effort`, write the provider into the adapter options, pass only the key variable into the container, and allow only the base URL's host.

### Token capture

For training, `ProviderRelay` takes a `capture` path. It is off by default. Each connector passes `run_capture()`, the `OVERNIGHT_CAPTURE_PATH` the runner sets only for a run started with `capture=True`, which only the [rollout worker](rollouts.md) does. The runner removes the variable from every other run's environment, so no evaluation captures. `run_capture()` also removes the variable from the connector's own environment, so a harness or tool process the connector starts afterwards does not inherit it; the Pi and OpenCode children and Prime's native process get explicit environments, and Feather's shell worker drops every `OVERNIGHT_` variable.

With capture, the relay serves Chat Completions only (a Responses request is refused, `capture_api`). It sends each call upstream without streaming and with `logprobs: true` and `return_token_ids: true`, the flag vLLM's OpenAI-compatible server reads to return `prompt_token_ids` on the completion and `token_ids` on each choice. Nothing arrives until the whole generation is done, so a captured call waits up to `capture_timeout_seconds` (a `RequestBudget` option, default 540, at most 3600) for the engine instead of the connector's usual client timeout; runs without capture keep that timeout. The default stays under the 10-minute default timeout of OpenAI SDK clients; Feather's own model client waits 30 seconds longer than the relay under capture. Other harnesses' client timeouts toward the relay are their own.

The relay appends one `CapturedCall` per answered call to the capture file (mode 0600) as JSON Lines: the relay's request number, generation ID, prompt and completion token IDs, `logprobs`, `completion_logprobs` and the finish reason. `logprobs` is `returned` when the engine sent one logprob per completion token, which `completion_logprobs` then holds; `not_returned` when it sent none; and `incomplete` when it sent some but not one number per token. Only `returned` carries values, and the relay never fills in a missing one. The `provider_captured` record in the connector's evidence repeats the outcome. A completion without token IDs is refused to the harness (502, `capture_missing`). An engine error passes to the harness with its status and JSON body. Each call the engine did not answer with a usable completion is stored as an `UnansweredCall` line instead: its request number and `reason`, `engine_error` (with `status`), `engine_unreachable` (failed, timed out or abandoned before an answer), `no_completion`, `capture_missing`, or `capture_api` (a refused Responses request, with no number). The capture file is then the whole record of the policy's calls, and the [rollout worker](rollouts.md#reward) drops an episode with any unanswered call. The harness gets the completion without those token fields, as JSON or, when it asked to stream, as a chat-completions event stream with the same content, tool calls and finish reason, plus a usage chunk when `stream_options.include_usage` is set. `read_capture(path)` returns a run's answered calls and its unanswered ones, each in order, and raises `ValueError` on a line that is neither. Runner, policy, world and evaluator never see the capture.

The runner names each capture file itself and reads only those ([rollouts](rollouts.md#capture-path)). A host process writes beside its provenance sidecar, where the agent can also write, because a host run can read and write anything its user can. A contained process writes to `/overnight/capture/calls.jsonl`, its own mount outside the agent folder and its working directory, copied to the host when the container ends; a link or other non-regular file there fails the run. The mount keeps the capture out of the agent's folder and out of the kept copy of it, but the agent's tools run as the same user as the relay, so an agent shell that looks for the mount can still rewrite the file, as it can read the provider key. Captures are trustworthy against an agent that does not target them, not against one that does.
