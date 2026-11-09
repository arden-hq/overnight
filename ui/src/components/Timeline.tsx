import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { Chip, Lane, RowDef } from "../types";
import { clock, type Scale } from "../lib/time";
import { LANE_NAMES, LANES } from "../lib/run";
import { Icon } from "./Icon";

interface Props {
  rows: RowDef[];
  chips: Chip[];
  scale: Scale;
  playhead?: number;
  onSeek?: (t: number) => void;
  selection?: string | null;
  onSelect?: (chip: Chip | null) => void;
  flash?: string | null;
  label?: ReactNode;
  glide?: boolean;
  dimFuture?: boolean;
  marked?: ReadonlySet<string>;
}

export const GUTTER = 192;
export const RULER = 34;
const TRACK = 26;
const LANE_CLASS: Record<Lane, string> = { operator: "user", agent: "agent", tools: "agent", people: "world", services: "world", views: "neutral", system: "neutral" };

const STACK = 2;
const MORE_W = 64;

function chipWidth(chip: Pick<Chip, "label" | "mark">): number {
  const text = chip.label.length + (chip.mark ? chip.mark.length + 1 : 0);
  return Math.min(300, Math.max(64, text * 6.6 + 40));
}

interface Placed {
  chip: Chip;
  x: number;
  w: number;
  track: number;
  more?: number;
}

function place(chips: Chip[], scale: Scale, isOpen: (t: number, group: Chip[]) => boolean): { placed: Placed[]; tracks: number } {
  const sorted = [...chips].sort((a, b) => a.t - b.t || a.sequence - b.sequence);
  const times = [...new Set(sorted.map((c) => c.t))];
  const ends: number[] = [];
  const placed: Placed[] = [];
  const take = (x: number, w: number) => {
    let track = ends.findIndex((e) => e + 4 <= x);
    if (track < 0) track = ends.length;
    ends[track] = x + w;
    return track;
  };
  times.forEach((t, i) => {
    const group = sorted.filter((c) => c.t === t);
    const x = scale.x(t) + 4;
    const room = i + 1 < times.length ? scale.x(times[i + 1]) - x - 4 : Infinity;
    const width = (chip: Chip) => (chip.kind === "tool" || chip.mark ? chipWidth(chip) : Math.min(chipWidth(chip), Math.max(scale.unit - 8, room)));
    const open = group.length <= STACK || isOpen(t, group);
    for (const chip of open ? group : group.slice(0, 1)) {
      const w = width(chip);
      placed.push({ chip, x, w, track: take(x, w) });
    }
    if (group.length > STACK) placed.push({ chip: group[0], x, w: MORE_W, track: take(x, MORE_W), more: open ? -(group.length - 1) : group.length - 1 });
  });
  return { placed, tracks: Math.max(1, ends.length) };
}

export function Timeline(p: Props) {
  const inner = useRef<HTMLDivElement>(null);
  const { scale } = p;
  const width = GUTTER + scale.width + 24;
  const scroller = useRef<HTMLDivElement>(null);
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el || p.playhead === undefined) return;
    const x = GUTTER + scale.x(p.playhead);
    const left = el.scrollLeft + GUTTER;
    const right = el.scrollLeft + el.clientWidth - 40;
    if (x < left || x > right) el.scrollLeft = Math.max(0, x - GUTTER - (el.clientWidth - GUTTER) / 3);
  }, [p.playhead, scale]);

  const tAt = (clientX: number) => {
    const r = inner.current!.getBoundingClientRect();
    return Math.max(0, Math.round(scale.t(clientX - r.left - GUTTER - 4)));
  };
  const seekDrag = (e: React.PointerEvent) => {
    if (!p.onSeek) return;
    p.onSeek(tAt(e.clientX));
    const move = (ev: PointerEvent) => p.onSeek!(tAt(ev.clientX));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const ticks: { t: number; x: number }[] = [];
  for (const s of scale.segments) {
    if (s.fold) continue;
    const every = Math.max(1, Math.ceil(56 / scale.unit));
    for (let t = Math.ceil(s.t0); t < s.t1; t += every) ticks.push({ t, x: scale.x(t) });
  }
  const folds = scale.segments.filter((s) => s.fold);
  const isFuture = (t: number) => p.dimFuture !== false && p.playhead !== undefined && t > p.playhead;

  const renderRow = (r: RowDef) => {
    const groupKey = (t: number) => `${r.lane}:${r.id}@${t}`;
    const { placed, tracks } = place(
      p.chips.filter((c) => c.lane === r.lane && c.row === r.id),
      scale,
      (t, group) => opened.has(groupKey(t)) || group.some((c) => c.id === p.selection || c.id === p.flash || p.marked?.has(c.id)),
    );
    const toggle = (t: number) =>
      setOpened((now) => {
        const next = new Set(now);
        if (!next.delete(groupKey(t))) next.add(groupKey(t));
        return next;
      });
    return (
      <div className="tl-row" key={r.lane + r.id} style={{ height: tracks * TRACK + 10 }}>
        <div className="tl-gutter tl-rowlabel">
          <Icon name={r.icon} size={13} />
          <span className="truncate" title={r.label}>
            {r.label}
          </span>
          {r.tag && <span className="tl-tag">{r.tag}</span>}
        </div>
        <div
          className="tl-track"
          onPointerDown={(e) => {
            if (e.button !== 0) return;
            if (p.onSeek) seekDrag(e);
            else p.onSelect?.(null);
          }}
        >
          {placed.map(({ chip, x, w, track, more }) => {
            if (more !== undefined)
              return (
                <button
                  type="button"
                  key={`${chip.id}-more`}
                  className="chip chip-more"
                  style={{ left: x, width: w, top: 5 + track * TRACK }}
                  aria-expanded={more < 0}
                  onPointerDown={(ev) => {
                    ev.stopPropagation();
                    toggle(chip.t);
                  }}
                  title={more < 0 ? "Fold these calls" : `${more} more at ${clock(chip.t)}`}
                >
                  {more < 0 ? "Fewer" : `+${more} more`}
                </button>
              );
            const cls = ["chip", `k-${chip.kind}`, chip.tone && `s-${chip.tone}`, p.selection === chip.id && "sel", p.marked?.has(chip.id) && "marked", isFuture(chip.t) && "future", p.flash === chip.id && "flash"].filter(Boolean).join(" ");
            return (
              <button
                type="button"
                key={chip.id}
                className={cls}
                style={{ left: x, width: w, top: 5 + track * TRACK }}
                onPointerDown={(ev) => {
                  ev.stopPropagation();
                  p.onSelect?.(chip);
                }}
                title={`${clock(chip.t)} ${chip.label}${chip.mark ? `: ${chip.mark}` : ""}`}
              >
                <Icon name={chip.icon} size={12} />
                <span className="truncate">{chip.label}</span>
                {chip.mark && <em className="chip-mark">{chip.mark}</em>}
              </button>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="tl">
      <div className="tl-scroll" ref={scroller}>
        <div className="tl-inner" ref={inner} style={{ width }}>
          <div className="tl-ruler">
            <div className="tl-gutter tl-day">{p.label ?? "Time"}</div>
            <div className="tl-ticks" onPointerDown={seekDrag}>
              {ticks.map((k) => (
                <div key={k.t} className="tick major" style={{ left: k.x + 4 }}>
                  <span>{k.t}</span>
                </div>
              ))}
              {folds.map((s) => (
                <div key={s.t0} className="fold-label" style={{ left: s.x0, width: s.x1 - s.x0 }} title={`Nothing happened for ${Math.round(s.t1 - s.t0)} ticks`}>
                  +{Math.round(s.t1 - s.t0)}
                </div>
              ))}
            </div>
          </div>

          {LANES.map((lane) => {
            const rows = p.rows.filter((r) => r.lane === lane);
            if (!rows.length) return null;
            return (
              <div className={`tl-lane lane-${LANE_CLASS[lane]}`} key={lane}>
                <div className="tl-lanehead">
                  <div className="tl-gutter">
                    <i className="lane-dot" />
                    {LANE_NAMES[lane]}
                  </div>
                  <div className="tl-track" />
                </div>
                {rows.map(renderRow)}
              </div>
            );
          })}

          <div className="tl-overlay tl-folds" style={{ left: GUTTER }}>
            {folds.map((s) => (
              <div key={s.t0} className="fold" style={{ left: s.x0, width: s.x1 - s.x0 }} />
            ))}
          </div>
          <div className={`tl-overlay${p.glide ? " glide" : ""}`} style={{ left: GUTTER }}>
            {p.playhead !== undefined && (
              <div className="tl-playhead" style={{ transform: `translateX(${scale.x(p.playhead) + 2}px)` }}>
                <span style={scale.x(p.playhead) < 24 ? { transform: "none" } : undefined}>{clock(p.playhead)}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
