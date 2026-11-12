import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { RunDetail } from "../api/types";
import { runPath, runsOf, scenarioOfRun, useCatalog, useResource } from "../lib/api";
import { openPeek, usePeek } from "../lib/peek";
import { go, href, SECTIONS, type Route, type Section } from "../lib/router";
import { isVariant, runTitle, servicesOf, summaryStatus } from "../lib/run";
import { readStorageIfAllowed, writeStorageIfAllowed } from "../lib/tolerant";
import { agentViews, SHAPE_VIEWS, useRecent, VIEWS, viewHref, visit, type ScenarioView } from "../lib/views";
import { STATUS_ICON, STATUS_TONE, STATUS_WORD } from "../lib/verdict";
import { Hint } from "./Hint";
import { Icon, serviceIcon } from "./Icon";
import { Picker } from "./Picker";

const VIEW_ICON: Record<string, string> = { all: "records", failing: "x", passing: "check", ungraded: "dash", never: "clock", invalid: "lock" };

const SECTION_META: Record<Section, { label: string; icon: string }> = {
  services: { label: "Services", icon: "compute" },
  events: { label: "Events", icon: "outage" },
  people: { label: "People", icon: "user" },
  operator: { label: "Operator and agent", icon: "shield" },
  checks: { label: "Checks", icon: "checks" },
};

const RUN_LIMIT = 12;

function readCollapsed(): boolean {
  return readStorageIfAllowed("overnight-sidebar") === "collapsed";
}

function Item({ href: to, onClick, icon, label, on, collapsed, trailing, className }: { href?: string; onClick?: () => void; icon?: React.ReactNode; label: string; on?: boolean; collapsed: boolean; trailing?: React.ReactNode; className?: string }) {
  const body = (
    <>
      {icon}
      {!collapsed && <span className="truncate">{label}</span>}
      {!collapsed && trailing}
    </>
  );
  const cls = cn("snav-item", on && "on", className);
  const el = to ? (
    <a href={to} className={cls} aria-current={on ? "page" : undefined} aria-label={collapsed ? label : undefined}>
      {body}
    </a>
  ) : (
    <button type="button" className={cls} aria-pressed={on} aria-label={collapsed ? label : undefined} onClick={onClick}>
      {body}
    </button>
  );
  return collapsed ? <Hint label={label}>{el}</Hint> : el;
}

export function Sidebar({ route, view, theme, onTheme }: { route: Route; view: ScenarioView; theme: string; onTheme: () => void }) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  useEffect(() => {
    writeStorageIfAllowed("overnight-sidebar", collapsed ? "collapsed" : "open");
  }, [collapsed]);
  const { scenarios, runs } = useCatalog();
  const peek = usePeek();
  const recent = useRecent();
  const runId = route.page === "run" ? route.id : null;
  const run = useResource<RunDetail>(runId ? runPath(runId) : null).data;
  const compareRun = runs.find((r) => route.page === "compare" && r.id === route.a);
  const scenario =
    route.page === "scenario"
      ? scenarios.find((s) => s.id === route.id)
      : scenarioOfRun(scenarios, route.page === "run" ? (run?.scenario_id ?? runs.find((r) => r.id === runId)?.scenario_id) : compareRun?.scenario_id);
  const scenarioRuns = runsOf(runs, scenario?.scenario_id ?? (route.page === "run" ? run?.scenario_id : undefined));
  const inside = route.page !== "scenarios";
  useEffect(() => {
    if (scenario) visit(scenario.id);
  }, [scenario]);
  const recentOthers = recent
    .filter((id) => id !== scenario?.id)
    .map((id) => scenarios.find((s) => s.id === id))
    .filter((s): s is NonNullable<typeof s> => !!s)
    .slice(0, 4);
  const counts: Partial<Record<Section, number>> = scenario
    ? {
        services: scenario.service_kinds.length,
        people: scenario.people,
        checks: (scenario.check_counts.outcomes ?? 0) + (scenario.check_counts.collateral ?? 0) + (scenario.check_counts.claims ?? 0) + (scenario.check_counts.forbidden ?? 0),
      }
    : {};
  const apps = run ? servicesOf(run.scenario) : [];
  const count = (v: ScenarioView) => scenarios.filter(v.test).length;
  const viewOn = (v: ScenarioView) => route.page === "scenarios" && v.kind === view.kind && v.id === view.id;

  return (
    <aside className={cn("snav", collapsed && "collapsed")} aria-label="Navigation">
      <div className="snav-head">
        <a href="#/scenarios" className="brand" aria-label="Overnight">
          <Icon name="moon" size={15} />
          {!collapsed && "Overnight"}
        </a>
        <Hint label={collapsed ? "Open the sidebar" : "Collapse the sidebar"}>
          <Button variant="ghost" size="icon-xs" className="snav-collapse" aria-label={collapsed ? "Open the sidebar" : "Collapse the sidebar"} onClick={() => setCollapsed(!collapsed)}>
            <Icon name={collapsed ? "right" : "left"} />
          </Button>
        </Hint>
      </div>

      <div className="snav-scroll">
        {!inside ? (
          <>
            <div className="snav-group">
              {VIEWS.map((v) => (
                <Item key={v.id} href={viewHref(v)} icon={<Icon name={VIEW_ICON[v.id]} />} label={v.label} on={viewOn(v)} collapsed={collapsed} trailing={<span className="snav-count">{count(v)}</span>} />
              ))}
            </div>
            {!collapsed && (
              <>
                <div className="snav-group">
                  <h3 className="snav-h">Agent</h3>
                  {agentViews(scenarios).map((v) => (
                    <Item key={v.id} href={viewHref(v)} label={v.label} on={viewOn(v)} collapsed={collapsed} trailing={<span className="snav-count">{count(v)}</span>} />
                  ))}
                </div>
                <div className="snav-group">
                  <h3 className="snav-h">Shape</h3>
                  {SHAPE_VIEWS.map((v) => (
                    <Item key={v.id} href={viewHref(v)} label={v.label} on={viewOn(v)} collapsed={collapsed} trailing={<span className="snav-count">{count(v)}</span>} />
                  ))}
                </div>
              </>
            )}
          </>
        ) : (
          <div className="snav-group">
            <Item href="#/scenarios" icon={<Icon name="records" />} label="All scenarios" collapsed={collapsed} />
          </div>
        )}

        {inside && scenario && (
          <>
            <div className="snav-group">
              {!collapsed && (
                <Picker
                  label="Scenario"
                  value={scenario.id}
                  className="snav-scenario w-full"
                  onChange={(id) => go(href.scenario(id, route.page === "scenario" ? route.section : "services"))}
                  groups={[{ options: scenarios.map((s) => ({ value: s.id, label: s.id })) }]}
                />
              )}
              {SECTIONS.map((s) => (
                <Item
                  key={s}
                  href={href.scenario(scenario.id, s)}
                  icon={<Icon name={SECTION_META[s].icon} />}
                  label={SECTION_META[s].label}
                  on={route.page === "scenario" && route.section === s}
                  collapsed={collapsed}
                  trailing={counts[s] !== undefined && <span className="snav-count">{counts[s]}</span>}
                />
              ))}
            </div>
          </>
        )}

        {inside && scenarioRuns.length > 0 && (
          <div className="snav-group">
            {!collapsed && <h3 className="snav-h">Runs</h3>}
            {scenarioRuns.slice(0, RUN_LIMIT).map((r) => {
              const status = summaryStatus(r);
              return (
                <Item
                  key={r.id}
                  href={href.run(r.id)}
                  icon={collapsed ? <Icon name={STATUS_ICON[status]} className={STATUS_TONE[status]} /> : undefined}
                  label={runTitle(r)}
                  on={runId === r.id}
                  collapsed={collapsed}
                  trailing={
                    <>
                      {isVariant(r) && (
                        <Hint label={r.has_variation ? "Variation run" : "Run with overrides"}>
                          <span className="snav-variant">
                            <Icon name="variant" size={12} />
                          </span>
                        </Hint>
                      )}
                      <span className={cn("snav-status", STATUS_TONE[status])}>{STATUS_WORD[status]}</span>
                    </>
                  }
                />
              );
            })}
            {scenarioRuns.length > RUN_LIMIT && !collapsed && <p className="snav-empty">{scenarioRuns.length - RUN_LIMIT} older runs</p>}
            {scenarioRuns.length >= 2 && (
              <Item
                href={href.compare(runId ?? scenarioRuns[0].id, scenarioRuns.find((r) => r.id !== (runId ?? scenarioRuns[0].id))!.id)}
                icon={<Icon name="compare" />}
                label="Compare two runs"
                on={route.page === "compare"}
                collapsed={collapsed}
                className="mt-1"
              />
            )}
          </div>
        )}

        {apps.length > 0 && (
          <div className="snav-group">
            {!collapsed && <h3 className="snav-h">Services at this moment</h3>}
            {apps.map((a) => (
              <Item key={a.id} onClick={() => openPeek(peek === a.id ? null : a.id)} icon={<Icon name={serviceIcon(a.kind)} className="snav-app-icon" />} label={a.id} on={peek === a.id} collapsed={collapsed} />
            ))}
          </div>
        )}
      </div>

      {!collapsed && recentOthers.length > 0 && (
        <div className="snav-recent">
          <h3 className="snav-h">Recent</h3>
          {recentOthers.map((s) => (
            <Item key={s.id} href={href.scenario(s.id)} label={s.id} collapsed={collapsed} />
          ))}
        </div>
      )}
      <div className="snav-foot">
        <Item onClick={onTheme} icon={<Icon name={theme === "dark" ? "sun" : "moon"} />} label={theme === "dark" ? "Light theme" : "Dark theme"} collapsed={collapsed} />
      </div>
    </aside>
  );
}
