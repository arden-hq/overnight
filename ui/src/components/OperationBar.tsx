import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ScenarioSummary } from "../api/types";
import { useCatalog } from "../lib/api";
import { go, href, parse } from "../lib/router";
import { adoptOperation, dismissOperation, runBlocker, startRun, stopRun, useOperation } from "../lib/operation";
import { cn } from "@/lib/utils";
import { Icon } from "./Icon";

const STATE_WORD = { running: "Running", finished: "Finished", failed: "Failed", stopped: "Stopped" } as const;
const STATE_TONE = { running: "text-muted-foreground", finished: "text-pass", failed: "text-fail", stopped: "text-late" } as const;

const lastLine = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .at(-1) ?? "";

export function RunButton({ scenario, dirty, compact }: { scenario: ScenarioSummary | undefined; dirty?: boolean; compact?: boolean }) {
  const { status } = useCatalog();
  const { operation } = useOperation();
  const blocker = runBlocker(scenario, status, operation?.state === "running", dirty);
  const button = (
    <Button
      variant={compact ? "ghost" : "outline"}
      size={compact ? "icon-xs" : "sm"}
      className={cn(!compact && "shadow-sm")}
      aria-label={compact ? `Run ${scenario?.id ?? ""}` : undefined}
      disabled={!!blocker}
      onClick={(e) => {
        e.stopPropagation();
        if (scenario) void startRun(scenario.id);
      }}
    >
      <Icon name="play" size={compact ? 12 : 14} />
      {!compact && "Run"}
    </Button>
  );
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* A disabled button gets no pointer events; the span carries the tooltip. */}
        <span className="inline-flex" onClick={(e) => e.stopPropagation()}>
          {button}
        </span>
      </TooltipTrigger>
      <TooltipContent>{blocker ?? `Run ${scenario?.id ?? ""}`}</TooltipContent>
    </Tooltip>
  );
}

export function OperationBar() {
  const { status, refresh } = useCatalog();
  const { operation, error, dismissed } = useOperation();
  const previous = useRef<string | null>(null);

  useEffect(() => adoptOperation(status), [status]);

  useEffect(() => {
    if (!operation) return;
    const key = `${operation.id}:${operation.state}`;
    const was = previous.current;
    previous.current = key;
    if (operation.state === "running" || was === null || was === key || !was.startsWith(operation.id)) return;
    void refresh().then(() => {
      const route = parse(location.hash);
      const fromStart = route.page === "scenarios" || (route.page === "scenario" && route.id === operation.scenario);
      if (operation.state === "finished" && operation.run_id && fromStart) go(href.run(operation.run_id));
      else toast(`${STATE_WORD[operation.state]}: ${operation.scenario}`);
    });
  }, [operation, refresh]);

  if (!operation && !error) return null;
  if (operation && dismissed === operation.id && operation.state !== "running") return null;
  const running = operation?.state === "running";
  const output = operation ? lastLine(operation.stderr_tail) || lastLine(operation.stdout_tail) : "";
  return (
    <div className="opbar" role="status" aria-live="polite">
      {operation ? (
        <>
          {running ? <Spinner className="size-3.5 text-muted-foreground" /> : <Icon name={operation.state === "finished" ? "check" : operation.state === "stopped" ? "stop" : "x"} size={13} className={STATE_TONE[operation.state]} />}
          <span className={cn("opbar-state", STATE_TONE[operation.state])}>{running && operation.stop_requested ? "Stopping" : STATE_WORD[operation.state]}</span>
          <span className="opbar-name truncate">{operation.scenario}</span>
          {operation.exit_code !== null && <span className="meta">Exit code {operation.exit_code}</span>}
          <Popover>
            <PopoverTrigger asChild>
              <button type="button" className="opbar-tail mono truncate" title="Show the output">
                {output || (running ? "Waiting for output" : "No output")}
              </button>
            </PopoverTrigger>
            <PopoverContent align="start" className="opbar-pop w-[min(720px,90vw)]">
              <pre className="json">{[operation.stdout_tail, operation.stderr_tail].filter(Boolean).join("\n") || "No output yet."}</pre>
            </PopoverContent>
          </Popover>
          {error && <span className="text-fail truncate">{error}</span>}
          <span className="grow" />
          {operation.run_id && !running && (
            <Button variant="outline" size="xs" onClick={() => go(href.run(operation.run_id!))}>
              Open run
            </Button>
          )}
          {running ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="outline" size="xs" disabled={operation.stop_requested} onClick={() => void stopRun()}>
                  <Icon name="stop" size={11} />
                  Stop
                </Button>
              </TooltipTrigger>
              <TooltipContent>Stops the run; what it wrote so far stays as an incomplete run</TooltipContent>
            </Tooltip>
          ) : (
            <Button variant="ghost" size="icon-xs" aria-label="Dismiss" onClick={dismissOperation}>
              <Icon name="x" />
            </Button>
          )}
        </>
      ) : (
        <>
          <Icon name="x" size={13} className="text-fail" />
          <span className="text-fail truncate">{error}</span>
          <span className="grow" />
          <Button variant="ghost" size="icon-xs" aria-label="Dismiss" onClick={dismissOperation}>
            <Icon name="x" />
          </Button>
        </>
      )}
    </div>
  );
}
