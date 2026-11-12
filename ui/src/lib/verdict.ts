import type { CheckView, Layer, RunFailure } from "../api/types";
import type { RunStatus } from "../types";

export const LAYERS: { id: Layer; name: string }[] = [
  { id: "outcome", name: "Outcome" },
  { id: "forbidden", name: "Forbidden" },
  { id: "collateral", name: "Collateral" },
  { id: "claims", name: "Claims" },
];

export const checkStatus = (c: CheckView): RunStatus => (c.passed === true ? "pass" : c.passed === false ? "fail" : "unknown");

export function failureLabel(failure: RunFailure): string {
  if (failure.retryable === false) return "Setup cannot run";
  return failure.kind === "agent" ? "Agent failed" : "Infrastructure failed";
}

export function verdictLine(status: RunStatus, checks: CheckView[], runState: string | null): string {
  const failed = LAYERS.map((l) => ({ l, n: checks.filter((c) => c.layer === l.id && c.passed === false).length })).filter((x) => x.n);
  if (status === "running") return "Still running; the checks are graded when the run ends.";
  if (status === "incomplete") return "No verdict recorded.";
  if (status === "unknown") return runState && runState !== "done" ? `Not graded: the run ended ${runState.replace(/_/g, " ")}.` : "Not graded.";
  if (status === "fail" && !failed.length) return "Failed.";
  if (!failed.length) return checks.length > 1 ? `All ${checks.length} checks passed.` : checks.length ? "The check passed." : "No checks.";
  const parts = failed.map((x) => `${x.n} ${x.l.name.toLowerCase()}`);
  const total = failed.reduce((s, x) => s + x.n, 0);
  return `Failed ${parts.join(", ").replace(/, ([^,]*)$/, " and $1")} ${total > 1 ? "checks" : "check"}.`;
}


export const STATUS_WORD: Record<RunStatus, string> = { ungraded: "Ungraded", pass: "Pass", fail: "Fail", unknown: "Unknown", incomplete: "Incomplete", running: "Running" };
export const STATUS_TONE: Record<RunStatus, string> = { ungraded: "text-muted-foreground", pass: "text-pass", fail: "text-fail", unknown: "text-muted-foreground", incomplete: "text-muted-foreground", running: "text-foreground" };
export const STATUS_ICON: Record<RunStatus, string> = { ungraded: "dash", pass: "check", fail: "x", unknown: "dash", incomplete: "clock", running: "play" };
