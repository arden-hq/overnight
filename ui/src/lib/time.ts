export const clock = (t: number): string => `t ${Math.floor(t)}`;

export interface Segment {
  t0: number;
  t1: number;
  x0: number;
  x1: number;
  fold: boolean;
}

export interface Scale {
  x: (t: number) => number;
  t: (x: number) => number;
  width: number;
  segments: Segment[];
  unit: number;
}

export const COLUMN = 190;
const FOLD_OVER = 2;
const FOLD_PX = 44;

export function buildScale(times: number[], end: number, unit = COLUMN): Scale {
  const ts = [...new Set([...times.filter((t) => t >= 0), end])].sort((a, b) => a - b);
  if (ts.length === 0) ts.push(0);
  const segments: Segment[] = [];
  let x = 0;
  const push = (t0: number, t1: number, width: number, fold: boolean) => {
    segments.push({ t0, t1, x0: x, x1: x + width, fold });
    x += width;
  };
  for (let i = 0; i < ts.length - 1; i++) {
    const a = ts[i];
    const b = ts[i + 1];
    if (b - a > FOLD_OVER) {
      push(a, a + 1, unit, false);
      push(a + 1, b, FOLD_PX, true);
    } else push(a, b, (b - a) * unit, false);
  }
  push(ts[ts.length - 1], ts[ts.length - 1] + 1, unit, false);
  const first = segments[0];
  const last = segments[segments.length - 1];
  const xOf = (t: number) => {
    if (t <= first.t0) return 0;
    for (const s of segments) if (t <= s.t1) return s.x0 + ((t - s.t0) / (s.t1 - s.t0)) * (s.x1 - s.x0);
    return last.x1;
  };
  const tOf = (px: number) => {
    if (px <= 0) return first.t0;
    for (const s of segments) if (px <= s.x1) return s.t0 + ((px - s.x0) / (s.x1 - s.x0)) * (s.t1 - s.t0);
    return last.t1;
  };
  return { x: xOf, t: tOf, width: x, segments, unit };
}

export function fitScale(times: number[], end: number, avail: number): Scale {
  const first = buildScale(times, end);
  if (!avail || first.width >= avail) return first;
  const folds = first.segments.filter((s) => s.fold).length * FOLD_PX;
  const open = first.width - folds;
  return buildScale(times, end, (COLUMN * (avail - folds)) / Math.max(1, open));
}
