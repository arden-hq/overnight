import { useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Kbd } from "@/components/ui/kbd";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { RunDetail } from "../api/types";
import { runPath, runsOf, scenarioOfRun, stateAt, useCatalog, useEntries, useResource, useSnapshots } from "../lib/api";
import { useOperation } from "../lib/operation";
import { openPeek, usePeek } from "../lib/peek";
import { go, href } from "../lib/router";
import { actorsOf, arr, chipsFor, distinctTimes, isVariant, modelName, obj, rowsFor, runStatus, runTitle, servicesOf } from "../lib/run";
import { isPlainKey } from "../lib/keys";
import { clock } from "../lib/time";
import { useWidth } from "../lib/useWidth";
import { useZoom } from "../lib/zoom";
import type { Chip } from "../types";
import { AppWindow } from "../components/AppWindow";
import { Hint } from "../components/Hint";
import { Icon } from "../components/Icon";
import { PersonaCard } from "../components/PersonaCard";
import { Picker } from "../components/Picker";
import { StatusBadge } from "../components/Status";
import { Timeline } from "../components/Timeline";
import { ToolCall } from "../components/ToolCall";
import { TaskList, VerdictPanel } from "../components/Verdict";
import { failureLabel, verdictLine } from "../lib/verdict";
import { Unavailable } from "../components/Unavailable";

let peekOpenedOnce = false;
const PEEK_OVERLAYS = "(max-width: 1280px)";

const PLAY_PX_PER_S = 200;

export function Viewer({ runId }: { runId: string }) {
  const { data, error, reload } = useResource<RunDetail>(runPath(runId));
  const { operation } = useOperation();
  const watched = operation?.run_id === runId ? operation : null;
  useEffect(() => {
    if (watched) reload();
  }, [watched, reload]);
  if (error && !data) return <Unavailable what={`run ${runId}`} error={error} onRetry={reload} />;
  if (!data) return <p className="empty pad">Loading the run…</p>;
  return <RunView key={data.id} run={data} live={watched?.state === "running"} />;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function pendingText(run: RunDetail): string | undefined {
  const p = run.pending;
  if (!p) return undefined;
  const parts = [
    arr(p.events).length && plural(arr(p.events).length, "event"),
    arr(p.operator_steps).length && plural(arr(p.operator_steps).length, "operator step"),
    arr(p.approvals).length && plural(arr(p.approvals).length, "approval"),
  ].filter(Boolean);
  return parts.length ? `Still pending when the run ended: ${parts.join(", ").replace(/, ([^,]*)$/, " and $1")}.` : undefined;
}

function RunView({ run, live }: { run: RunDetail; live: boolean }) {
  const { scenarios, runs } = useCatalog();
  const summary = runs.find((r) => r.id === run.id);
  const scenario = scenarioOfRun(scenarios, run.scenario_id);
  const siblings = runsOf(runs, run.scenario_id).filter((r) => r.id !== run.id);
  const [views, setViews] = useState(false);
  const [task, setTask] = useState<string | null>(null);
  const chips = useMemo(() => chipsFor(run, { views, task }), [run, views, task]);
  const rows = useMemo(() => rowsFor(run, chips), [run, chips]);
  const times = useMemo(() => distinctTimes(chips), [chips]);
  const end = times.at(-1) ?? 0;
  const horizon = typeof obj(run.scenario).horizon === "number" ? (obj(run.scenario).horizon as number) : undefined;
  const [t, setT] = useState(end);
  const [follow, setFollow] = useState(live);
  const [wasLive, setWasLive] = useState(live);
  if (live !== wasLive) {
    setWasLive(live);
    if (live) setFollow(true);
  }
  if (follow && t !== end) setT(end);
  const [playing, setPlaying] = useState(false);
  const [glide, setGlide] = useState(false);
  const [sel, setSel] = useState<Chip | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const peek = usePeek();
  const services = useMemo(() => servicesOf(run.scenario), [run]);
  const people = useMemo(() => actorsOf(run.scenario).filter((a) => a.role === "person"), [run]);
  const { snapshots, error: snapshotError } = useSnapshots(run.id, run.timeline.length);
  const { entries, error: entryError } = useEntries(run.id, sel?.lines ?? null);
  const [peekW, setPeekW] = useState(460);
  const [box, avail] = useWidth<HTMLDivElement>();

  useEffect(() => {
    if (peek === null && !peekOpenedOnce && services[0] && !window.matchMedia(PEEK_OVERLAYS).matches) {
      peekOpenedOnce = true;
      openPeek(services[0].id);
    }
  }, [peek, services]);

  const { scale, zoom } = useZoom(times, end, avail);
  const [query, setQuery] = useState("");
  const findBox = useRef<HTMLInputElement>(null);
  const ordered = useMemo(() => [...chips].sort((a, b) => a.t - b.t || a.sequence - b.sequence), [chips]);
  const matches = useMemo(() => {
    const words = query.trim().toLowerCase();
    if (!words) return undefined;
    return new Set(ordered.filter((c) => `${c.label} ${c.mark ?? ""} ${c.row} ${c.kind.replace(/_/g, " ")}`.toLowerCase().includes(words)).map((c) => c.id));
  }, [ordered, query]);
  const byLine = useMemo(() => {
    const map = new Map<number, Chip>();
    for (const chip of chips) for (const line of chip.lines) map.set(line, chip);
    return map;
  }, [chips]);

  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);
  useEffect(() => {
    if (!playing) return;
    const root = box.current;
    const head = root?.querySelector<HTMLElement>(".tl-playhead");
    const label = head?.querySelector("span");
    const clockEl = root?.querySelector(".transport-clock .mono");
    let x = scale.x(tRef.current);
    let last = performance.now();
    let committed = last;
    let raf = 0;
    const step = (now: number) => {
      x += (PLAY_PX_PER_S * Math.min(64, now - last)) / 1000;
      last = now;
      const tt = scale.t(x);
      if (tt >= end) {
        setT(end);
        setPlaying(false);
        return;
      }
      if (head) head.style.transform = `translateX(${x + 2}px)`;
      if (label) label.textContent = clock(tt);
      if (clockEl) clockEl.textContent = clock(tt);
      if (times.some((et) => et > tRef.current && et <= tt) || now - committed > 250) {
        committed = now;
        tRef.current = tt;
        setT(tt);
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, scale, end, times, box]);

  const showServiceOf = (chip: Chip) => {
    const coversTheTimeline = window.matchMedia(PEEK_OVERLAYS).matches;
    if (chip.service && services.some((s) => s.id === chip.service) && (peek !== null || !coversTheTimeline)) openPeek(chip.service);
  };
  const seekTo = (to: number) => {
    setFollow(false);
    setPlaying(false);
    setGlide(true);
    setT(to);
  };
  const seek = (chip: Chip) => {
    seekTo(chip.t);
    setSel(chip);
    setFlash(chip.id);
    setTimeout(() => setFlash(null), 1200);
    showServiceOf(chip);
  };
  const stepItem = (direction: 1 | -1) => {
    const list = matches ? ordered.filter((c) => matches.has(c.id)) : ordered;
    const at = sel ? list.findIndex((c) => c.id === sel.id) : -1;
    const next = at >= 0 ? list[at + direction] : direction > 0 ? (list.find((c) => c.t > t) ?? list[0]) : (list.findLast((c) => c.t <= t) ?? list.at(-1));
    if (next) seek(next);
  };
  const keys = useRef({ stepItem });
  useEffect(() => {
    keys.current = { stepItem };
  });

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (!isPlainKey(e)) return;
      if (e.key === "ArrowRight" || e.key === "ArrowLeft" || e.key === " ") setFollow(false);
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") setGlide(true);
      if (e.key === "ArrowRight") setT((c) => times.find((x) => x > c) ?? c);
      if (e.key === "ArrowLeft") setT((c) => [...times].reverse().find((x) => x < c) ?? 0);
      if (e.key === ".") keys.current.stepItem(1);
      if (e.key === ",") keys.current.stepItem(-1);
      if (e.key === "Escape") setSel(null);
      if (e.key === "/") {
        e.preventDefault();
        findBox.current?.focus();
      }
      if (e.key === " ") {
        e.preventDefault();
        setPlaying((p) => !p);
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [times]);

  const selectedTask = task ? run.tasks.find((x) => x.task_id === task) : undefined;
  const statusOf = (verdict: string | null, complete: boolean) => (live && !verdict ? "running" : runStatus(verdict, complete));
  const status = selectedTask ? statusOf(selectedTask.verdict, selectedTask.status !== null) : statusOf(run.verdict, run.verdict !== null);
  const checks = selectedTask ? selectedTask.checks : run.checks;
  const request = run.timeline.find((item) => item.kind === "operator_message" && (!task || item.task_id === task))?.label;
  const model = modelName(run.provenance?.model, run.provenance?.adapter_kind ?? run.agent_kind);
  const current = services.find((s) => s.id === peek);
  const shown = current ? stateAt(snapshots, current.id, current.state, t) : undefined;
  const lastOf = (actor: string) => {
    const mine = chips.filter((c) => (c.lane === "people" || c.lane === "tools") && c.row === actor && c.t <= t);
    return mine.length ? { t: Math.max(...mine.map((c) => c.t)), count: mine.length } : null;
  };

  return (
    <div className={`viewer-frame${current ? " peeking" : ""}`} style={{ ["--peek-w" as string]: `${peekW}px` }}>
      <div className="viewer" ref={box}>
        <div className="pagehead">
          <h1 className="truncate">{summary ? runTitle(summary) : run.id}</h1>
          <StatusBadge status={statusOf(run.verdict, run.verdict !== null)} />
          {model && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="secondary" className="font-medium">
                  {model.replace(/_/g, " ")}
                </Badge>
              </TooltipTrigger>
              <TooltipContent className="flex flex-col items-start gap-0.5">
                <span>{run.provenance?.adapter_identity ?? `Agent kind ${run.agent_kind ?? "unknown"}`}</span>
                {run.provenance?.backend_commit && <span className="mono">{run.provenance.backend_commit.slice(0, 12)}</span>}
                {run.provenance?.connector_status && <span>Connector {run.provenance.connector_status.replace(/_/g, " ")}</span>}
              </TooltipContent>
            </Tooltip>
          )}
          {summary && isVariant(summary) && <Badge variant="outline">Variant</Badge>}
          {run.provenance?.seed !== null && run.provenance?.seed !== undefined && (
            <span className="meta">
              Seed <span className="mono">{run.provenance.seed}</span>
            </span>
          )}
          {scenario && (
            <a className="meta link truncate" href={href.scenario(scenario.id)}>
              {scenario.id}
            </a>
          )}
          <span className="grow" />
          {siblings.length > 0 && (
            <Button variant="outline" size="sm" className="shadow-sm" onClick={() => go(href.compare(run.id, siblings[0].id))}>
              <Icon name="compare" />
              Compare
            </Button>
          )}
        </div>

        {run.provenance?.adapter_kind === "historical_import" && (
          <div className="banner notice"><Icon name="lock" /><span>Historical Arden session · Original elapsed time · Ungraded · World state unavailable</span></div>
        )}
        {!run.schema_current && run.provenance?.adapter_kind !== "historical_import" && (
          <div className="banner notice">
            <Icon name="lock" />
            <span className="truncate" title={run.schema_error ?? undefined}>
              Read only: recorded under an older scenario contract. {run.schema_error}
            </span>
          </div>
        )}
        {run.issues.length > 0 && (
          <div className="banner notice">
            <Icon name="outage" />
            <span className="truncate" title={run.issues.join("\n")}>
              {plural(run.issues.length, "trace problem")}: {run.issues[0]}
            </span>
          </div>
        )}
        {run.failure && (
          <div className="banner failure">
            <Icon name="outage" />
            <span className="truncate" title={run.failure.message ?? undefined}>
              {failureLabel(run.failure)}: {run.failure.message ?? "no details recorded"}
            </span>
          </div>
        )}
        {request && <p className="summary truncate">{request}</p>}

        <div className="transport">
          <Hint label="Previous time" keys={["←"]}>
            <Button variant="ghost" size="icon-sm" aria-label="Previous time" onClick={() => seekTo([...times].reverse().find((x) => x < t) ?? 0)}>
              <Icon name="prev" />
            </Button>
          </Hint>
          <Hint label={playing ? "Pause" : "Play"} keys={["Space"]}>
            <Button
              size="icon-sm"
              className="rounded-full"
              aria-label={playing ? "Pause" : "Play"}
              onClick={() => {
                setFollow(false);
                setGlide(false);
                if (t >= end) setT(times[0] ?? 0);
                setPlaying(t >= end ? true : !playing);
              }}
            >
              <Icon name={playing ? "pause" : "play"} />
            </Button>
          </Hint>
          <Hint label="Next time" keys={["→"]}>
            <Button variant="ghost" size="icon-sm" aria-label="Next time" onClick={() => seekTo(times.find((x) => x > t) ?? t)}>
              <Icon name="next" />
            </Button>
          </Hint>
          <span className="transport-clock">
            <span className="mono">{clock(t)}</span>
            {horizon !== undefined && <span className="muted">of {horizon}</span>}
          </span>
          {live && (
            <Hint label="Keep the playhead at the newest time">
              <label className="toggle-follow">
                <Switch
                  size="sm"
                  checked={follow}
                  onCheckedChange={(on) => {
                    setPlaying(false);
                    setFollow(on);
                  }}
                  aria-label="Follow the run"
                />
                Follow
              </label>
            </Hint>
          )}
          <span className="muted">{matches ? `${matches.size} of ${chips.length} match` : `${chips.filter((c) => c.t <= t).length} of ${chips.length} items`}</span>
          <span className="grow" />
          <InputGroup className="tl-find h-7 w-48 shrink-0">
            <InputGroupAddon>
              <Icon name="search" />
            </InputGroupAddon>
            <InputGroupInput
              ref={findBox}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") stepItem(e.shiftKey ? -1 : 1);
                if (e.key === "Escape") {
                  setQuery("");
                  e.currentTarget.blur();
                }
              }}
              placeholder="Find in the timeline"
              aria-label="Find in the timeline"
            />
            <Hint label="Focus the search; step through items" keys={["/", ",", "."]}>
              <InputGroupAddon align="inline-end">
                <Kbd className="border border-border bg-transparent">/</Kbd>
              </InputGroupAddon>
            </Hint>
          </InputGroup>
          {run.tasks.length > 0 && (
            <Picker
              label="Task"
              value={task ?? "all"}
              className="task-picker"
              onChange={(v) => setTask(v === "all" ? null : v)}
              groups={[{ options: [{ value: "all", label: "All tasks" }, ...run.tasks.map((x) => ({ value: x.task_id, label: x.task_id, meta: x.verdict ?? "not graded" }))] }]}
            />
          )}
          <label className="toggle-views">
            <Switch size="sm" checked={views} onCheckedChange={setViews} aria-label="Show views" />
            Views
          </label>
        </div>

        <Timeline
          rows={rows}
          chips={chips}
          scale={scale}
          playhead={t}
          onSeek={(x) => {
            setFollow(false);
            setPlaying(false);
            setGlide(false);
            setT(x);
          }}
          glide={glide && !playing}
          selection={sel?.id}
          onSelect={(chip) => {
            setSel(chip);
            if (chip) showServiceOf(chip);
          }}
          flash={flash}
          matches={matches}
          zoom={zoom}
        />

        {sel && (
          <div className="viewer-selected">
            <div className="selected-tool">
              <ToolCall key={sel.id} chip={sel} entries={entries} error={entryError} open onOpenChange={(o) => !o && setSel(null)} onSeek={() => seekTo(sel.t)} />
            </div>
          </div>
        )}

        <div className="viewer-bottom">
          <section className="pane">
            <VerdictPanel status={status} line={verdictLine(status, checks, selectedTask ? selectedTask.status : run.run_status)} checks={checks} chipOfLine={(line) => byLine.get(line)} onSeek={seek} pending={selectedTask ? undefined : pendingText(run)} rejected={selectedTask ? 0 : run.rejected_actions} />
            {run.tasks.length > 0 && !selectedTask && (
              <div className="verdict mt-2">
                <TaskList tasks={run.tasks} onPick={setTask} />
              </div>
            )}
            {selectedTask && (
              <p className="check-note mt-2">
                {selectedTask.message} {selectedTask.steps !== null && `${plural(selectedTask.steps, "step")}`}
                {selectedTask.completed_at !== null && `, completed at ${clock(selectedTask.completed_at)}`}
              </p>
            )}
          </section>
          <section className="pane people-pane">
            <h2 className="pane-title">People at {clock(t)}</h2>
            {people.length === 0 && <p className="empty">No people in this scenario.</p>}
            {people.map((actor) => (
              <PersonaCard key={actor.id} actor={actor} last={lastOf(actor.id)} />
            ))}
          </section>
        </div>
      </div>
      {current && shown && (
        <aside className="peek" aria-label={`${current.id} at ${clock(t)}`}>
          <div
            className="peek-resize"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize the service window"
            onPointerDown={(e) => {
              const x0 = e.clientX;
              const w0 = peekW;
              (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
              const move = (ev: PointerEvent) => setPeekW(Math.min(760, Math.max(360, w0 + x0 - ev.clientX)));
              const up = () => {
                window.removeEventListener("pointermove", move);
                window.removeEventListener("pointerup", up);
              };
              window.addEventListener("pointermove", move);
              window.addEventListener("pointerup", up);
            }}
          />
          <div className="peek-head">
            <span className="peek-time mono">{clock(t)}</span>
            <Button variant="ghost" size="icon-xs" className="ml-auto" aria-label="Close the service window" onClick={() => openPeek(null)}>
              <Icon name="x" />
            </Button>
          </div>
          <AppWindow key={current.id} service={current} state={shown.state} changedAt={shown.changed?.t} t={t} calls={chips.filter((c) => c.lane === "tools" && c.service === current.id)} onSeek={seek} />
          {!snapshots && <p className="empty">{snapshotError ? "Initial state only: the run recorded no service history." : "Loading service history…"}</p>}
        </aside>
      )}
    </div>
  );
}

