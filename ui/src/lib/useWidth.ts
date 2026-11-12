import { useLayoutEffect, useRef, useState } from "react";
import { GUTTER, LAST_CHIP_ROOM } from "../components/Timeline";

export function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(Math.max(0, el.clientWidth - GUTTER - LAST_CHIP_ROOM)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}
