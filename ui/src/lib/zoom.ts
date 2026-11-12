import { useEffect, useMemo, useRef, useState } from "react";
import { isPlainKey } from "./keys";
import { COLUMN, fitScale, MAX_UNIT, MIN_UNIT, type Scale, type Zoom } from "./time";

export interface ZoomControls {
  by: (factor: number) => void;
  toggleFit: () => void;
  fitted: boolean;
  changes: number;
}

export const ZOOM_STEP = 1.5;

export function useZoom(times: number[], end: number, avail: number): { scale: Scale; zoom: ZoomControls } {
  const [level, setLevel] = useState<Zoom>(COLUMN);
  const scale = useMemo(() => fitScale(times, end, avail, level), [times, end, avail, level]);
  const [changes, setChanges] = useState(0);
  const unit = useRef(scale.unit);
  useEffect(() => {
    unit.current = scale.unit;
  }, [scale.unit]);
  const zoom = useMemo<ZoomControls>(
    () => ({
      by: (factor) => {
        unit.current = Math.min(MAX_UNIT, Math.max(MIN_UNIT, unit.current * factor));
        setLevel(unit.current);
        setChanges((n) => n + 1);
      },
      toggleFit: () => {
        setLevel((now) => (now === "fit" ? COLUMN : "fit"));
        setChanges((n) => n + 1);
      },
      fitted: level === "fit",
      changes,
    }),
    [level, changes],
  );
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (!isPlainKey(e)) return;
      if (e.key === "+" || e.key === "=") zoom.by(ZOOM_STEP);
      if (e.key === "-") zoom.by(1 / ZOOM_STEP);
      if (e.key.toLowerCase() === "f") zoom.toggleFit();
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [zoom]);
  return { scale, zoom };
}
