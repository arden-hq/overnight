import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";

export interface PickOption {
  value: string;
  label: string;
  trigger?: string;
  meta?: ReactNode;
}

export interface PickGroup {
  label?: string;
  options: PickOption[];
}

export function Picker({ value, groups, onChange, label, className }: { value: string; groups: PickGroup[]; onChange: (v: string) => void; label: string; className?: string }) {
  const current = groups.flatMap((g) => g.options).find((o) => o.value === value);
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger size="sm" aria-label={label} className={cn("min-w-0 bg-card font-medium shadow-sm dark:bg-card", className)}>
        <SelectValue>
          <span className="truncate">{current?.trigger ?? current?.label}</span>
        </SelectValue>
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="min-w-(--radix-select-trigger-width)">
        {groups.map((g, i) => (
          <SelectGroup key={g.label ?? i}>
            {i > 0 && <SelectSeparator />}
            {g.label && <SelectLabel>{g.label}</SelectLabel>}
            {g.options.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                <span className="truncate">{o.label}</span>
                {o.meta && <span className="ml-auto pl-4 text-xs text-muted-foreground">{o.meta}</span>}
              </SelectItem>
            ))}
          </SelectGroup>
        ))}
      </SelectContent>
    </Select>
  );
}
