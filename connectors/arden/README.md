# Arden connector

Runs production Arden as an Overnight harness agent. Arden keeps its own loop, approvals, questions, Automations and children; Overnight keeps the world, policy and grading.

## Run

The connector drives a built Arden source checkout (production server, HeadlessClient, MCP and native scheduler). It needs Node, a built checkout and an existing local Codex login; it does not attach to an installed Arden app or touch its database.

```sh
.venv/bin/python scripts/prepare_harness_catering.py --harness arden \
  --run-dir .local/runs/arden-catering --out .local/cases/arden-catering.json
OVERNIGHT_ARDEN_REPO=/path/to/arden \
OVERNIGHT_ARDEN_RUN_DIR="$PWD/.local/runs/arden-catering" \
.venv/bin/overnight run .local/cases/arden-catering.json --out .local/runs/arden-catering
```

`OVERNIGHT_ARDEN_RUN_DIR` must be the same fresh path as `--out`. `OVERNIGHT_ARDEN_MODEL` selects a Codex model route; the default is `openai-codex/gpt-6-luna`. The provider reads the local Codex token in child memory; it is never written to evidence. Native databases, journals and `arden-native.jsonl` stay beside the standard run artifacts.

Clock advancement waits for Arden's native scheduler (up to 60 seconds of wall time after a jump). Model calls and native timers remain nondeterministic, so runs are not exact replays. A passing world verdict does not show which native features Arden used: check its Automations, questions and answers in `arden-native.jsonl` and `arden.db`.

## Protocol

`host.mjs` is the version-1 harness JSONL command for an Overnight scenario. After capability negotiation, the runner sends a chat-scoped `observe` envelope; the host returns an `action` envelope containing one `invoke`, `request_approval`, `report`, `wait`, or `done` action per line. The next observation carries the previous action's receipt. An invocation keeps its ID through Arden's native approval card, the operator decision, and the real MCP call. If the world still requests approval, the host waits and retries that same invocation after the decision. A `system.wait_for_notification` call stays open while its receipt is `waiting` and returns its later result receipt, which lists the notifications it delivered.

When the runner sets `OVERNIGHT_PROVENANCE_PATH`, the host writes a private JSON sidecar before processing the first observation. The connector fingerprint covers its production `.mjs` files; the runtime fingerprint covers Arden's built server and library JS/JSON, the installed MCP SDK's ESM build, package manifests and Bun lockfile. Git commits are recorded separately because a checkout can be dirty or its build can differ from source. The selected model is explicit; `settings: {}` and `uses_runtime_defaults: true` mean the connector supplies no model setting overrides, so defaults belong to the fingerprinted Arden runtime. Credentials and environment contents are excluded.
The sidecar is an audit receipt, not a guarantee that the run can be replayed identically. It does not fingerprint the Node/Bun binaries or every installed transitive dependency.

The host starts `arden-host.mjs` (this connector's own child, which imports `startServer` from the Arden checkout's built `dist/index.js`) and `HeadlessClient` in a child with an isolated `arden.db`. `runtime.mjs` reads the local Codex credential in child memory; the token is never written to trial evidence. The world operations become MCP tools through Arden's normal connection and tool catalog. The first operator message starts the chat. Each new actionable native question becomes a `report` action holding its prompt, detail and choice labels, so the runner hands it to the operator (`native_question_reported` evidence). A later operator message answers its sole actionable Arden question through the native question API; otherwise it is typed into the chat. Multiple actionable questions require explicit routing. New operator inputs include the runner-filtered world observation without replaying prior messages. The evaluator snapshot and criteria remain private. Native file tools use a run-local workspace configured through session permissions.

The adapter preloads `clock.mjs` to freeze native `Date` at a recent UTC epoch plus simulation seconds. Only acknowledged, monotonic world ticks move it; timers and monotonic time stay real. Arden's separate offset-clock hook is disabled, so model latency cannot move native timestamps ahead of the world or cause backward clock updates. When Arden is idle, the bridge reads its own `scheduled_tasks.next_run_at` and asks the runner to advance to the earlier world event or native Automation wake. The native scheduler starts the Automation; the bridge does not call `/run`. The 60-second scheduler poll remains a real wall-clock wait. `arden-native.jsonl` records model runs, native Automation transitions, tool calls, approvals, clock moves, and world receipts. Arden's database retains the native scheduler and tool records for independent verification. The runner closes the JSONL process at the end, which kills the child and MCP server.

## Ownership

- `host.mjs`: JSONL input/output, signals and final cleanup.
- `arden-host.mjs`: the child that starts the Arden server from the given checkout and answers `start`/`stop`.
- `trial.mjs`: observation boundaries and native scheduler settling.
- `arden-process.mjs`: private trial files/evidence, child lifecycle, bounded IPC/HTTP and HeadlessClient.
- `clock.mjs`: adapter-owned frozen native Date and monotonic world-time IPC.
- `server.log`: private native stdout/stderr for concrete infrastructure failures.
- `native-state.mjs`: read-only native SQL and reported transition cursors. Pending answer deliveries count as busy before their continuation run exists.
- `mcp-services.mjs`: owns the shared Python MCP sidecar process and Arden catalog registration; MCP tool exposure has one owner in `src/overnight/execution/tool_server.py`.
- `world-bridge.mjs`: invocation IDs, native approvals, world receipts and pending decisions. Approval identity is installed before releasing the native gate.
- `operator-messages.mjs`: operator message cursor and paginated native question routing.
- `runtime.mjs`: production Codex provider and in-memory credential only.
- `provenance.mjs`: source/build byte identities and pre-run sidecar.

These owners share explicit APIs, without a global context or a second agent loop. Partial startup and normal/signal shutdown close the child and active MCP connections; an unresponsive child is killed after five seconds.

Arden MCP tools advertise `_meta["arden/execution"]: true` when trusted native identity is required. The native tool executor supplies run, tool-call and chat identity in request metadata, independently of model arguments. The bridge binds that tuple to the approved world invocation, validates its operation/arguments, consumes authorization once, and rejects missing/unknown identities. Concurrent identical approvals therefore retain separate identities. Ordinary external tools receive no identity unless they explicitly request it.

## Capabilities

Lifecycle capabilities are `simulated_clock: true`, `chat_isolation: true`, `cancel: true`, `reset_scope: "chat"`. One adapter owns one native runtime for one Overnight chat, including native child and Automation chats. Other Overnight chat IDs are refused. Episodes explicitly bind `OVERNIGHT_CHAT_ID` and safe `OVERNIGHT_CHAT_NAMESPACE` before startup; their private native files live under `<run>/chats/<namespace>/`. Single-chat runs retain `<run>/arden.db`. Cancellation is processed between observations: native APIs pause user automations, stop active runs and dismiss pending asks while preserving runtime reuse. It does not undo world effects. Chat reset stops the isolated runtime and recreates native state under a fresh `resets/N` directory; old evidence remains. External filesystem deletion is not promised. Status separates active `busy` work from `pending` approvals/questions/deliveries/future wakes. A due native wake is busy/pending with no future `next_wake`; the original due timestamp remains in `native_status` evidence. Wakes beyond the horizon stay reported as future obligations. Native scheduler and question authority remain unchanged.

Child lifetime ends on Node's `close` event, including failed spawns that never emit `exit`; the final force-kill wait is bounded. An MCP disconnect withdraws calls still queued for world dispatch. Calls already dispatched keep their receipt slot so their result settles normally; disconnect cannot undo an external effect. Unused approval identities are retired only from exact native terminal tool/run evidence, never from a timeout or argument heuristic.

## Authority

Trial authority is explicit: simulated services bind through the registered MCP
catalog; native local tools use the private workspace permissions. Native
permission requests outside that authority are denied through the native approval
API, with a direction to use world MCP tools for service access. Real mail/calendar
accounts are never approved by this adapter. Native tools, Automations and children
remain native; an approval refusal is an evaluation condition, not universal
permission coverage.

The configured model is applied through native `/config` to chat, background and
helper routes. Native question answers carry the operator's exact message; ordinary
submissions include the runner's filtered observation. All native actionable
questions in this isolated runtime belong to its operator; multiple simultaneous
questions require explicit routing.

World MCP bindings are already configured in native settings and inherited by
Automations. Repeating those bindings as Automation grants conflicts with the
native settings authority and can make creation fail. Use the existing bindings;
inspect native tool errors and confirm creation before reporting it. This is
integration guidance, not a native permission override or a business-case answer.

## Tests

`cd connectors/arden && node --test` runs the no-model regressions: approval release and retry routing, MCP disconnect before and after dispatch, terminal-identity grant retirement, paginated question authority, clock IPC, provenance and failed-spawn teardown. They use the production owners directly.
