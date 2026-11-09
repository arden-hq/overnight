# Run evidence

`overnight run --out DIR` creates a fresh owner-only directory and refuses an existing one, so earlier evidence is never overwritten.

| File | Content |
| --- | --- |
| `resolved-scenario.json` | The scenario after overrides, variation and defaults |
| `variation.json` | The validated variation, when `--variation` was given |
| `trace.jsonl` | The append-only world trace, one canonical `TraceEntry` per line |
| `final-state.json` | The final evaluator snapshot |
| `usage.json` | Wall-clock `seconds` measured by the runner, plus the agent's model `calls`, tokens and `cost_usd`, and the operator persona's own under `operator` (zero for a scripted operator or a replay). Null means not measured, never zero |
| `verdict.json` | Verdict, check results, pending work and any `failure`. Written last, so its presence marks complete evidence |
| `task-outcomes.json` | Per-task outcomes for an episode |
| `provenance.json` | Code, seed, adapter identity and the operator for this run |
| `agent/` | The agent process's sidecars (`connector-N-generation-G`). For a [contained](harness-integration.md#isolation) agent, everything it wrote in its agent folder, including native files and `egress.log`; files it left that could not be copied are listed in `copy-errors.txt` |

The trace and resolved scenario are privileged: they may contain private simulation data and evaluation criteria. The operator's requests and answers are in the trace: `operator_request` (what was asked), `operator_answer` (the answer, and as evidence the exact prompts, their digests, the raw model text, settings and usage), `operator_failure` (a request the operator could not answer, with the calls it made) and `operator_message` with the `request_id` it answers. The persona's brief appears only in that evidence. Harness connectors write their native evidence (for example `arden-native.jsonl` or `native/`) beside these files; it is not merged into the world trace.

## Provenance

`provenance.json` ties the resolved setup and seed to the backend code and adapter metadata. Content hashes identify exact bytes even in a dirty checkout; `git_commit` carries a `-dirty` suffix when the work tree differs from HEAD. The backend hash covers `uv.lock` as well as source. Built-in adapters are covered by the backend hash; external connectors report their own identity. A caller-supplied adapter has a class identity and `adapter.code: null`.

Overnight owns provenance. The core records the scenario, seed and its own code. A connector records the connector and runtime it launched and the model it configured, in the file named by `OVERNIGHT_PROVENANCE_PATH`. The native harness never sees this file. The model is the configured one, not one observed from model calls. A plain command wrapper may write the same file. This sidecar is separate from agent actions and never appears in observations. Missing metadata stays unavailable; malformed metadata invalidates the run. Credentials and the process environment are never recorded. `operator` is `{"kind": "scripted"}`, or for a persona its `transport` (`model`, `recorded` or `qa`), `provider`, `configured_model`, the `model` and `settings` a provider reported (null when no model answered) and `prompt_version`. Provenance is an audit receipt, not a promise of deterministic model replay. Snapshots support inspection, not restart.

## Usage

Connectors meter model calls with `UsageMeter`, which saves totals to the file named by `OVERNIGHT_USAGE_PATH`. Feather and Prime record each provider response. ACP connectors record what the agent reports; of these only OpenCode reports usage. Pi and Arden report nothing, so their tokens and cost stay null. A command wrapper may write the same file. A value left out of that file is null, not zero. A model turn or response with no usage, or one that was cancelled, failed or never answered, makes the process totals null, and a value that is not valid usage makes that field null. The exception is a running cost reported per session: it is already a total, so it stays. The meter marks each call as started before it is sent, so a process killed mid-call, by Overnight, its harness or anything else, leaves null totals. A request the provider refused with a client error is not counted. If any process in a run has no meter, the run's totals are null. These are provider-reported numbers read by the connector; no proxy checks them.

## Inspect a run

```sh
overnight inspect .local/runs/catering-chaos
overnight inspect .local/runs/catering-chaos/trace.jsonl \
  --task catering --actor agent --kind result --time-from 5 --limit 20
```

The source is a run directory (with `trace.jsonl` and optional `verdict.json` and `provenance.json`) or an explicit trace path; `--verdict` and `--provenance` attach sidecars to an explicit trace. Filters are exact and combine with AND; time bounds are inclusive. Output is JSON with the matching records in file order, `matched` (all matches) and `returned` (after `--limit`), a run summary and a link graph.

Every nonblank line is parsed as a `TraceEntry`; a malformed line fails the command with its line number. The summary reports result statuses and failures, policy denials, pending decisions and, with a verdict, pending facts. Evaluation-only data is read for inspection and never enters agent observations.

### Links

Rows are identified by line number because one invocation or event ID appears on several lifecycle rows. The graph uses explicit fields only:

- `cause_id` points to the matching invocation anchor.
- Executed lifecycle rows (`fault`, `state_change`, `result`) point to their earlier `kind: invocation` row through `invocation_id`. Policy checks and approvals carry association IDs but are not execution descendants.
- Event lifecycle rows point to their `kind: event` row through `event_id`.

Missing, duplicate and future anchors are reported; links to non-earlier lines are rejected, so cycles cannot occur. Message text, timestamps and row adjacency never create links. The graph supports navigation; it does not diagnose a root cause.
