# Suites

A suite runs every arm on every case a fixed number of attempts and compares the arms. An arm is one setup under test. A case is one world: a scenario with optional overrides and a variation.

```sh
overnight suite validate examples/suites/catering.json
overnight suite run examples/suites/catering.json --out .local/suites/catering
overnight suite resume .local/suites/catering
overnight suite report .local/suites/catering
```

`validate` resolves every arm on every case and prints the attempt count and agent kinds. Check these before a run that calls a model: a suite has no live gate and spends what it plans. Keep suites outside the workbench's run roots, because an arm's attempts would otherwise appear as runs of the authored scenario.

## Scenario QA

A score means nothing if a scenario can be passed without the work. QA checks that before you spend money, using the real runner and no model:

```sh
overnight qa examples/catering-chaos.json
overnight suite qa examples/suites/catering.json
```

| Probe | Agent | Must |
| --- | --- | --- |
| `oracle` | The authored agent | Pass, which shows the scenario is solvable |
| `quits` | Finishes at once | Not pass |
| `idles` | Waits until the bound | Be rejected by at least one check |
| `waits` | Waits until the last moment before the bound, then finishes. One more run per task deadline, named `waits for <task>`, finishes just before that deadline | Be rejected by at least one check |
| `claims` | Reports the task is complete, then waits until the bound | Be rejected by at least one check |

A run that reaches the bound is `unknown` whatever its checks say, so `idles`, `waits` and `claims` count the checks that rejected them. If none did, or a task with outcomes had none rejected, the world satisfied it with no agent work, which is a defect. Each task must reject on its own; a run-wide outcome that is already true, such as a quiet inbox, is fine while other checks reject. Output gives the count per probe. A probe that runs out of `max_steps` with events still pending is also a defect, because it did not see the whole run.

The oracle is a fixed script written for the base scenario. When it fails a case with a variation, QA reports that case `no_oracle` rather than a defect: the script does not fit the varied world, which does not show the variation is unsolvable.

For a scenario whose operator is a persona, the sham probes get an operator that replies to nothing and denies every approval, and no model runs. Its oracle needs recorded answers (`overnight qa --operator-answers TRACE`); without them it is reported `no_oracle`.

Exit code 0 means every probe held, 1 means a defect, 2 means nothing is wrong but no oracle proved the scenario solvable, 3 means a scenario, suite or variation file could not be read or is invalid, and 64 means a usage error. Only a scripted oracle runs by default. `--allow-command` also runs a local command agent as the oracle, and it may call a model. A harness is never run. `suite run` does not enforce QA. These probes catch shortcuts in general; they do not replace a scenario-specific cheating attempt.

## Format

| Field | Meaning |
| --- | --- |
| `id` | Suite identity |
| `attempts` | Attempts per arm and case, 1 to 100 |
| `arms` | `id` and an optional `agent` that replaces the case's agent; it keeps the case's `context` unless it sets its own. Without `agent` the case keeps its authored agent |
| `cases` | `id`, `scenario`, optional `overrides` and `variation`; paths are relative to the suite file |
| `retries` | Fresh runs allowed per attempt after `agent_failed`, `invalid` or `error`, 0 to 5, default 2 |
| `operator_model` | Optional [operator model](scenario-format.md#persona) that replaces every persona case's own, so the arms and the operator share one provider |
| `resamples` | Bootstrap resamples, default 5000 |
| `bootstrap_seed` | Bootstrap seed, default 0 |

Every attempt of a case uses the same world seed, so attempts measure the agent's own variation. To vary the world, add cases with different variations.

## Evidence

```
out/
  suite.json                     suite as run
  report.json                    statistics
  .inputs/<arm>/<case>/          frozen scenario, overrides and variation
  <arm>/<case>/attempt-<n>/      an ordinary run directory
  <arm>/<case>/attempt-<n>.log   run output
  <arm>/<case>/attempt-<n>-retry-<r>/  a fresh run after an unscored one
```

Each attempt is one `overnight run` subprocess reading the frozen inputs, with its own directory, ports and agent processes. `--concurrency N` on `suite run` and `suite resume` keeps up to N attempts running at once; the default is 1. Attempts start in order, attempt 1 of every cell first, so a stopped suite leaves arms within about one round of attempts of each other. Nothing limits the provider's request rate, so pick N within your provider's limits. Ctrl-C, SIGTERM or SIGHUP stops every running attempt and its agent, then writes the report. `suite resume` continues from the frozen inputs: it keeps every finished attempt, retries what stayed unscored and has retries left, and starts what never ran. A run the suite stopped mid-way gets a `.stopped` marker next to its directory. It failed nothing, so it may always continue and does not count as a retry. A lock refuses a second process on the same suite. `suite report` recomputes statistics from the attempt directories.

Exit code 0 means every attempt was scored, 4 means some were not, 3 means the suite is invalid, and 64 means a usage error such as an existing `--out`. Verdicts do not change the exit code.

## Scoring

A run that ended early has no meaningful grade, so it is not scored, whoever caused it.

| Attempt outcome | Scored | Counts as a pass | Retried |
| --- | --- | --- | --- |
| `passed` | yes | yes | no |
| `failed` | yes | no | no |
| `unknown` | yes | no | no |
| `agent_failed`: the agent loop failed | no | no | if `failure.retryable` |
| `invalid`: the infrastructure failed | no | no | if `failure.retryable` |
| `error`: no complete evidence was saved | no | no | yes |
| `missing`: never started, or stopped by the suite before it finished | no | no | no |

A retry is a fresh run from the frozen inputs, and earlier tries stay on disk. Only the last try of an attempt counts. A live retry spends money again, up to `retries` times per attempt. The terminal table shows retries per arm, and `report.json` counts each final outcome.

An arm with an attempt still unscored after its retries gets no statistics and is not ranked, only its counts and per-case outcomes. Dropping that attempt alone could hide a failure.

A case scores the mean of its attempts. An arm's `mean` weights cases equally.

| Field | Meaning |
| --- | --- |
| `interval` | 95% percentile bootstrap interval of the mean |
| `pass_all` | Chance that `attempts` draws from a case's scored attempts all pass, averaged over cases |
| `pass_any` | Chance that at least one of those draws passes |
| `rerun_flip` | Chance that two attempts of the arm disagree on a case. Empty with one attempt per case |
| `versus_leader.difference` | Arm mean minus leader mean |
| `versus_leader.interval` | 95% paired bootstrap interval of that difference |
| `versus_leader.tied` | The interval covers zero. Null when there is no interval; the table then prints `no interval` |
| `versus_leader.swap_flip` | Chance that one attempt from each arm disagrees on a case. Close to `rerun_flip` means the arms differ little more than reruns do |

`spend` sums every run an arm paid for, retries included: `seconds`, tokens and `cost_usd`. `seconds` adds each run's own time, so with `--concurrency` above 1 it is more than the wall-clock time. `seconds_per_attempt` divides `seconds` by the attempts that ran, so attempts never started do not lower it; `cost_per_pass` divides cost by passes. One unmeasured run makes that total null, and the table prints `unknown`. The operator persona's spend is kept apart, in `operator_calls` and `operator_cost_usd`, so an arm's cost stays its agent's own.

The leader is the ranked arm with the highest mean; on equal means, the first arm in the file.

The bootstrap resamples cases, then attempts within each case. Arms share case draws, which is what pairs the comparison. The two levels slightly overstate the noise, so intervals run a little wide: that adds ties, never false wins.

Limits:

- Below five cases the report gives no interval, so it cannot tell arms apart: `tied` is null and the table prints `no interval` beside the difference. Neither a win nor a tie is claimed.
- A stopped suite leaves unscored attempts, so usually no arm is ranked. Its counts and per-case outcomes remain.
- A retry resamples the run, so an agent that crashes or corrupts its own provenance while going wrong replaces that run with a new one and can raise its mean. A forbidden action or collateral damage cannot be erased this way. A false claim followed by a crash can, because a claim is only judged in a finished run. High `retries` in one arm deserves a look at the run logs.
- Intervals assume cases represent the scenarios you care about. The leader is picked from the same data and comparisons are not corrected for several arms, so treat a narrow win among many arms with care.
