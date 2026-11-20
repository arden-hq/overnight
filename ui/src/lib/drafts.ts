import { createContext, useSyncExternalStore } from "react";
import type { ScenarioDetail, ScenarioValidation } from "../api/types";
import { ApiFailure, errorText, prime, reload, scenarioPath, send } from "./api";
import { obj, type Json } from "./run";

export type Problem = { kind: "stale"; message: string } | { kind: "invalid"; message: string; detail?: string } | { kind: "error"; message: string };
export type DraftPath = readonly (string | number)[];
type InputError = { text: string; problem: string };
type InvalidInput = InputError & { path: DraftPath };

interface Draft {
  base: Json;
  etag: string;
  doc: Json;
  invalidInputs: ReadonlyMap<string, InvalidInput>;
  inputRevision: number;
  busy?: "validate" | "save";
  validated?: string;
  problem?: Problem;
}

interface DraftInputs {
  revision: number;
  invalid: ReadonlyMap<string, InvalidInput>;
  setInvalid: (path: DraftPath, invalid: InputError | null) => void;
}

export const DraftInputsContext = createContext<DraftInputs | null>(null);

const drafts = new Map<string, Draft>();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => (listeners.add(listener), () => listeners.delete(listener));
const emit = () => listeners.forEach((listener) => listener());
const serial = (doc: Json) => JSON.stringify(doc);

const set = (id: string, next: Draft) => {
  drafts.set(id, next);
  emit();
};

const isDirty = (draft: Draft) => draft.invalidInputs.size > 0 || serial(draft.doc) !== serial(draft.base);
export const anyDirty = () => [...drafts.values()].some(isDirty);
export function useDraftDirty(id: string | undefined): boolean {
  return useSyncExternalStore(subscribe, () => {
    const draft = id === undefined ? undefined : drafts.get(id);
    return draft !== undefined && isDirty(draft);
  });
}

function childValue(value: unknown, part: string | number): unknown {
  return Array.isArray(value) && typeof part === "number" ? value[part] : obj(value)[String(part)];
}

function sameFieldAfterEdit(before: unknown, after: unknown, path: DraftPath): boolean {
  for (const part of path) {
    if (Array.isArray(before) && Array.isArray(after) && before.length > after.length) return false;
    before = childValue(before, part);
    after = childValue(after, part);
  }
  return JSON.stringify(before) === JSON.stringify(after);
}

function keepUnchangedInputs(draft: Draft, doc: Json): Draft["invalidInputs"] {
  return new Map([...draft.invalidInputs].filter(([, input]) => input.path.length === 0 ||
    sameFieldAfterEdit(draft.doc, doc, input.path)));
}

function problemOf(cause: unknown): Problem {
  if (cause instanceof ApiFailure && cause.status === 409) return { kind: "stale", message: cause.message };
  if (cause instanceof ApiFailure && cause.status === 422) {
    const detail = typeof obj(cause.details).error === "string" ? (obj(cause.details).error as string) : undefined;
    return { kind: "invalid", message: cause.message, detail: detail && detail !== cause.message ? detail : undefined };
  }
  return { kind: "error", message: errorText(cause) };
}

export interface DraftHandle {
  doc: Json;
  dirty: boolean;
  invalidInput: boolean;
  inputs: DraftInputs;
  busy?: "validate" | "save";
  valid: boolean;
  problem?: Problem;
  edit: (fn: (doc: Json) => void) => void;
  replace: (doc: Json) => void;
  discard: () => void;
  validate: () => Promise<void>;
  save: () => Promise<boolean>;
  reloadFromDisk: () => void;
}

export function useDraft(id: string, detail: ScenarioDetail, onSaved?: () => void): DraftHandle {
  useSyncExternalStore(subscribe, () => drafts.get(id));
  let draft = drafts.get(id);
  const etag = detail.summary.etag;
  if (!draft || (draft.etag !== etag && !draft.busy && !isDirty(draft))) {
    const base = obj(detail.scenario);
    draft = { base, etag, doc: base, invalidInputs: new Map(), inputRevision: (draft?.inputRevision ?? 0) + 1 };
    drafts.set(id, draft);
  }
  const current = draft;
  const invalidInput = current.invalidInputs.size > 0;
  const path = scenarioPath(id);
  const patch = (next: Partial<Draft>) => set(id, { ...(drafts.get(id) ?? current), ...next });
  const setInvalid = (path: DraftPath, input: InputError | null) => {
    const latest = drafts.get(id) ?? current;
    const key = JSON.stringify(path);
    if (input === null && !latest.invalidInputs.has(key)) return;
    const invalidInputs = new Map(latest.invalidInputs);
    if (input) invalidInputs.set(key, { ...input, path });
    else invalidInputs.delete(key);
    set(id, { ...latest, invalidInputs });
  };
  const discard = () => set(id, {
    base: current.base, etag: current.etag, doc: current.base,
    invalidInputs: new Map(), inputRevision: current.inputRevision + 1,
  });
  const update = (doc: Json) => {
    const latest = drafts.get(id) ?? current;
    set(id, { ...latest, doc, invalidInputs: keepUnchangedInputs(latest, doc), problem: latest.problem?.kind === "stale" ? latest.problem : undefined });
  };

  return {
    doc: current.doc,
    dirty: isDirty(current),
    invalidInput,
    inputs: { revision: current.inputRevision, invalid: current.invalidInputs, setInvalid },
    busy: current.busy,
    valid: !invalidInput && current.validated === serial(current.doc),
    problem: current.problem,
    edit: (fn) => {
      const doc = structuredClone(drafts.get(id)?.doc ?? current.doc);
      fn(doc);
      update(doc);
    },
    replace: update,
    discard,
    validate: async () => {
      const submitted = drafts.get(id) ?? current;
      if (submitted.invalidInputs.size || submitted.busy) return;
      const text = serial(submitted.doc);
      patch({ busy: "validate", problem: undefined });
      try {
        await send<ScenarioValidation>("POST", `${path}/validate`, { scenario: submitted.doc });
        patch({ busy: undefined, validated: text });
      } catch (cause) {
        const latest = drafts.get(id) ?? submitted;
        patch({ busy: undefined, problem: serial(latest.doc) === text ? problemOf(cause) : undefined });
      }
    },
    save: async () => {
      const submitted = drafts.get(id) ?? current;
      if (submitted.invalidInputs.size || submitted.busy) return false;
      patch({ busy: "save", problem: undefined });
      try {
        const sent = serial(submitted.doc);
        const saved = await send<ScenarioDetail>("PUT", path, { scenario: submitted.doc }, { "if-match": submitted.etag });
        const doc = obj(saved.scenario);
        const latest = drafts.get(id) ?? submitted;
        const kept = !latest.invalidInputs.size && serial(latest.doc) === sent ? doc : latest.doc;
        prime(path, saved);
        set(id, { ...latest, base: doc, etag: saved.summary.etag, doc: kept, busy: undefined, validated: kept === doc ? serial(doc) : undefined });
        onSaved?.();
        return true;
      } catch (cause) {
        patch({ busy: undefined, problem: problemOf(cause) });
        return false;
      }
    },
    reloadFromDisk: () => {
      discard();
      reload(path);
    },
  };
}
