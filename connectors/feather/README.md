# Feather adapter

External native integration. World effects and approvals stay in Overnight; the
pinned Feather SDK owns model execution, native tools, compaction and subagents.

```sh
uv sync --extra feather
uv pip install --no-deps -e ./connectors/feather
.venv/bin/overnight-feather --help
```

`connectors/feather/agent.py` is the file entry point.
Use `.venv/bin` after installation; an unrelated `uv sync` can remove editable
connector packages unless they are explicitly installed again.

## Native condition

Pass `--native-config /absolute/native.json` in the scenario harness command:

```json
{
  "profile": "prod",
  "image_tools": false,
  "web_tools": false,
  "skills": null,
  "subagents": {"max_concurrent": 2, "max_agents": 2, "max_turns": 8},
  "compaction": {"enabled": true, "max_tokens": 1024, "trigger_tokens": 12000}
}
```

- `world` (default): world tools plus connector questions/wakes; compaction off
  unless explicitly enabled.
- `train` / `prod`: official native file/process/todo tools and installed skills.
  `prod` uses production descriptions/instructions, response recovery and the
  native production compaction policy. A supplied `skills` directory is installed
  alongside bundled skills. File/process tools use a private per-chat workspace.
- `image_tools` enables native image reads. `web_tools` adds official web tools
  and requires `EXA_API_KEY`; it is not required otherwise. Public web usage has
  its own external-service cost, outside the model budget.
- `subagents` enables official Feather `Subagents` in `prod`. Children share the
  workspace and world bridge, with independent dispatch IDs and model accounting.
  Children cannot delegate further, matching the native SDK. Parent completion
  waits for running children; cancel stops children; reset closes all native scopes.

Native files/processes run with local OS permissions; this adapter is not a sandbox.
World service calls from parents and children still pass through the same runner policy.
Connector `schedule_wake` / `ask_operator` remain adapter-owned, not native schedulers.

## Run and verify

Prepare the existing common case, then append `--native-config` and its absolute
path to the generated Feather harness command:

```sh
.venv/bin/python scripts/prepare_harness_catering.py --harness feather \
  --run-dir .local/runs/feather-check --out .local/cases/feather-check.json --max-cost 1
.venv/bin/overnight run .local/cases/feather-check.json --out .local/runs/feather-check
```

The generator selects GPT-6 Luna and explicit conservative route prices. Supply
`OPENROUTER_API_KEY` to the process. Use fresh paths. Allocate budgets across
per-chat processes; each connector's `--max-cost` is its own budget.

The model client reaches the provider only through Overnight's shared loopback relay
(`overnight.adapters.provider_relay`), started in the connector process, which holds the key.
The relay sends at most `--max-requests` (default 12, at most 40) requests of at most
`--max-request-bytes`, replaces each request's output bound with `--max-tokens`, and refuses
another request before paying. These limits at the configured rates must fit `--max-cost`, or
the connector does not start. Its decisions are in `provider.jsonl` beside `native.jsonl`.

For a child-effect check, ask the parent to use `spawn_agent` to read a world record,
then send a specified message through a world tool and return the record value;
require the parent to collect the result using `wait_agent`. Verify world outcomes
and `native_child_result` plus child-labeled `native_model_calls` in `native.jsonl`.
The trusted `native_child_operation` record maps child identity and native call ID to
a distinct adapter dispatch ID; check matching world receipts. Native IDs need not be
globally unique across children. A spawn event alone is not proof.

`reset_scope: chat` clears history, children and process tools before recreating the
chat. It does not promise deletion of retained workspace files or external memory.
Separate episode chats have private workspaces/history; they share the scenario world.

Evidence includes the native condition, tool catalog, effective compaction, provider
usage, child results, world dispatch/receipts and scoped reset records. Configuring a
feature does not show that it ran; check the evidence.
