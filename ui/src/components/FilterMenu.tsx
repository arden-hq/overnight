import { useState, type ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandSeparator } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Icon } from "./Icon";

export interface FilterOption {
  id: string;
  label: string;
  mark?: ReactNode;
}

export interface FilterGroup {
  id: string;
  label: string;
  options: FilterOption[];
}

export type FilterValue = Record<string, string[]>;

export function FilterMenu({ groups, value, onChange }: { groups: FilterGroup[]; value: FilterValue; onChange: (v: FilterValue) => void }) {
  const [open, setOpen] = useState(false);
  const picked = groups.flatMap((g) => (value[g.id] ?? []).map((id) => ({ group: g, option: g.options.find((o) => o.id === id)! }))).filter((p) => p.option);
  const toggle = (gid: string, id: string) => {
    const cur = value[gid] ?? [];
    onChange({ ...value, [gid]: cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id] });
  };

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button variant="outline" size="sm" className="shadow-sm">
            <Icon name="filter" />
            Filter
            {picked.length > 0 && <span className="text-muted-foreground tabular-nums">{picked.length}</span>}
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-64 p-0">
          <Command>
            <CommandInput placeholder="Find a filter" />
            <CommandList className="max-h-80">
              <CommandEmpty>No filter matches.</CommandEmpty>
              {groups.map((g, i) => (
                <div key={g.id}>
                  {i > 0 && <CommandSeparator />}
                  <CommandGroup heading={g.label}>
                    {g.options.map((o) => {
                      const on = (value[g.id] ?? []).includes(o.id);
                      return (
                        <CommandItem key={o.id} value={`${g.label} ${o.label}`} onSelect={() => toggle(g.id, o.id)}>
                          <Checkbox checked={on} tabIndex={-1} className="pointer-events-none" />
                          {o.mark}
                          <span className="truncate">{o.label}</span>
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                </div>
              ))}
            </CommandList>
          </Command>
          {picked.length > 0 && (
            <div className="border-t border-border p-1">
              <Button variant="ghost" size="sm" className="w-full justify-start text-muted-foreground" onClick={() => onChange({})}>
                Clear filters
              </Button>
            </div>
          )}
        </PopoverContent>
      </Popover>
      <div className="flex min-w-0 items-center gap-1 overflow-hidden">
        {picked.map(({ group, option }) => (
          <Badge key={group.id + option.id} variant="secondary" className="filter-badge h-6 shrink-0 gap-1 pr-1 font-medium">
            {option.mark}
            {option.label}
            <button
              type="button"
              aria-label={`Remove ${option.label}`}
              className="grid size-4 place-items-center rounded-sm text-muted-foreground hover:bg-accent hover:text-foreground"
              onClick={() => toggle(group.id, option.id)}
            >
              <Icon name="x" size={11} />
            </button>
          </Badge>
        ))}
      </div>
    </div>
  );
}
