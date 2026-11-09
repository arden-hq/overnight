# Prime Agent connector

Runs Prime's actual ACP process and Python tool loop against Overnight MCP services.
Native configuration, provider routing, clock/scheduler integration, reset and cleanup live here;
world authorization and approvals remain in Overnight.

## Setup

From the Overnight root:

```sh
uv sync --extra acp
uv pip install -e ./connectors/prime
```

Install the official [Prime Agent 0.10.0 release](https://github.com/PrimeIntellect-ai/prime-agent/releases/tag/v0.10.0)
into a private runtime directory with `bin/prime-agent`. Bootstrap its separate Python kernel:

```sh
export PRIME_RUNTIME=/absolute/private/prime-runtime
mkdir -p "$PRIME_RUNTIME/bootstrap-home"
env HOME="$PRIME_RUNTIME/bootstrap-home" \
  PRIME_AGENT_KERNEL_VENV="$PRIME_RUNTIME/kernel" PRIME_AGENT_TELEMETRY=0 \
  "$PRIME_RUNTIME/bin/prime-agent" --prime-agent-bootstrap
```

The tested macOS ARM64 archive SHA-256 was
`e418bdf62fcb0002777bf3ac43bcc136512b26763f2ab5c500792f26e37294e5`.
Other platform archives have different hashes and were not tested. The backend uses Python
3.14; Prime's own bootstrap selected Python 3.11.16. Launch verifies native version 0.10.0
and records binary/source hashes; the bootstrapped kernel is not a locked dependency bundle.

`options.provider` is `openrouter` (key in `OPENROUTER_API_KEY`) or an OpenAI-compatible
endpoint ([model providers](../../docs/harness-integration.md#model-providers)). The key stays in
the connector's relay; Prime receives a run-local relay credential. For a configured endpoint the
relay posts to `{base_url}/chat/completions` with `max_completion_tokens`, drops OpenRouter-only
fields, and sends the endpoint's `reasoning_effort` when set. On the host this is a trusted local
integration, not a sandbox.

## Image

Build from the repository root, after `overnight-agent-base`
([isolation](../../docs/harness-integration.md#isolation)), passing the `.git` folder of a
Prime Agent checkout that contains the pinned release commit:

```sh
docker build -f connectors/prime/Dockerfile \
  --build-context prime-git=/absolute/prime-agent/.git -t overnight-prime .
```

The image builds the patched evaluation runtime at `/opt/prime` and bootstraps its Python
kernel there; `build_native.py` without `--kernel` does the same outside Docker.

## Build the evaluation runtime

The official release has no virtual-clock control. Build the pinned native evaluation extension;
it uses Prime's scheduler and session replacement rather than emulating them in Overnight:

```sh
.venv/bin/python connectors/prime/build_native.py \
  --source /absolute/prime-agent-checkout \
  --kernel "$PRIME_RUNTIME/kernel" \
  --out /absolute/fresh/prime-eval-runtime
export PRIME_RUNTIME=/absolute/fresh/prime-eval-runtime
```

The build also installs the pinned bundled Python skill packages into the supplied private
kernel venv, following Prime's Verifiers setup. Loading `SKILL.md` alone does not install
its Python module. Repair an existing evaluation runtime with:

```sh
.venv/bin/python connectors/prime/install_skills.py --runtime "$PRIME_RUNTIME"
```

Factory startup checks selected skill imports before model/provider startup; doctor fails
if a module is unavailable. Custom Python skill packages must likewise be installed in that
kernel before selecting their skill files.

The checkout must contain release commit `763094e1b6a7c17450ee8399b06cda1853847ca5`.
Rust 1.95.0 is the default toolchain. The build manifest records source, patch and binary hashes;
launch rejects mismatches. This is a patched native runtime, not the unchanged official binary.
Factory entry point: `overnight_prime.launch:adapter`. Optional `autonomous: true` enables
Prime's continuation loop. Configure its native stop condition and limits separately:

```json
{
  "autonomous": true,
  "autonomy": {
    "gates": ["python /absolute/path/verify.py"],
    "max_continuations": 2,
    "max_turns": 20,
    "timeout_ms": 180000,
    "gate_timeout_ms": 10000
  }
}
```

These are native CLI settings. Gates execute trusted local shell commands. Goal completion
alone does not stop the separate autonomous mode; a passing native gate ends it normally,
while reaching a native limit returns a bounded stop reason. Overnight does not count that
limit as a normal completed answer. Unused continuation budget is not pending work.

Native heartbeat, goal, compact, messaging, observation and factory skills are loaded explicitly.
`bundled_skills` selects installed native skill names; `skills` adds explicit skill-file paths.

## Run the retained-chat probe

```sh
.venv/bin/python scripts/prepare_acp_probe.py \
  --factory overnight_prime.launch:adapter --runtime "$PRIME_RUNTIME" \
  --out .local/runs/prime-probe
.venv/bin/python -m overnight.adapters.doctor \
  --config .local/runs/prime-probe/adapter.json \
  --scenario .local/runs/prime-probe/scenario.json \
  --out .local/runs/prime-probe/doctor.json
.venv/bin/overnight run .local/runs/prime-probe/scenario.json \
  --out .local/runs/prime-probe/result
```

Use a fresh output directory per run. Doctor makes zero model calls and proves startup only.
The probe uses GPT-6 Luna; generated options use conservative $0.20/$0.75 per-million
input/output rates, 12 requests, 2,048 output tokens and a 262,144-byte request limit.
Verify rates before changing models. `PrimeOptions` rejects limits above the configured
cost estimate; this is a request/token bound, not provider-side prepaid credit enforcement.

The case checks retained memory, two reads within one Python execution, one refused deletion,
and a subsequent permitted message. Besides the world verdict, check native quiescence and
recorded process exit in the native evidence. Parent attribution is never inferred from timing.

## Boundaries

- Native spawn defers child kickoff until the parent turn ends. Finish the turn after
  spawning; collecting or messaging before that boundary can precede the initial task.
- Scheduled work uses native heartbeat/cron jobs and world time. Use native minute/hour/day
  intervals, such as `every 1m`; recurring jobs remain pending until deleted.
- Chat reset replaces native conversation/kernel state and cancels the old session's scheduled
  jobs and native child tree. It does not claim to erase persisted global memories or skill files.
- World operations have distinct adapter-generated IDs. Native Python parent identity remains
  unknown because Prime does not forward it in MCP metadata.
- ACP client filesystem/terminal/permission escalation is refused. Prime's native Python
  process still has local OS access: this integration is trusted local code, not a sandbox.
- A declared capability is an interface, not proof that a particular native feature ran;
  check each run's native evidence.
