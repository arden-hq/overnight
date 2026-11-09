import type { RunDetail, RunSummary, TimelineItem } from "../api/types";
import { serviceIcon } from "../components/Icon";
import type { Chip, Lane, RowDef, RunStatus, Tone } from "../types";

export const LANES: Lane[] = ["operator", "agent", "tools", "people", "services", "views", "system"];

export const LANE_NAMES: Record<Lane, string> = {
  operator: "Operator",
  agent: "Agent",
  tools: "Tool calls",
  people: "People",
  services: "Services",
  views: "Views",
  system: "System",
};

const FAIL = new Set(["failed", "denied", "rejected", "reject", "fault", "error", "timeout"]);
const LATE = new Set(["pending", "approval_required", "waiting"]);
const QUIET = new Set(["succeeded", "allowed", "approve", "approved", "dispatched", "running", "done"]);

export const toneOf = (status: string | null | undefined): Tone => (!status ? undefined : FAIL.has(status) ? "fail" : LATE.has(status) ? "late" : undefined);

const ICON_BY_KIND: Record<string, string> = {
  operator_message: "message",
  operator_approval: "approval",
  operator_status: "wake",
  agent_wait: "wake",
  agent_report: "say",
  agent_done: "check",
  event: "arrival",
  observation: "view",
  agent_observation: "view",
};

export type Json = Record<string, unknown>;
export const obj = (value: unknown): Json => (typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Json) : {});
export const arr = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
export const str = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined);

export interface ServiceDef {
  id: string;
  kind: string;
  state: unknown;
}

export const servicesOf = (scenario: unknown): ServiceDef[] =>
  arr(obj(scenario).services).map((raw) => {
    const service = obj(raw);
    return { id: str(service.id) ?? "", kind: str(service.kind) ?? "", state: service.state };
  });

export interface ActorDef {
  id: string;
  role: string;
  persona: Json;
  operations: Json;
  read: string[];
}

export const actorsOf = (scenario: unknown): ActorDef[] =>
  arr(obj(scenario).actors).map((raw) => {
    const actor = obj(raw);
    return {
      id: str(actor.id) ?? "",
      role: str(actor.role) ?? "",
      persona: obj(actor.persona),
      operations: obj(actor.operations),
      read: arr(actor.read_services).filter((item): item is string => typeof item === "string"),
    };
  });

export function chipsFor(run: RunDetail, opts: { views: boolean; task: string | null }): Chip[] {
  const services = new Map(servicesOf(run.scenario).map((s) => [s.id, s.kind]));
  const taskOfInvocation = new Map<string, string>();
  for (const item of run.timeline) if (item.invocation_id && item.task_id) taskOfInvocation.set(item.invocation_id, item.task_id);
  const taskOf = (item: TimelineItem) => item.task_id ?? (item.invocation_id ? (taskOfInvocation.get(item.invocation_id) ?? null) : null);
  const keep = (item: TimelineItem) => (opts.views || !item.view) && (!opts.task || taskOf(item) === null || taskOf(item) === opts.task);
  const chips: Chip[] = [];
  const groups = new Map<string, Chip>();
  for (const item of run.timeline) {
    if (!keep(item)) continue;
    const t = item.time ?? 0;
    const sequence = item.sequence ?? item.line;
    if (item.lane === "tools") {
      const key = `${item.invocation_id ?? `line-${item.line}`}@${t}`;
      let chip = groups.get(key);
      if (!chip) {
        chip = { id: `L${item.line}`, lane: "tools", row: item.actor_id ?? "agent", t, sequence, kind: "tool", label: "", tone: undefined, icon: "tool", lines: [], task: item.task_id };
        groups.set(key, chip);
        chips.push(chip);
      }
      chip.lines.push(item.line);
      if (item.kind === "invocation" || item.kind === "policy_check") {
        const operation = item.label.replace(/: [^:]*$/, "");
        if (!chip.label || item.kind === "invocation") chip.label = operation;
        chip.service = operation.split(".")[0];
      }
      if (item.status && item.kind !== "invocation") {
        chip.mark = QUIET.has(item.status) ? undefined : item.status;
        chip.tone = toneOf(item.status);
      }
      if (item.kind === "fault") {
        chip.mark = "fault";
        chip.tone = "fail";
      }
      chip.task ??= item.task_id;
      continue;
    }
    const service = item.lane === "services" ? item.row : undefined;
    chips.push({
      id: `L${item.line}`,
      lane: item.lane,
      row: item.lane === "system" ? item.kind : item.row,
      t,
      sequence,
      kind: item.kind,
      label: item.kind === "state_change" ? `${item.row} changed` : item.label,
      mark: item.status && !QUIET.has(item.status) && item.lane !== "operator" ? item.status : undefined,
      tone: toneOf(item.status) ?? (item.kind.endsWith("_error") ? "fail" : undefined),
      icon: service ? serviceIcon(services.get(service)) : (ICON_BY_KIND[item.kind] ?? (item.kind.endsWith("_error") ? "outage" : "records")),
      lines: [item.line],
      task: item.task_id,
      service,
    });
  }
  for (const chip of chips) if (!chip.label) chip.label = "tool call";
  return chips;
}

export function rowsFor(run: RunDetail, chips: Chip[]): RowDef[] {
  const services = servicesOf(run.scenario);
  const kinds = new Map(services.map((s) => [s.id, s.kind]));
  const seen = new Set<string>();
  const rows: RowDef[] = [];
  const add = (lane: Lane, id: string) => {
    const key = `${lane}:${id}`;
    if (seen.has(key)) return;
    seen.add(key);
    rows.push({ lane, id, label: rowLabel(lane, id, kinds.get(id)), icon: rowIcon(lane, kinds.get(id)) });
  };
  for (const lane of LANES) {
    if (lane === "services") for (const s of services) if (chips.some((c) => c.lane === lane && c.row === s.id)) add(lane, s.id);
    for (const chip of chips) if (chip.lane === lane) add(lane, chip.row);
  }
  return rows;
}

function rowLabel(lane: Lane, id: string, kind: string | undefined): string {
  if (lane === "tools") return id === "agent" ? "Agent's calls" : `${id}'s calls`;
  if (lane === "agent") return "Waits and reports";
  if (lane === "services") return kind && kind !== id ? `${id} (${kind})` : id;
  if (lane === "views") return `What ${id} saw`;
  if (lane === "system") return id.replace(/_/g, " ");
  return id;
}

function rowIcon(lane: Lane, kind: string | undefined): string {
  if (lane === "services") return serviceIcon(kind);
  return { operator: "user", agent: "arden", tools: "tool", people: "user", services: "records", views: "view", system: "outage" }[lane];
}

export const distinctTimes = (chips: Chip[]): number[] => [...new Set(chips.map((c) => c.t))].sort((a, b) => a - b);

export function runStatus(verdict: string | null | undefined, complete = true): RunStatus {
  if (verdict === "ungraded") return "ungraded";
  if (verdict === "passed") return "pass";
  if (verdict === "failed") return "fail";
  if (!complete || !verdict) return "incomplete";
  return "unknown";
}

export const isVariant = (run: Pick<RunSummary, "has_variation" | "has_overrides">): boolean => run.has_variation || run.has_overrides;

export const summaryStatus = (run: RunSummary): RunStatus => runStatus(run.verdict, run.complete);

export function modelName(model: unknown, adapter: string | null | undefined): string | undefined {
  if (typeof model === "string" && model) return model;
  const m = obj(model);
  return str(m.name) ?? str(m.id) ?? str(m.model) ?? adapter ?? undefined;
}

export function runTitle(run: Pick<RunSummary, "id" | "name">): string {
  const parts = run.id.split("~").slice(1).map(decodeURIComponent);
  return parts.length > 1 ? parts.slice(-2).join(" / ") : (parts[0] ?? run.name);
}
