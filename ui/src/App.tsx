import { useEffect, useState } from "react";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useCatalog } from "./lib/api";
import { useRoute } from "./lib/router";
import { findView } from "./lib/views";
import { Sidebar } from "./components/Sidebar";
import { OperationBar } from "./components/OperationBar";
import { Unavailable } from "./components/Unavailable";
import { anyDirty } from "./lib/drafts";
import { Compare } from "./screens/Compare";
import { Editor } from "./screens/Editor";
import { Scenarios } from "./screens/Scenarios";
import { Viewer } from "./screens/Viewer";

function useTheme(): [string, () => void] {
  const initial = document.documentElement.dataset.theme ?? "light";
  const [theme, setTheme] = useState(initial);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("overnight-theme", theme);
    } catch {
      /* storage blocked */
    }
  }, [theme]);
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    const off = document.createElement("style");
    off.textContent = "*,*::before,*::after{transition:none !important}";
    document.head.appendChild(off);
    document.documentElement.dataset.theme = next;
    void getComputedStyle(document.body).backgroundColor;
    setTimeout(() => off.remove(), 0);
    setTheme(next);
  };
  return [theme, toggle];
}

export function App() {
  const catalog = useCatalog();
  const route = useRoute();
  const [theme, toggle] = useTheme();
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (anyDirty()) e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);
  if (catalog.loadState.kind === "loading") return <p className="empty pad">Loading scenarios…</p>;
  if (catalog.loadState.kind === "error") return <Unavailable what="the scenarios" error={catalog.loadState.error} onRetry={() => void catalog.refresh()} />;
  const view = findView(catalog.scenarios, route.page === "scenarios" ? route.viewKind : undefined, route.page === "scenarios" ? route.viewId : undefined);
  return (
    <TooltipProvider delayDuration={400}>
      <div className="shell">
        <Sidebar route={route} view={view} theme={theme} onTheme={toggle} />
        <main className="screen">
          {route.page === "scenario" && <Editor key={route.id} id={route.id} section={route.section} />}
          {route.page === "run" && <Viewer key={route.id} runId={route.id} />}
          {route.page === "compare" && <Compare key={route.a + route.b} left={route.a} right={route.b} />}
          {route.page === "scenarios" && <Scenarios view={view} />}
        </main>
        <OperationBar />
      </div>
      <Toaster theme={theme as "light" | "dark"} position="bottom-right" />
    </TooltipProvider>
  );
}
