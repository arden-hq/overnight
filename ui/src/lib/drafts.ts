import { useSyncExternalStore } from "react";
import type { ScenarioDetail, ScenarioValidation } from "../api/types";
import { ApiFailure, errorText, prime, reload, scenarioPath, send } from "./api";
import { obj, type Json } from "./run";

export type Problem = { kind: "stale"; message: string } | { kind: "invalid"; message: string; detail?: string } | { kind: "error"; message: string };

interface Draft {
  base: Json;
  etag: string;
  doc: Json;
  busy?: "validate" | "save";
  validated?: string;
  problem?: Problem;
}

const drafts = new Map<string, Draft>();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((listener) => listener());
const serial = (doc: Json) => JSON.stringify(doc);

const set = (id: string, next: Draft) => {
  drafts.set(id, next);
  emit();
};

export const anyDirty = () => [...drafts.values()].some((d) => serial(d.doc) !== serial(d.base));

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
  useSyncExternalStore(
    (listener) => (listeners.add(listener), () => listeners.delete(listener)),
    () => drafts.get(id),
  );
  let draft = drafts.get(id);
  const etag = detail.summary.etag;
  if (!draft || (draft.etag !== etag && serial(draft.doc) === serial(draft.base))) {
    draft = { base: obj(detail.scenario), etag, doc: obj(detail.scenario) };
    drafts.set(id, draft);
  }
  const current = draft;
  const dirty = serial(current.doc) !== serial(current.base);
  const path = scenarioPath(id);
  const patch = (next: Partial<Draft>) => set(id, { ...(drafts.get(id) ?? current), ...next });

  return {
    doc: current.doc,
    dirty,
    busy: current.busy,
    valid: current.validated === serial(current.doc),
    problem: current.problem,
    edit: (fn) => {
      const doc = structuredClone(drafts.get(id)?.doc ?? current.doc);
      fn(doc);
      patch({ doc, problem: current.problem?.kind === "stale" ? current.problem : undefined });
    },
    replace: (doc) => patch({ doc, problem: undefined }),
    discard: () => set(id, { base: current.base, etag: current.etag, doc: current.base }),
    validate: async () => {
      const text = serial(current.doc);
      patch({ busy: "validate", problem: undefined });
      try {
        await send<ScenarioValidation>("POST", `${path}/validate`, { scenario: current.doc });
        patch({ busy: undefined, validated: text });
      } catch (cause) {
        patch({ busy: undefined, problem: problemOf(cause) });
      }
    },
    save: async () => {
      patch({ busy: "save", problem: undefined });
      try {
        const sent = serial(current.doc);
        const saved = await send<ScenarioDetail>("PUT", path, { scenario: current.doc }, { "if-match": current.etag });
        const doc = obj(saved.scenario);
        const latest = drafts.get(id)?.doc ?? current.doc;
        const kept = serial(latest) === sent ? doc : latest;
        set(id, { base: doc, etag: saved.summary.etag, doc: kept, validated: kept === doc ? serial(doc) : undefined });
        prime(path, saved);
        onSaved?.();
        return true;
      } catch (cause) {
        patch({ busy: undefined, problem: problemOf(cause) });
        return false;
      }
    },
    reloadFromDisk: () => {
      drafts.delete(id);
      emit();
      reload(path);
    },
  };
}
