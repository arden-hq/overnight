import { useEffect, useState } from "react";

export type Route =
  | { page: "scenarios"; viewKind?: string; viewId?: string }
  | { page: "scenario"; id: string; section: Section }
  | { page: "run"; id: string }
  | { page: "compare"; a: string; b: string };

export type Section = "services" | "events" | "people" | "operator" | "checks";
export const SECTIONS: Section[] = ["services", "events", "people", "operator", "checks"];

export function parse(hash: string): Route {
  const [page, a, b] = hash
    .replace(/^#\/?/, "")
    .split("/")
    .map((part) => decodeURIComponent(part));
  if (page === "scenario" && a) return { page, id: a, section: SECTIONS.includes(b as Section) ? (b as Section) : "services" };
  if (page === "run" && a) return { page, id: a };
  if (page === "compare" && a && b) return { page, a, b };
  return { page: "scenarios", viewKind: a || undefined, viewId: b || undefined };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(location.hash));
  useEffect(() => {
    const on = () => setRoute(parse(location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

const enc = encodeURIComponent;
export const href = {
  scenarios: () => "#/scenarios",
  scenario: (id: string, section: Section = "services") => `#/scenario/${enc(id)}/${section}`,
  run: (id: string) => `#/run/${enc(id)}`,
  compare: (a: string, b: string) => `#/compare/${enc(a)}/${enc(b)}`,
};

export function go(target: string) {
  location.hash = target;
}
