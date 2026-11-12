import { useMemo, useState } from "react";
import type { RunSummary, ScenarioSummary } from "../api/types";
import { useCatalog } from "../lib/api";
import { go, href } from "../lib/router";
import { isVariant, runStatus, summaryStatus } from "../lib/run";
import { agentKinds, agentWord, lastStatus, type ScenarioView } from "../lib/views";
import { Icon, serviceIcon } from "../components/Icon";
import { FilterMenu, type FilterValue } from "../components/FilterMenu";
import { RunButton } from "../components/OperationBar";
import { StatusWord, TrialsSummary } from "../components/Status";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const RECENT = 5;

const date = (iso: string | undefined) => (iso ? iso.slice(5, 10).replace("-", "/") : "");

const COLUMN_WIDTHS = [220, 96, 120, 150, 150, 76, 84, 82, 60, 60, 120, 70, 48];

export function Scenarios({ view }: { view: ScenarioView }) {
  const { scenarios, runs } = useCatalog();
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<FilterValue>({});

  const groups = useMemo(
    () => [
      {
        id: "verdict",
        label: "Last run",
        options: [
          { id: "pass", label: "Passed" },
          { id: "fail", label: "Failed" },
          { id: "unknown", label: "Not graded" },
          { id: "never", label: "Never run" },
        ],
      },
      { id: "agent", label: "Agent", options: agentKinds(scenarios).map((kind) => ({ id: kind, label: agentWord(kind) })) },
      {
        id: "tasks",
        label: "Tasks",
        options: [
          { id: "tasks", label: "Has tasks" },
          { id: "single", label: "Single request" },
        ],
      },
    ],
    [scenarios],
  );

  const recent = useMemo(() => {
    const by = new Map<string, RunSummary[]>();
    for (const run of runs) if (run.scenario_id && !isVariant(run)) by.set(run.scenario_id, [...(by.get(run.scenario_id) ?? []), run]);
    return by;
  }, [runs]);

  const rows = useMemo(() => {
    const picked = (g: string) => filter[g] ?? [];
    const verdictKey = (s: ScenarioSummary) => {
      const status = lastStatus(s);
      return !status ? "never" : status === "pass" || status === "fail" ? status : "unknown";
    };
    return scenarios
      .filter(view.test)
      .filter((s) => !q || `${s.id} ${s.scenario_id ?? ""} ${s.path}`.toLowerCase().includes(q.toLowerCase()))
      .filter((s) => !picked("verdict").length || picked("verdict").includes(verdictKey(s)))
      .filter((s) => !picked("agent").length || picked("agent").includes(s.agent_kind ?? "unknown"))
      .filter((s) => !picked("tasks").length || picked("tasks").includes(s.task_count > 0 ? "tasks" : "single"))
      .sort((a, b) => a.id.localeCompare(b.id));
  }, [scenarios, q, filter, view]);

  return (
    <div className="scenarios">
      <div className="toolbar">
        <h1>{view.kind === "view" && view.id === "all" ? "Scenarios" : view.label}</h1>
        <span className="count tabular-nums">{rows.length}</span>
        <InputGroup className="h-7 w-56 shrink-0 bg-card shadow-sm">
          <InputGroupAddon>
            <Icon name="search" />
          </InputGroupAddon>
          <InputGroupInput value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a scenario" aria-label="Find a scenario" />
        </InputGroup>
        <FilterMenu groups={groups} value={filter} onChange={setFilter} />
      </div>
      {runs.some((run) => run.adapter_kind === "historical_import") && (
        <div className="p-4 border-b border-border">
          <h2 className="font-medium">Your real research runs</h2>
          <p className="text-sm text-muted-foreground">Historical Arden sessions. Original messages and tool evidence; ungraded.</p>
          <div className="flex flex-wrap gap-4 mt-2">
            {runs.filter((run) => run.adapter_kind === "historical_import").map((run) => (
              <a key={run.id} className="link text-sm" href={href.run(run.id)}>{run.name.replaceAll("-", " ")}</a>
            ))}
          </div>
        </div>
      )}
      <div className="table-wrap">
        <Table className="scenarios-table table-fixed">
          <colgroup>
            {COLUMN_WIDTHS.map((width, i) => (
              <col key={i} style={{ width }} />
            ))}
          </colgroup>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="scenarios-pinned">Scenario</TableHead>
              <TableHead>Last run</TableHead>
              <TableHead>Last {RECENT} runs</TableHead>
              <TableHead>Agent</TableHead>
              <TableHead>Services</TableHead>
              <TableHead className="text-right">Outcome</TableHead>
              <TableHead className="text-right">Collateral</TableHead>
              <TableHead className="text-right">Forbidden</TableHead>
              <TableHead className="text-right">Claims</TableHead>
              <TableHead className="text-right">Tasks</TableHead>
              <TableHead>Validity</TableHead>
              <TableHead className="text-right">Updated</TableHead>
              <TableHead aria-label="Run" className="scenarios-pinned-end" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((s) => {
              const mine = recent.get(s.scenario_id ?? "") ?? [];
              const last = s.last_run;
              return (
                <TableRow key={s.id} className="cursor-pointer" onClick={() => go(href.scenario(s.id))}>
                  <TableCell className="scenarios-pinned">
                    <div className="scenario-name">
                      <span className="truncate" title={s.path}>
                        {s.id}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell>
                    {last ? (
                      <button
                        type="button"
                        className="trials-link"
                        onClick={(e) => {
                          e.stopPropagation();
                          go(href.run(last.id));
                        }}
                      >
                        <StatusWord status={runStatus(last.verdict)} />
                      </button>
                    ) : (
                      <span className="text-muted-foreground">Never run</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <TrialsSummary trials={mine.slice(0, RECENT).map(summaryStatus)} />
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    <span className="truncate">{agentWord(s.agent_kind)}</span>
                  </TableCell>
                  <TableCell>
                    <div className="svc-icons">
                      {s.service_kinds.slice(0, 6).map((kind) => (
                        <Tooltip key={kind}>
                          <TooltipTrigger asChild>
                            <span className="svc">
                              <Icon name={serviceIcon(kind)} size={13} />
                            </span>
                          </TooltipTrigger>
                          <TooltipContent>{kind}</TooltipContent>
                        </Tooltip>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{s.check_counts.outcomes ?? 0}</TableCell>
                  <TableCell className="text-right tabular-nums">{s.check_counts.collateral ?? 0}</TableCell>
                  <TableCell className="text-right tabular-nums">{s.check_counts.forbidden ?? 0}</TableCell>
                  <TableCell className="text-right tabular-nums">{s.check_counts.claims ?? 0}</TableCell>
                  <TableCell className="text-right tabular-nums text-muted-foreground">{s.task_count || "None"}</TableCell>
                  <TableCell>
                    <span className={`sound truncate ${s.valid ? "s-sound" : "s-blocked"}`} title={s.error ?? undefined}>
                      <Icon name={s.valid ? "check" : "x"} size={12} />
                      {s.valid ? "Valid" : `Invalid: ${s.error ?? "unknown error"}`}
                    </span>
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground tabular-nums">{date(last?.modified)}</TableCell>
                  <TableCell className="scenarios-pinned-end text-right">
                    <RunButton scenario={s} compact />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
