# Adapter authoring

The shared authoring package is `overnight.adapters`. The world, actor permissions, approval policy and evaluator stay in Overnight's runner. Read [harness integration](harness-integration.md) first for the control protocol and capability model.

## Responsibility boundary

Native launch/configuration, tool binding, vendor metadata and completion/error interpretation belong in the external connector. Shared adapters own transport, per-chat dispatch/receipts and cleanup. The runner/world/evaluator consume their contracts and must not branch on harness names or native payloads. A standard transport capability may be shared when needed; native interpretation stays external.

The provided connectors under `connectors/` are working examples: Pi is the smallest ACP factory; OpenCode adds native children and steering; Prime adds a native scheduler; Feather uses the bridge directly; Arden uses the JSONL control protocol.

## Choose the connection

- Native MCP with trusted identity forwarding: configure its clients from `SessionContext`.
- ACP with a native tool extension: use the shared ACP host and a small external launch factory. Pi is the working example.
- Other runtimes: keep the documented JSONL control/sidecar boundary in [harness integration](harness-integration.md).
- Bounded one-shot commands receive one observation followed by stdin EOF and must emit exactly one JSON line, then exit successfully. Persistent commands keep stdin open across exchanges. Neither transport supplies native session features automatically.

ACP alone does not forward invocation identity into MCP. Direct tools retain the exact native call ID; programmatic execution needs a distinct operation ID for each inner call. Pi supplies native IDs through `execute(toolCallId, ..., context)`, outside model-authored arguments. Operator approval remains in the runner.

## Shared operation bridge

`ToolBridge` accepts concurrent submissions and dispatches them FIFO, one effect at a time, per bound chat/session. `next_action()` yields a proposal; `resolve(receipts)` validates the full runner receipt and releases only that caller. A pending approval holds its slot, and so does a `waiting` `system.wait_for_notification` receipt: the caller stays blocked until its result receipt arrives. Approval re-proposes the same operation; denial returns to the caller without authorizing the next operation. Queued calls are not pre-approved. `cancel_pending()` releases all waits, drops queued calls and prevents replay within that bridge; it does not roll back an already dispatched effect. Independent chats have independent queues.

MCP metadata uses `arden/execution` and `tool_call_id` as the dispatch key. `TrustedToolContext` is immutable and adds `identity_source` (`native` by default, or `adapter`) plus optional `parent_call_id`. Direct callers keep their native IDs and wire fields. For programmatic calls, a trusted host wrapper can use:

```python
from overnight.execution.tool_server import NATIVE_CONTEXT_KEY, TrustedToolContext

identity = TrustedToolContext.for_operation(
    native_session_id, bound_chat_id, parent_call_id=trusted_native_parent_id
)
result = await mcp_session.call_tool(
    operation,
    arguments,
    meta={NATIVE_CONTEXT_KEY: identity.model_dump(mode="json", exclude_none=True)},
)
```

Create the context per operation, outside evaluated/model-authored code. Omit a parent when it cannot be established; generated operation IDs are recorded as `adapter_dispatch`/`adapter_receipt`, never native calls. A parent supplied by model arguments or a timing/argument join is not trusted. Prime uses adapter-owned operation IDs and leaves its native Python parent unknown. Feather child tools record their trusted native call ID and child identity alongside distinct dispatch IDs. Neither behavior relies on argument matching.

Shared ACP and Feather consumers use this bridge for admission and cancellation. The queue accepts concurrent submissions; world effects remain sequential per chat. Native turn completion with unresolved operations is an infrastructure error. The default ACP client refuses host-file and permission requests. An external factory may supply a native client with its own declared capabilities and explicit access handling; this never authorizes world effects. Native-identity MCP mode rejects absent/malformed identity; adapter-identity mode mints identity outside evaluated code for each authenticated call.

## Python factory contract

An installed ACP connector exports a validated `Factory` entry point:

```python
from overnight.adapters.config import Factory, Launch, SessionContext
from overnight.execution.harness import Capabilities


async def prepare(context: SessionContext) -> Launch:
    # Configure the native runtime; return its command and lifecycle hooks.
    ...


adapter = Factory(
    prepare=prepare,
    capabilities=Capabilities(chat_isolation=True, cancel=True, reset_scope="chat"),
)
```

Declare only capabilities the connector implements. `SessionContext` supplies the bound chat, private directory, allowed catalog, authenticated MCP endpoint/token and caller options. Validate options with Pydantic. The shared host owns ACP transport, receipts, standard session creation and lifecycle dispatch; world authorization stays in the runner.

`Launch` supplies command/cwd/env, code/model provenance and optional standard MCP descriptors. Its native hooks are:

| Hook | Responsibility |
| --- | --- |
| `on_session(SessionBinding)` | Bind a newly created native session, connection and actual client instance |
| `observe(observation)` | Update native time/state; return `NativeObservation` with status, continuation or native request |
| `after_turn(NativeTurn)` | Inspect native completion and return remaining work |
| `wait_for_activity()` | Await native background state changes while the shared bridge admits child effects |
| `validate_turn(NativeTurn)` | Reject incomplete/error native turns using native metadata |
| `cancel`, `reset`, `close` | Release native obligations/resources at the corresponding lifecycle boundary |

`Factory.client_type` optionally provides an external `NativeClient` subclass for vendor notifications. The shared client remains protocol-only. `identity_source="native"` requires trusted invocation metadata; `"adapter"` mints a distinct ID outside evaluated code for each authenticated operation. `instructions` adds connector-specific native guidance. A hook's presence is not proof its native behavior passed a live test.

`Launch.session_metadata` forwards the connector's standard ACP `_meta` object when
creating each native session, including after reset. Native option names and interpretation
remain in the connector. Use it for native SDK limits or profiles; never include credentials.

`NativeClient.stream_observers()` may return public ACP SDK stream observers. Connectors
can consume native lifecycle notifications that the SDK router does not dispatch. Keep
event names, schemas and family ownership in the connector; shared code only registers
the observers. Surface invalid owned events through the connector's lifecycle hooks.

A completed parent turn does not complete an active native child. Return truthful
`pending`/`busy` status and supply `wait_for_activity` for runnable background work.
The frontend races that wait against queued world calls, without another model prompt.
A native scheduled wake is reported as `status.next_wake`; the host answers `wait` until
that time, or `done` when it falls after the horizon. Waiting for the world itself goes
through the `system` tools, never through a native timer.

## Notifications and turns

The shared host starts a native turn only for input: notifications in the observation that
no native prompt has carried yet, a native continuation, or a native request. Operator
messages arrive as `operator_message` notifications, so the prompt carries `notifications`
(only the new ones) instead of the `messages` transcript. A session's first prompt also
carries the instructions and the rest of the observation (world view, operations, receipts).
Later prompts carry only `time` and the new notifications: the session already holds its
tools and earlier results, and resending the observation each turn grows the native context
past provider request limits. The host records the ids it
prompted in `delivered_notifications`, reports them once in `status.delivered`, and never
prompts the same id twice in a session; a chat reset clears that set. An observation that
differs only in time or `as_of` starts no turn. When a turn ends without text and new
notifications arrived meanwhile, the host starts the next turn at once; otherwise it answers
`done`, and the runner treats the chat as idle until a notification for it exists.

Configuration:

```json
{
  "factory": "overnight_pi:adapter",
  "evidence": "/absolute/fresh/run/native",
  "options": {
    "runtime": "/absolute/pi-runtime",
    "model": "openai/gpt-6-luna",
    "input_usd_per_million": 0.2,
    "output_usd_per_million": 0.75
  },
  "timeout_seconds": 120
}
```

Run with `python -m overnight.adapters.acp --config adapter.json`. Use that command in an existing `agent.kind: harness` scenario. Factory code is trusted local code; the import path is not a sandbox boundary. Stdout is protocol-only; diagnostics go to private artifacts/stderr. No core registry edit is required.

## Run the Pi example

From the repository root:

```sh
uv sync --extra acp
uv pip install --no-deps -e ./connectors/pi
mkdir -p .local/pi-runtime
cp connectors/pi/package.json connectors/pi/package-lock.json .local/pi-runtime/
npm ci --prefix .local/pi-runtime --no-audit --no-fund
.venv/bin/python scripts/prepare_acp_probe.py --runtime .local/pi-runtime --out .local/runs/pi-check
```

Supply `OPENROUTER_API_KEY` through your environment. Provider secrets are not written to configuration or journals. Pi/ACP homes and workspace are scoped to this run. The default Pi evaluation profile enables world tools only. [Native profiles](../connectors/pi/README.md) can enable built-ins, explicit extensions, skills, templates, thinking and compaction. Discovery stays disabled. This is a trusted local integration, not hostile-code isolation.

No-model startup check:

```sh
.venv/bin/python -m overnight.adapters.doctor --config .local/runs/pi-check/adapter.json \
  --scenario .local/runs/pi-check/scenario.json --out .local/runs/pi-check/doctor.json
```

The doctor verifies required capability declarations and actual native session startup/shutdown. `ready` does not mean native tool execution or task outcome passed. It uses a separate evidence directory. Use fresh paths for repeat checks.

Real task probe (calls the configured model):

```sh
.venv/bin/overnight run .local/runs/pi-check/scenario.json --out .local/runs/pi-check/result
```

The probe checks retained conversation without replaying an earlier codeword, two same-argument native calls, actual MCP results, a refused scoped approval, a subsequent permitted effect and unchanged collateral state. Compare the world verdict with the native session and tool records under `native/`. The shared contracts are tested in `tests/test_adapter_authoring.py`.

## Capabilities and limits

| Behavior | Shared ACP path (Pi) |
| --- | --- |
| Retained native session | One ACP/Pi session per chat; only new notifications, operator messages included, are delivered |
| Exact tool attribution | Requires a native factory binding; Pi supplies it |
| Cancellation | Between observe exchanges; native ACP cancel stops the active turn and retains the session |
| Chat reset | Fresh native ACP session; prior evidence retained; workspace files are not erased |
| Waiting and follow-ups | World `system` tools (`wait_for_notification`, `set_reminder`) for every connector |
| Native scheduled wake / simulated-clock participation | Pi has no first-party scheduler; its factory rejects this requirement. Prime supplies external native clock/scheduler integration |
| Live mid-turn steering | Optional `steering` capability; see [active-turn control](#active-turn-control) |
| Reconnect / uncertain mutation replay | Unsupported; no blind replay |

The complex catering case needs waiting for a vendor who replies later. Every connector can wait through the world's `system` tools, and an idle harness gets a new turn when the vendor's reply arrives as a notification, so the case needs no native scheduler. Arden's native scheduler, Feather's connector-owned wake and Prime's patched native evaluation runtime remain alternative implementations of the native status/clock contract, not interchangeable native features.

Pi's price inputs are caller-verified route rates. Aggregate request, byte and output limits must fit `max_cost_usd` (at most $5), and the extension exits before an excess request. Native retry is disabled. This is a conservative request bound, not provider billing. A different model requires verified prices.

## Files

- `src/overnight/adapters/`: shared host, protocol, receipt bridge, ACP client, factory contract, evidence, provider relay and doctor.
- `connectors/pi/`: separately installable example package plus pinned npm lock; copy/install it outside Overnight to develop independently.
- `connectors/feather/`: separately installable native Feather integration using the shared host/bridge.
- `connectors/prime/`: native Prime launch, skills, lifecycle and pinned evaluation-runtime patch.
- `connectors/arden/`: production Arden control, tool binding, native scheduling and isolated runtime ownership.
- `connectors/opencode/`: pinned native ACP runtime, builders and patches, child-family ownership and steering hooks.

Native artifacts are private. Keep raw native logs alongside normalized run artifacts. Cleanup closes owned runtimes; it does not roll back effects or erase retained evidence.

## Active-turn control

A connector that declares `steering` lets the operator inject text into a running native turn.

```python
import threading

from overnight.execution.harness import HarnessAdapter

adapter: HarnessAdapter = ...  # started harness adapter
worker = threading.Thread(target=adapter.respond, args=(observation,))
worker.start()
target = adapter.wait_for_active_turn(timeout_seconds=30)
ack = adapter.steer(target, "Use the 11:00 slot instead.")
if not ack.accepted:
    print(ack.reason)  # inactive, target_mismatch, duplicate, unsupported, native_rejected
worker.join()
```

- `wait_for_active_turn()` publishes the target after the observation is written. Its `(session_id, turn_id)` identifies that blocking control exchange; a native prompt spanning several world receipt exchanges receives a fresh target for each, so an earlier target cannot steer a later exchange.
- Each steering request and reply is correlated independently of action replies. The shared host accepts only the exact active chat, session and turn; inactive, stale, mismatched and duplicate requests are rejected immediately and never queued or replayed.
- `SteerAck.accepted` means the connector's native callback (`Launch.steer`) completed. Unsupported or rejected delivery is explicit.
- An acknowledged reset, cancel or close retires the active target, suppresses its late action reply and wakes the blocked caller with an interruption error.
- Adapters that do not declare `steering` keep serial JSONL exchanges.

Control IDs are opaque host identifiers. The connector addresses its already-bound native session; it must never compare or forward the control IDs as native IDs, and it must refuse to fall back to a new native turn.

ACP native and MCP contexts live in one resource-owner task, so concurrent observations and lifecycle commands never enter and exit AnyIO contexts from different tasks. Cleanup failures propagate; an acknowledgement cannot hide failed native retirement. A connector's activity signal must preserve events that arrive between observation and waiting.
