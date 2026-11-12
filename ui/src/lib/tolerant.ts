export function readStorageIfAllowed(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStorageIfAllowed(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    return;
  }
}

export function parseJsonOrUndefined(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

export function onNextTaskEvenInHiddenTab(run: () => void): () => void {
  const timer = setTimeout(run, 0);
  return () => clearTimeout(timer);
}
