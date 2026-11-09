import { useSyncExternalStore } from "react";

let app: string | null = null;
const listeners = new Set<() => void>();

export function openPeek(next: string | null) {
  app = next;
  listeners.forEach((l) => l());
}

export function usePeek(): string | null {
  return useSyncExternalStore(
    (l) => (listeners.add(l), () => listeners.delete(l)),
    () => app,
  );
}
