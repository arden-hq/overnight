import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import type { ApiError, RunList, RunSummary, ScenarioList, ScenarioSummary, StatusResponse, TraceLine, TraceQueryResult } from "../api/types";
import { parseJsonOrUndefined } from "./tolerant";

export class ApiFailure extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  constructor(status: number, error: ApiError) {
    super(error.message);
    this.status = status;
    this.code = error.code;
    this.details = error.details;
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

const asApiError = (body: unknown, fallback: string): ApiError =>
  isRecord(body) && typeof body.code === "string" && typeof body.message === "string"
    ? { code: body.code, message: body.message, details: body.details }
    : { code: "http_error", message: fallback };

export async function request<T>(path: string): Promise<T> {
  return send<T>("GET", path);
}

export async function send<T>(method: string, path: string, payload?: unknown, headers: Record<string, string> = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: { accept: "application/json", ...(method === "GET" ? {} : { "content-type": "application/json" }), ...headers },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
  } catch (cause) {
    throw new ApiFailure(0, { code: "unreachable", message: cause instanceof Error ? cause.message : String(cause) });
  }
  const text = await response.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiFailure(response.status, { code: response.ok ? "invalid_json" : "unreachable", message: text.slice(0, 200) || response.statusText });
  }
  if (!response.ok) throw new ApiFailure(response.status, asApiError(body, response.statusText));
  return body as T;
}

export const errorText = (cause: unknown): string => (cause instanceof ApiFailure ? `${cause.code}: ${cause.message}` : cause instanceof Error ? cause.message : String(cause));

type Entry = { data?: unknown; error?: unknown; pending?: Promise<void> };
const cache = new Map<string, Entry>();
const generation = new Map<string, number>();
const listeners = new Set<() => void>();
let version = 0;
const emit = () => {
  version++;
  listeners.forEach((listener) => listener());
};

function load(path: string, force = false): void {
  const entry = cache.get(path);
  if (entry?.pending || (entry && !force && (entry.data !== undefined || entry.error !== undefined))) return;
  const next: Entry = { ...entry };
  const started = generation.get(path) ?? 0;
  const current = () => (generation.get(path) ?? 0) === started;
  next.pending = request<unknown>(path).then(
    (data) => {
      cache.set(path, current() ? { data } : { ...cache.get(path), pending: undefined });
      emit();
    },
    (error: unknown) => {
      cache.set(path, current() ? { error, data: entry?.data } : { ...cache.get(path), pending: undefined });
      emit();
    },
  );
  cache.set(path, next);
}

export function prime(path: string, data: unknown): void {
  generation.set(path, (generation.get(path) ?? 0) + 1);
  cache.set(path, { data });
  emit();
}

export function reload(path: string): void {
  load(path, true);
}

export interface Resource<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

export function useResource<T>(path: string | null): Resource<T> {
  useSyncExternalStore(
    (listener) => (listeners.add(listener), () => listeners.delete(listener)),
    () => version,
  );
  useEffect(() => {
    if (path) load(path);
  }, [path]);
  const entry = path ? cache.get(path) : undefined;
  const reload = useCallback(() => {
    if (path) load(path, true);
  }, [path]);
  return { data: entry?.data as T | undefined, error: entry?.error, loading: !!path && entry?.data === undefined && entry?.error === undefined, reload };
}

export const runPath = (id: string) => `/api/runs/${encodeURIComponent(id)}`;
export const scenarioPath = (id: string) => `/api/scenarios/${encodeURIComponent(id)}`;
export const entryPath = (run: string, line: number) => `${runPath(run)}/entries/${line}`;

type LoadState = { kind: "loading" } | { kind: "error"; error: unknown } | { kind: "ready" };

interface Catalog {
  loadState: LoadState;
  status: StatusResponse | undefined;
  scenarios: readonly ScenarioSummary[];
  runs: readonly RunSummary[];
  refresh: () => Promise<void>;
}

const CatalogContext = createContext<Catalog | undefined>(undefined);

const POLL_MS = 15_000;

export function ApiProvider({ children }: { children: ReactNode }) {
  const [loadState, setLoadState] = useState<LoadState>({ kind: "loading" });
  const [status, setStatus] = useState<StatusResponse>();
  const [scenarios, setScenarios] = useState<readonly ScenarioSummary[]>([]);
  const [runs, setRuns] = useState<readonly RunSummary[]>([]);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, scenarioList, runList] = await Promise.all([
        request<StatusResponse>("/api/status"),
        request<ScenarioList>("/api/scenarios"),
        request<RunList>("/api/runs"),
      ]);
      setStatus(nextStatus);
      setScenarios(scenarioList.scenarios);
      setRuns(runList.runs);
      setLoadState({ kind: "ready" });
    } catch (error) {
      setLoadState((current) => (current.kind === "ready" ? current : { kind: "error", error }));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const value = useMemo<Catalog>(() => ({ loadState, status, scenarios, runs, refresh }), [loadState, status, scenarios, runs, refresh]);
  return createElement(CatalogContext.Provider, { value }, children);
}

export function useCatalog(): Catalog {
  const value = useContext(CatalogContext);
  if (value === undefined) throw new Error("ApiProvider is missing.");
  return value;
}

export function scenarioOfRun(scenarios: readonly ScenarioSummary[], scenarioId: string | null | undefined): ScenarioSummary | undefined {
  return scenarioId ? scenarios.find((item) => item.scenario_id === scenarioId) : undefined;
}

export function runsOf(runs: readonly RunSummary[], scenarioId: string | null | undefined): RunSummary[] {
  return scenarioId ? runs.filter((run) => run.scenario_id === scenarioId) : [];
}

const entryCache = new Map<string, TraceLine>();

export function useEntries(runId: string, lines: readonly number[] | null): { entries?: TraceLine[]; error?: string } {
  const key = lines ? lines.join(",") : "";
  const [state, setState] = useState<{ key: string; entries?: TraceLine[]; error?: string }>({ key: "" });
  useEffect(() => {
    if (!lines) return;
    let current = true;
    void Promise.all(
      lines.map(async (line) => {
        const path = entryPath(runId, line);
        const hit = entryCache.get(path);
        if (hit) return hit;
        const entry = await request<TraceLine>(path);
        entryCache.set(path, entry);
        return entry;
      }),
    ).then(
      (entries) => current && setState({ key, entries }),
      (error: unknown) => current && setState({ key, error: errorText(error) }),
    );
    return () => {
      current = false;
    };
  }, [runId, key]);
  return state.key === key ? { entries: state.entries, error: state.error } : {};
}

export interface Snapshot {
  t: number;
  sequence: number;
  line: number;
  service: string;
  state: unknown;
}

export function useSnapshots(runId: string): { snapshots?: Snapshot[]; error?: string } {
  const [state, setState] = useState<{ runId: string; snapshots?: Snapshot[]; error?: string }>({ runId: "" });
  useEffect(() => {
    let current = true;
    const fromRow = (line: number, row: Record<string, unknown>): Snapshot | undefined => {
      if (row.kind !== "state_change") return undefined;
      const payload = isRecord(row.payload) ? row.payload : {};
      if (typeof payload.service_id !== "string") return undefined;
      return { t: typeof row.time === "number" ? row.time : 0, sequence: typeof row.sequence === "number" ? row.sequence : line, line, service: payload.service_id, state: payload.state };
    };
    const strict = async () => {
      const result = await request<TraceQueryResult>(`${runPath(runId)}/trace?kind=state_change`);
      return result.records.map((record) => fromRow(record.line, record.entry as unknown as Record<string, unknown>)).filter((s): s is Snapshot => !!s);
    };
    const raw = async () => {
      const response = await fetch(`${runPath(runId)}/artifacts/trace.jsonl`);
      if (!response.ok) throw new ApiFailure(response.status, asApiError(await response.json().catch(() => null), response.statusText));
      const out: Snapshot[] = [];
      (await response.text()).split("\n").forEach((text, index) => {
        if (!text.includes('"state_change"')) return;
        const row = parseJsonOrUndefined(text);
        const snapshot = isRecord(row) ? fromRow(index + 1, row) : undefined;
        if (snapshot) out.push(snapshot);
      });
      return out;
    };
    void strict()
      .catch(raw)
      .then(
        (snapshots) => current && setState({ runId, snapshots: snapshots.sort((a, b) => a.t - b.t || a.sequence - b.sequence) }),
        (error: unknown) => current && setState({ runId, error: errorText(error) }),
      );
    return () => {
      current = false;
    };
  }, [runId]);
  return state.runId === runId ? state : {};
}

export function stateAt(snapshots: readonly Snapshot[] | undefined, service: string, initial: unknown, t: number): { state: unknown; changed?: Snapshot } {
  let changed: Snapshot | undefined;
  for (const s of snapshots ?? []) if (s.service === service && s.t <= t) changed = s;
  return { state: changed ? changed.state : initial, changed };
}
