import { useEffect, useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import type { ComparedCheck, RunComparison, RunDetail, RunSummary, TraceStep } from "../api/types";
import { runPath, runsOf, useCatalog, useEntries, useResource } from "../lib/api";
import { go, href } from "../lib/router";
import { chipsFor, distinctTimes, isVariant, LANES, modelName, rowsFor, runStatus, runTitle, summaryStatus } from "../lib/run";
import { clock, fitScale } from "../lib/time";
import { useWidth } from "../lib/useWidth";
import { LAYERS, STATUS_WORD } from "../lib/verdict";
import type { Chip, RowDef } from "../types";
import { Picker } from "../components/Picker";
import { StatusBadge, StatusWord } from "../components/Status";
import { GUTTER, RULER, Timeline } from "../components/Timeline";
import { ToolCall } from "../components/ToolCall";
import { Unavailable } from "../components/Unavailable";

const FIELD_WORD: Record<string, string> = { kind: "step", actor: "actor", operation: "operation", args: "arguments", result: "result", presence: "presence" };
const SIDES = ["A", "B"] as const;

export function Compare({ left, right }: { left: string; right: string }) {
  const a = useResource<RunDetail>(runPath(left));
  const b = useResource<RunDetail>(runPath(right));
  const diff = useResource<RunComparison>(`/api/compare?a=${encodeURIComponent(left)}&b=${encodeURIComponent(right)}`);
  const failed = [a, b, diff].find((r) => r.error && !r.data);
  if (failed) return <Unavailable what="the comparison" error={failed.error} onRetry={failed.reload} />;
  if (!a.data || !b.data || !diff.data) return <p className="empty pad">Loading the comparison…</p>;
  return <CompareView a={a.data} b={b.data} diff={diff.data} />;
}

const stepText = (step: TraceStep | null | undefined) => (step ? `${step.label} at ${clock(step.time ?? 0)}` : "nothing");

function differenceLine(diff: RunComparison): string {
  const d = diff.divergence;
  if (!d) return `No difference in ${diff.compared_entries} steps.`;
  if (d.fields.includes("presence")) return `Run ${d.a ? "A" : "B"} goes on after step ${d.position}: ${stepText(d.a ?? d.b)}.`;
  const fields = d.fields.map((f) => FIELD_WORD[f] ?? f).join(" and ");
  return `First difference at step ${d.position + 1}, in the ${fields}: A has ${stepText(d.a)}, B has ${stepText(d.b)}.`;
}

function tagged(run: RunDetail, side: (typeof SIDES)[number]) {
  const chips = chipsFor(run, { views: false, task: null }).map((c) => ({ ...c, id: `${side}${c.id}`, row: `${side}:${c.row}` }));
  const rows = rowsFor(run, chipsFor(run, { views: false, task: null })).map((r) => ({ ...r, id: `${side}:${r.id}`, tag: side }));
  return { chips, rows };
}

function mergeRows(a: RowDef[], b: RowDef[]): RowDef[] {
  const out: RowDef[] = [];
  for (const lane of LANES) {
    const ids = [...new Set([...a, ...b].filter((r) => r.lane === lane).map((r) => r.id.slice(2)))];
    for (const id of ids) for (const r of [a, b]) out.push(...r.filter((row) => row.lane === lane && row.id.slice(2) === id));
  }
  return out;
}

function CompareView({ a, b, diff }: { a: RunDetail; b: RunDetail; diff: RunComparison }) {
  const { runs } = useCatalog();
  const [box, avail] = useWidth<HTMLDivElement>();
  const [sel, setSel] = useState<Chip | null>(null);
  const left = useMemo(() => tagged(a, "A"), [a]);
  const right = useMemo(() => tagged(b, "B"), [b]);
  const chips = useMemo(() => [...left.chips, ...right.chips], [left, right]);
  const rows = useMemo(() => mergeRows(left.rows, right.rows), [left, right]);
  const times = useMemo(() => distinctTimes(chips), [chips]);
  const scale = useMemo(() => fitScale(times, times.at(-1) ?? 0, avail), [times, avail]);
  const d = diff.divergence;
  const marked = useMemo(() => {
    const ids = new Set<string>();
    for (const [side, step] of [
      ["A", d?.a],
      ["B", d?.b],
    ] as const)
      if (step) for (const chip of side === "A" ? left.chips : right.chips) if (chip.lines.includes(step.line)) ids.add(chip.id);
    return ids;
  }, [d, left, right]);
  const selRun = sel?.id.startsWith("B") ? b.id : a.id;
  const { entries, error } = useEntries(selRun, sel?.lines ?? null);
  const diffTimes = [d?.a?.time, d?.b?.time].filter((t): t is number => typeof t === "number");
  const divergeAt = diffTimes.length ? Math.min(...diffTimes) : undefined;
  useEffect(() => {
    const chip = [...(box.current?.querySelectorAll<HTMLElement>(".chip.marked") ?? [])].at(-1);
    const scroller = chip?.closest<HTMLElement>(".tl-scroll");
    if (!chip || !scroller) return;
    const c = chip.getBoundingClientRect();
    const s = scroller.getBoundingClientRect();
    const top = s.top + RULER;
    const left = s.left + GUTTER;
    if (c.bottom > s.bottom) scroller.scrollTop += c.bottom - s.bottom + 12;
    else if (c.top < top) scroller.scrollTop -= top - c.top + 12;
    if (c.right > s.right) scroller.scrollLeft += c.right - s.right + 24;
    else if (c.left < left) scroller.scrollLeft -= left - c.left + 24;
  }, [marked, box, scale]);

  const summaryA = runs.find((r) => r.id === a.id);
  const summaryB = runs.find((r) => r.id === b.id);
  const choices = [...new Map([...runsOf(runs, a.scenario_id), ...runsOf(runs, b.scenario_id), ...[summaryA, summaryB].filter((r): r is RunSummary => !!r)].map((r) => [r.id, r])).values()];
  const options = choices.map((r) => ({ value: r.id, label: isVariant(r) ? `${runTitle(r)} variant` : runTitle(r), meta: STATUS_WORD[summaryStatus(r)] }));

  return (
    <div className="compare viewer" ref={box}>
      <div className="pagehead">
        <h1>Compare</h1>
        {SIDES.map((side) => {
          const run = side === "A" ? a : b;
          return (
            <span key={side} className="cmp-pick">
              <span className="tl-tag">{side}</span>
              <Picker label={`Run ${side}`} value={run.id} className="h-7 w-60" onChange={(id) => go(side === "A" ? href.compare(id, b.id) : href.compare(a.id, id))} groups={[{ options }]} />
              <StatusBadge status={runStatus(run.verdict, run.verdict !== null)} />
              <span className="meta truncate">{modelName(run.provenance?.model, run.provenance?.adapter_kind ?? run.agent_kind)?.replace(/_/g, " ")}</span>
            </span>
          );
        })}
      </div>
      <p className={cn("summary truncate", d && "cmp-diverged")} title={differenceLine(diff)}>
        {differenceLine(diff)}
      </p>
      <Timeline rows={rows} chips={chips} scale={scale} playhead={divergeAt} label={d ? "First difference" : "Time"} dimFuture={false} selection={sel?.id} onSelect={setSel} marked={marked} />
      {sel && (
        <div className="viewer-selected">
          <div className="selected-tool">
            <ToolCall key={sel.id} chip={sel} entries={entries} error={error} open onOpenChange={(o) => !o && setSel(null)} onSeek={() => undefined} />
          </div>
        </div>
      )}
      <div className="viewer-bottom cmp-bottom">
        <section className="pane">
          <CheckTable checks={diff.checks} a={a} b={b} />
        </section>
      </div>
    </div>
  );
}

const word = (check: ComparedCheck, side: "a" | "b") => {
  if (!(side === "a" ? check.in_a : check.in_b)) return <span className="muted">Not in this run</span>;
  const passed = side === "a" ? check.a : check.b;
  return <StatusWord status={passed === true ? "pass" : passed === false ? "fail" : "unknown"} />;
};

function CheckTable({ checks, a, b }: { checks: ComparedCheck[]; a: RunDetail; b: RunDetail }) {
  const differing = checks.filter((c) => c.differs).length;
  const changed = checks.filter((c) => c.definition_changed).length;
  const hasTasks = checks.some((c) => c.task_id);
  const counts = [differing && `${differing} differ`, changed && `${changed} ${changed === 1 ? "check" : "checks"} changed between the runs`].filter(Boolean);
  return (
    <>
      <h2 className="pane-title">
        Checks <span className="count ml-2">{counts.length ? counts.join(" · ") : "Same results"}</span>
      </h2>
      <table className="dtable cmp-checks">
        <colgroup>
          <col />
          <col style={{ width: 96 }} />
          {hasTasks && <col style={{ width: 140 }} />}
          <col style={{ width: 120 }} />
          <col style={{ width: 120 }} />
        </colgroup>
        <thead>
          <tr>
            <th>Check</th>
            <th>Layer</th>
            {hasTasks && <th>Task</th>}
            <th title={a.id}>Run A</th>
            <th title={b.id}>Run B</th>
          </tr>
        </thead>
        <tbody>
          {checks.map((c) => (
            <tr key={`${c.task_id ?? ""}-${c.layer}-${c.index}`} className={cn((c.differs || c.definition_changed) && "cmp-differs")}>
              <td className="truncate" title={c.definition_changed ? `${c.text}. Defined differently in the two runs, so the results do not compare.` : c.text}>
                {c.definition_changed && <span className="cmp-changed">Changed</span>}
                {c.text}
              </td>
              <td className="muted">{LAYERS.find((l) => l.id === c.layer)?.name ?? c.layer}</td>
              {hasTasks && <td className="mono truncate">{c.task_id ?? ""}</td>}
              <td>{word(c, "a")}</td>
              <td>{word(c, "b")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
