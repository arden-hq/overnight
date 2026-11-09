# Pi connector

Pinned runtime: Pi `0.86.1`, `pi-acp` `0.0.34`. Factory entry point: `overnight_pi:adapter`. Install the Python package separately; install the npm runtime with this directory's `package-lock.json`. [Setup and run instructions](../../docs/adapter-authoring.md#run-the-pi-example).

`options.provider` is `openrouter` (key in `OPENROUTER_API_KEY`) or an OpenAI-compatible endpoint ([model providers](../../docs/harness-integration.md#model-providers)). When the endpoint sets `reasoning_effort`, the model is declared reasoning-capable with thinking `off` mapped to that effort.

## Container

Build the image from the repository root, after `overnight-agent-base` ([isolation](../../docs/harness-integration.md#isolation)):

```sh
docker build -f connectors/pi/Dockerfile -t overnight-pi .
.venv/bin/python scripts/prepare_acp_probe.py --container overnight-pi --out .local/runs/pi-contained
.venv/bin/overnight run .local/runs/pi-contained/scenario.json --out .local/runs/pi-contained/result
```

The case passes the provider key by name and allows only the provider's host. Pi's native files land in the run's `agent/` folder.

The default profile enables Overnight world tools only. Native Pi features can be selected in adapter `options`:

```json
{
  "native": {
    "builtin_tools": ["read", "bash", "edit", "write"],
    "extensions": ["/absolute/path/extension.ts"],
    "skills": ["/absolute/path/SKILL.md"],
    "prompt_templates": [],
    "thinking": "off",
    "compaction": false
  }
}
```

Resources use explicit existing local paths. Automatic resource discovery stays disabled. Native tools run in a private working directory with host access; world operations continue through Overnight policy. The directory is not a filesystem sandbox.

The model definition uses text input and disables reasoning by default. Set `options.reasoning: true` only for a verified reasoning-capable route before selecting a thinking level; the default Luna example uses `thinking: "off"`.

Extensions are trusted code. An extension that starts its own agents, provider clients or schedulers needs its own identity, budget and lifecycle integration; loading it is not proof that those behaviors are covered by this connector.

Pi has no first-party scheduler, so the factory rejects `simulated_clock`. It waits and schedules follow-ups through the world's `system` tools (`current_time`, `wait_for_notification`, `set_reminder`, `list_reminders`, `cancel_reminder`), which every agent gets with its world tools. Interactive extension prompts are refused; operator messages and other notifications arrive as new turns of the same session.
