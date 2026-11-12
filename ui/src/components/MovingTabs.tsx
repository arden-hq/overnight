import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { onNextTaskEvenInHiddenTab } from "@/lib/tolerant";
import { TabsList, TabsTrigger } from "@/components/ui/tabs";

export function MovingTabs({ value, items, variant, className, triggerClassName }: { value: string; items: { value: string; label: ReactNode }[]; variant: "line" | "pill"; className?: string; triggerClassName?: string }) {
  const list = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<{ x: number; w: number } | null>(null);
  const [ready, setReady] = useState(false);

  useLayoutEffect(() => {
    const el = list.current;
    if (!el) return;
    const place = () => {
      const active = el.querySelector<HTMLElement>('[data-state="active"]');
      if (active) setBox({ x: active.offsetLeft, w: active.offsetWidth });
    };
    place();
    const ro = new ResizeObserver(place);
    ro.observe(el);
    return () => ro.disconnect();
  }, [value]);

  useLayoutEffect(() => {
    if (!box || ready) return;
    return onNextTaskEvenInHiddenTab(() => setReady(true));
  }, [box, ready]);

  return (
    <TabsList ref={list} variant={variant === "line" ? "line" : "default"} className={cn("moving-tabs relative", `moving-${variant}`, ready && "ready", className)}>
      {box && <span className="moving-mark" aria-hidden style={{ transform: `translateX(${box.x}px)`, width: box.w }} />}
      {items.map((i) => (
        <TabsTrigger key={i.value} value={i.value} className={cn("relative z-[1] data-[state=active]:bg-transparent! data-[state=active]:shadow-none! after:opacity-0! dark:data-[state=active]:border-transparent!", triggerClassName)}>
          {i.label}
        </TabsTrigger>
      ))}
    </TabsList>
  );
}
