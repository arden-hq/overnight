# OpenCode connector

This package runs pinned OpenCode `2.1.3` source through its native ACP server and Overnight's shared ACP host. Factory: `overnight_opencode:adapter`.

Install without changing the repository environment:

```sh
uv sync --extra acp
uv pip install --python .venv/bin/python --no-deps -e ./connectors/opencode
.venv/bin/python connectors/opencode/build_native.py \
  --source /path/to/opencode --out .local/opencode-runtime
.venv/bin/python scripts/prepare_opencode_native.py \
  --runtime .local/opencode-runtime --out .local/runs/opencode-native
.venv/bin/overnight run .local/runs/opencode-native/scenario.json --out .local/runs/opencode-native/result
```

`scripts/prepare_opencode_native.py` writes an adapter config and scenario from `examples/fixtures/` (`--fixture native-capabilities|native-delegation|native-cancellation`) without running a model. By default it uses OpenCode's OpenRouter package with `OPENROUTER_API_KEY`. `--base-url`, `--api-key-env` and `--reasoning-effort` select an OpenAI-compatible endpoint, which uses OpenCode's OpenAI-compatible Responses package with `reasoningEffort` set when given ([model providers](../../docs/harness-integration.md#model-providers)). Responses honors OpenCode's `strict: false`, so the model leaves optional fields such as the subagent `sessionID` out.

## Container

Build the image from the repository root, after `overnight-agent-base` ([isolation](../../docs/harness-integration.md#isolation)), passing the `.git` folder of an OpenCode checkout whose HEAD is the pinned commit. Its build stage runs `build_native.py` on Linux:

```sh
docker build -f connectors/opencode/Dockerfile \
  --build-context opencode-git=/path/to/opencode/.git -t overnight-opencode .
.venv/bin/python scripts/prepare_opencode_native.py --container overnight-opencode --out .local/runs/opencode-contained
.venv/bin/overnight run .local/runs/opencode-contained/scenario.json --out .local/runs/opencode-contained/result
```

The case passes the key by name to the container, where only the connector's relay uses it, and allows only the provider's host.

## Spending cap

OpenCode never receives the provider key. The connector starts Overnight's shared loopback relay (`overnight.adapters.provider_relay`, also used by Prime), points OpenCode's provider package at it with a per-run token, and makes the paid calls itself, so only the connector process reaches the provider host. The options bound a run's spend the way Prime's do:

| Option | Default | Meaning |
| --- | --- | --- |
| `max_requests` | 12 (at most 40) | Model requests the relay sends upstream for this connector, children included |
| `max_tokens` | 2,048 | Output bound written into every request (`max_output_tokens` or the Chat Completions field) |
| `max_request_bytes` | 262,144 | Largest request body the relay forwards |
| `input_usd_per_million`, `output_usd_per_million` | 0.20, 0.75 | Caller-verified route rates |
| `max_cost_usd` | 1 (at most 5) | Options are rejected unless `max_requests` worst-case requests at these rates fit it |

The relay refuses, before paying, an oversized request (413), any request past `max_requests` (429, `native request budget exhausted`), fields outside a standard set (such as `previous_response_id`), `item_reference` inputs, image, file or audio parts, and hosted tools. Each refusal is a `provider_rejected` record in `provider.jsonl` in the connector's evidence, beside `provider_request`, `provider_usage` and a closing `provider_closed` with the request count; the key never appears there. OpenCode gets the refusal as a provider error; retries are refused the same way, so nothing more is paid for. Usage is still metered from OpenCode's ACP reports. This is a request bound, not provider billing.

Pass the built output as `options.runtime`. The factory verifies its pinned source, native steering
patch, and reviewed output hashes at startup; a stock source checkout or npm runtime is unsupported.
The connector creates private HOME/XDG/config/workspace roots, disables updates, model-catalog fetches, sharing and snapshots, and supplies the world as authenticated ACP MCP servers. The default profile preserves OpenCode's native tools and children. Set `native.full_native_tools` to false and list selected tools in `native.allow` for the restricted profile. Native tools still have ordinary host access inside this trusted local integration.

OpenCode retains the ACP session across turns. Overnight creates one connector per chat, resets with a fresh native session, forwards ACP cancellation, and owns process cleanup. World calls use adapter-owned trusted operation IDs because stock OpenCode MCP does not forward Overnight's custom invocation metadata. Native OpenCode tool and child IDs remain in ACP evidence, but they are not asserted as parent lineage for world calls.

OpenCode has native child sessions and background result delivery. The connector observes their exact root, parent and child IDs through the public ACP stream observer. The shared ACP host dispatches authenticated world callbacks while the parent is idle and waits for native quiescence without inserting a model prompt. The pinned runtime has no simulated-time scheduler interface, so this connector declares no scheduling capability and cases that require one fail preflight. It waits and schedules follow-ups through the world's `system` tools (`current_time`, `wait_for_notification`, `set_reminder`, `list_reminders`, `cancel_reminder`), and operator messages and other notifications arrive as new turns of the same session.

When a direct background child completes after its parent turn, the connector forwards the validated terminal event and exact native report once through the retained ACP session. The notice is explicitly connector-owned and untrusted, is acknowledged only when its event ID appears in an outgoing native prompt, and never substitutes for a world receipt or approval.

## Steering

The pinned patch exposes OpenCode's existing native steering through ACP: `_session/steering` → core `delivery: "steer"`. `requireActive` admission runs inside the core execution coordinator, so idle or stopping execution rejects before persistence and execution ownership cannot settle during admission. Stock steering for other callers is unchanged. The builder applies the patch in fresh staging with install scripts suppressed, verifies the reviewed output hashes and publishes atomically; startup rejects stock or mismatched runtimes.
