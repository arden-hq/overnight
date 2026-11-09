import { useSyncExternalStore } from "react";
import type { ScenarioSummary } from "../api/types";
import { runStatus } from "./run";

export interface ScenarioView {
  kind: "view" | "agent" | "shape";
  id: string;
  label: string;
  test: (s: ScenarioSummary) => boolean;
}

export const lastStatus = (s: ScenarioSummary) => (s.last_run ? runStatus(s.last_run.verdict) : undefined);

export const VIEWS: ScenarioView[] = [
  { kind: "view", id: "all", label: "All scenarios", test: () => true },
  { kind: "view", id: "failing", label: "Last run failed", test: (s) => lastStatus(s) === "fail" },
  { kind: "view", id: "passing", label: "Last run passed", test: (s) => lastStatus(s) === "pass" },
  { kind: "view", id: "ungraded", label: "Last run not graded", test: (s) => !!s.last_run && lastStatus(s) !== "pass" && lastStatus(s) !== "fail" },
  { kind: "view", id: "never", label: "Never run", test: (s) => !s.last_run },
  { kind: "view", id: "invalid", label: "Invalid", test: (s) => !s.valid },
];

export const SHAPE_VIEWS: ScenarioView[] = [
  { kind: "shape", id: "tasks", label: "Episodes with tasks", test: (s) => s.task_count > 0 },
  { kind: "shape", id: "single", label: "Single request", test: (s) => s.task_count === 0 },
];

export const agentKinds = (scenarios: readonly ScenarioSummary[]): string[] => [...new Set(scenarios.map((s) => s.agent_kind ?? "unknown"))].sort();

export const AGENT_WORD: Record<string, string> = { scripted: "Scripted", command: "Command", persistent_command: "Persistent command", harness: "Live harness", unknown: "Unknown" };
export const agentWord = (kind: string | null | undefined) => AGENT_WORD[kind ?? "unknown"] ?? kind ?? "Unknown";

export const agentViews = (scenarios: readonly ScenarioSummary[]): ScenarioView[] =>
  agentKinds(scenarios).map((kind) => ({ kind: "agent", id: kind, label: agentWord(kind), test: (s) => (s.agent_kind ?? "unknown") === kind }));

export function findView(scenarios: readonly ScenarioSummary[], kind?: string, id?: string): ScenarioView {
  return [...VIEWS, ...SHAPE_VIEWS, ...agentViews(scenarios)].find((v) => v.kind === kind && v.id === id) ?? VIEWS[0];
}

export function viewHref(v: ScenarioView): string {
  return v.kind === "view" && v.id === "all" ? "#/scenarios" : `#/scenarios/${v.kind}/${encodeURIComponent(v.id)}`;
}

let recent: string[] = [];
const listeners = new Set<() => void>();
try {
  recent = JSON.parse(localStorage.getItem("overnight-recent-scenarios") ?? "[]");
} catch {
  /* storage blocked */
}

export function visit(id: string) {
  if (recent[0] === id) return;
  recent = [id, ...recent.filter((x) => x !== id)].slice(0, 5);
  try {
    localStorage.setItem("overnight-recent-scenarios", JSON.stringify(recent));
  } catch {
    /* storage blocked */
  }
  listeners.forEach((l) => l());
}

export function useRecent(): string[] {
  return useSyncExternalStore(
    (l) => (listeners.add(l), () => listeners.delete(l)),
    () => recent,
  );
}
