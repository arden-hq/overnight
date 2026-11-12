import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Button } from "@/components/ui/button";
import type { Chip, Lane, RowDef } from "../types";
import { clock, type Scale } from "../lib/time";
import { LANE_NAMES, LANES } from "../lib/run";
import { ZOOM_STEP, type ZoomControls } from "../lib/zoom";
import { Hint } from "./Hint";
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
  matches?: ReadonlySet<string>;
  zoom?: ZoomControls;
}

export const GUTTER = 192;
export const RULER = 34;
export const LAST_CHIP_ROOM = 164;
const TRACK = 26;
const LANE_CLASS: Record<Lane, string> = { operator: "user", agent: "agent", tools: "agent", people: "world", services: "world", views: "neutral", system: "neutral" };

const STACK = 2;
const MORE_W = 64;
const COMPACT_BELOW = 96;
const COMPACT_W = 22;
const COMPACT_MORE_W = 30;
const TAIL_ROOM = 24;

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

function place(chips: Chip[], scale: Scale, compact: boolean, rightEdge: number, isOpen: (t: number, group: Chip[]) => boolean): { placed: Placed[]; tracks: number } {
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
    const moreW = compact ? COMPACT_MORE_W : MORE_W;
    const width = (chip: Chip) => (compact ? COMPACT_W : chip.kind === "tool" || chip.mark ? chipWidth(chip) : Math.min(chipWidth(chip), Math.max(scale.unit - 8, room)));
    const open = group.length <= STACK || isOpen(t, group);
    for (const chip of open ? group : group.slice(0, 1)) {
      const w = Math.max(COMPACT_W, Math.min(width(chip), rightEdge - x));
      placed.push({ chip, x, w, track: take(x, w) });
    }
    if (group.length > STACK) placed.push({ chip: group[0], x, w: moreW, track: take(x, moreW), more: open ? -(group.length - 1) : group.length - 1 });
  });
  return { placed, tracks: Math.max(1, ends.length) };
}

export function Timeline(p: Props) {
  const inner = useRef<HTMLDivElement>(null);
  const { scale } = p;
  const compact = scale.unit < COMPACT_BELOW;
  const { zoom } = p;
  const scroller = useRef<HTMLDivElement>(null);
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());

  const zoomAnchorPx = useRef<number | null>(null);
  const changes = zoom?.changes ?? 0;
  const drawn = useRef({ scale, changes });

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const before = drawn.current;
    drawn.current = { scale, changes };
    if (changes !== before.changes) {
      const px = zoomAnchorPx.current ?? (el.clientWidth - GUTTER) / 2;
      zoomAnchorPx.current = null;
      el.scrollLeft = Math.max(0, scale.x(before.scale.t(el.scrollLeft + px)) - px);
      return;
    }
    if (p.playhead === undefined) return;
    const x = GUTTER + scale.x(p.playhead);
    const left = el.scrollLeft + GUTTER;
    const right = el.scrollLeft + el.clientWidth - 40;
    if (x < left || x > right) el.scrollLeft = Math.max(0, x - GUTTER - (el.clientWidth - GUTTER) / 3);
  }, [p.playhead, scale, changes]);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      if (!zoom || (!e.ctrlKey && !e.metaKey)) return;
      e.preventDefault();
      zoomAnchorPx.current = e.clientX - el.getBoundingClientRect().left - GUTTER;
      zoom.by(Math.exp(-e.deltaY * 0.01));
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [zoom]);

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

  const groupKeyOf = (r: RowDef, t: number) => `${r.lane}:${r.id}@${t}`;
  const laidOut = p.rows.map((row) => ({
    row,
    ...place(
      p.chips.filter((c) => c.lane === row.lane && c.row === row.id),
      scale,
      compact,
      zoom?.fitted ? scale.width + LAST_CHIP_ROOM - TAIL_ROOM : Infinity,
      (t, group) => opened.has(groupKeyOf(row, t)) || group.some((c) => c.id === p.selection || c.id === p.flash || p.marked?.has(c.id) || p.matches?.has(c.id)),
    ),
  }));
  const contentWidth = Math.max(scale.width, ...laidOut.flatMap((l) => l.placed.map((c) => c.x + c.w))) + TAIL_ROOM;
  const width = GUTTER + contentWidth;

  const renderRow = ({ row: r, placed, tracks }: (typeof laidOut)[number]) => {
    const groupKey = (t: number) => groupKeyOf(r, t);
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
                  {more < 0 ? (compact ? "−" : "Fewer") : compact ? `+${more}` : `+${more} more`}
                </button>
              );
            const cls = ["chip", `k-${chip.kind}`, chip.tone && `s-${chip.tone}`, p.selection === chip.id && "sel", p.marked?.has(chip.id) && "marked", isFuture(chip.t) && "future", p.matches && !p.matches.has(chip.id) && "unmatched", compact && "compact", p.flash === chip.id && "flash"].filter(Boolean).join(" ");
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
                {!compact && <span className="truncate">{chip.label}</span>}
                {!compact && chip.mark && <em className="chip-mark">{chip.mark}</em>}
              </button>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="tl">
      <Overview
        chips={p.chips}
        scale={scale}
        total={contentWidth}
        playhead={p.playhead}
        scroller={scroller}
        onPan={(centerX) => {
          const el = scroller.current;
          if (el) el.scrollLeft = centerX - (el.clientWidth - GUTTER) / 2;
        }}
      />
      <div className="tl-scroll" ref={scroller}>
        <div className="tl-inner" ref={inner} style={{ width }}>
          <div className="tl-ruler">
            <div className="tl-gutter tl-day">
              <span className="truncate">{p.label ?? "Time"}</span>
              {zoom && (
                <span className="tl-zoom">
                  <Hint label="Zoom out" keys={["−"]}>
                    <Button variant="ghost" size="icon-xs" aria-label="Zoom out" onClick={() => zoom.by(1 / ZOOM_STEP)}>
                      <Icon name="zoomout" size={13} />
                    </Button>
                  </Hint>
                  <Hint label="Zoom in" keys={["+"]}>
                    <Button variant="ghost" size="icon-xs" aria-label="Zoom in" onClick={() => zoom.by(ZOOM_STEP)}>
                      <Icon name="zoomin" size={13} />
                    </Button>
                  </Hint>
                  <Hint label={zoom.fitted ? "Readable size" : "Fit the whole run"} keys={["F"]}>
                    <Button variant="ghost" size="icon-xs" aria-label={zoom.fitted ? "Readable size" : "Fit the whole run"} aria-pressed={zoom.fitted} onClick={zoom.toggleFit}>
                      <Icon name="fit" size={13} />
                    </Button>
                  </Hint>
                </span>
              )}
            </div>
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
            const rows = laidOut.filter((l) => l.row.lane === lane);
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

function Overview({ chips, scale, total, playhead, scroller, onPan }: { chips: Chip[]; scale: Scale; total: number; playhead?: number; scroller: RefObject<HTMLDivElement | null>; onPan: (centerX: number) => void }) {
  const view = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const sync = () => {
      const visible = el.clientWidth - GUTTER;
      setOverflows(total > visible + 1);
      if (!view.current) return;
      const left = Math.min(100, (el.scrollLeft / total) * 100);
      view.current.style.left = `${left}%`;
      view.current.style.width = `${Math.min(100 - left, (visible / total) * 100)}%`;
    };
    sync();
    el.addEventListener("scroll", sync, { passive: true });
    const ro = new ResizeObserver(sync);
    ro.observe(el);
    return () => {
      el.removeEventListener("scroll", sync);
      ro.disconnect();
    };
  }, [scroller, total, overflows]);

  if (!overflows) return null;
  const failing = new Set(chips.filter((c) => c.tone === "fail").map((c) => c.t));
  const times = [...new Set(chips.map((c) => c.t))];
  const pan = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    onPan(((e.clientX - r.left) / r.width) * total);
  };
  return (
    <div className="tl-overview">
      <div className="tl-gutter tl-overview-label">Whole run</div>
      <div
        className="tl-overview-track"
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          pan(e);
        }}
        onPointerMove={(e) => e.buttons === 1 && pan(e)}
      >
        {times.map((t) => (
          <i key={t} className={failing.has(t) ? "tl-overview-mark fail" : "tl-overview-mark"} style={{ left: `${(scale.x(t) / total) * 100}%` }} />
        ))}
        {playhead !== undefined && <i className="tl-overview-now" style={{ left: `${(scale.x(playhead) / total) * 100}%` }} />}
        <div className="tl-overview-view" ref={view} />
      </div>
    </div>
  );
}
