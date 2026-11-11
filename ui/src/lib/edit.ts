import type { Json } from "./run";

export function put(target: Json, key: string, value: unknown) {
  if (value === undefined || (typeof value === "number" && Number.isNaN(value))) delete target[key];
  else target[key] = value;
}

export function list(target: Json, key: string): Json[] {
  if (!Array.isArray(target[key])) target[key] = [];
  return target[key] as Json[];
}

export function child(target: Json, key: string): Json {
  if (typeof target[key] !== "object" || target[key] === null || Array.isArray(target[key])) target[key] = {};
  return target[key] as Json;
}

export const pathText = (path: unknown): string =>
  (Array.isArray(path) ? path : [])
    .map((part, i) => (typeof part === "number" ? `[${part}]` : `${i ? "." : ""}${String(part)}`))
    .join("");

export function parsePath(text: string): (string | number)[] {
  const trimmed = text.trim();
  if (trimmed.startsWith("[")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return parsed.map((p) => (typeof p === "number" ? p : String(p)));
    } catch {
    }
  }
  const parts: (string | number)[] = [];
  for (const piece of trimmed.split(".")) {
    const match = /^([^[\]]*)((\[\d+\])*)$/.exec(piece);
    if (!match) {
      parts.push(piece);
      continue;
    }
    if (match[1]) parts.push(match[1]);
    for (const index of match[2].matchAll(/\[(\d+)\]/g)) parts.push(Number(index[1]));
  }
  return parts;
}
