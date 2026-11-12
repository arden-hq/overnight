import { useSyncExternalStore } from "react";
import type { RunOperation, ScenarioSummary, StatusResponse } from "../api/types";
import { ApiFailure, errorText, send } from "./api";

interface State {
  operation: RunOperation | null;
  error: string | null;
  dismissed: string | null;
}

let state: State = { operation: null, error: null, dismissed: null };
const listeners = new Set<() => void>();
const setState = (next: Partial<State>) => {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
};

const POLL_MS = 700;
let timer: number | undefined;

function poll(id: string) {
  window.clearTimeout(timer);
  timer = window.setTimeout(async () => {
    try {
      const operation = await send<RunOperation>("GET", `/api/operations/${encodeURIComponent(id)}`);
      setState({ operation });
      if (operation.state === "running") poll(id);
    } catch (error) {
      setState({ error: errorText(error) });
      if (!(error instanceof ApiFailure) || error.status === 0 || error.status >= 500) poll(id);
    }
  }, POLL_MS);
}

export function useOperation(): State {
  return useSyncExternalStore(
    (l) => (listeners.add(l), () => listeners.delete(l)),
    () => state,
  );
}

export function useRunningRunId(): string | null {
  const { operation } = useOperation();
  return operation?.state === "running" ? operation.run_id : null;
}

export function adoptOperation(status: StatusResponse | undefined) {
  const operation = status?.operation;
  if (!operation || state.operation?.id === operation.id) return;
  setState({ operation, error: null });
  poll(operation.id);
}

export async function startRun(scenario: string, extra: { overrides?: string; variation?: string } = {}) {
  try {
    const operation = await send<RunOperation>("POST", "/api/runs", { scenario, ...extra });
    setState({ operation, error: null, dismissed: null });
    poll(operation.id);
  } catch (error) {
    setState({ error: errorText(error) });
  }
}

export async function stopRun() {
  const id = state.operation?.id;
  if (!id) return;
  try {
    setState({ operation: await send<RunOperation>("POST", `/api/operations/${encodeURIComponent(id)}/stop`) });
  } catch (error) {
    setState({ error: errorText(error) });
  }
}

export const dismissOperation = () => setState({ dismissed: state.operation?.id ?? null, error: null });

export function runBlocker(scenario: ScenarioSummary | undefined, status: StatusResponse | undefined, running: boolean, dirty = false): string | undefined {
  if (!scenario) return "Unknown scenario";
  if (running) return "Another run is in progress";
  if (scenario.agent_kind !== "scripted" && !status?.allow_live)
    return `Only scripted runs start here; ${(scenario.agent_kind ?? "this").replace(/_/g, " ")} agents need overnight serve --allow-live`;
  if (!scenario.valid) return "The scenario file is invalid";
  if (dirty) return "Save or discard the edits first";
  return undefined;
}
