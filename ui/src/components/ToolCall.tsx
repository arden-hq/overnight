import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { TraceLine } from "../api/types";
import type { Chip } from "../types";
import { clock } from "../lib/time";
import { obj } from "../lib/run";
import { Icon } from "./Icon";

function Json({ value }: { value: unknown }) {
  if (typeof value === "string") return <>{value}</>;
  const text = JSON.stringify(value, null, 2) ?? "null";
  const parts: ReactNode[] = [];
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(-?\d+(?:\.\d+)?)\b|\b(true|false|null)\b/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    parts.push(text.slice(last, m.index));
    if (m[1] && m[2]) parts.push(<span key={m.index} className="tc-key">{m[1]}</span>, m[2]);
    else if (m[1]) parts.push(<span key={m.index} className="tc-str">{m[1]}</span>);
    else if (m[3]) parts.push(<span key={m.index} className="tc-num">{m[3]}</span>);
    else parts.push(<span key={m.index} className="tc-lit">{m[4]}</span>);
    last = re.lastIndex;
  }
  parts.push(text.slice(last));
  return <>{parts}</>;
}

function Section({ label, value }: { label: string; value: unknown }) {
  const copy = () => {
    void navigator.clipboard?.writeText(typeof value === "string" ? value : JSON.stringify(value, null, 2));
    toast(`${label} copied`);
  };
  return (
    <div className="tc-section">
      <div className="tc-section-head">
        <span>{label}</span>
        <Button variant="ghost" size="icon-xs" aria-label={`Copy ${label.toLowerCase()}`} onClick={copy}>
          <Icon name="copy" />
        </Button>
      </div>
      <pre className="tc-code">
        <Json value={value} />
      </pre>
    </div>
  );
}

const SECTION_NAME: Record<string, string> = { policy_check: "Policy", invocation: "Arguments", fault: "Fault", result: "Result" };

function sections(chip: Chip, entries: TraceLine[]): { label: string; value: unknown }[] {
  return entries.map(({ line, entry }) => {
    const row = obj(entry);
    const kind = typeof row.kind === "string" ? row.kind : "entry";
    const payload = obj(row.payload);
    const value = chip.lane === "tools" && kind === "invocation" ? (payload.args ?? payload) : chip.lane === "tools" ? payload : entry;
    return { label: `${SECTION_NAME[kind] ?? kind.replace(/_/g, " ")}, line ${line}`, value };
  });
}

export function ToolCall({ chip, entries, error, open, onOpenChange, onSeek }: { chip: Chip; entries?: TraceLine[]; error?: string; open?: boolean; onOpenChange?: (o: boolean) => void; onSeek?: () => void }) {
  const [inner, setInner] = useState(false);
  const isOpen = open ?? inner;
  return (
    <Collapsible open={isOpen} onOpenChange={(o) => (onOpenChange ?? setInner)(o)} className={cn("tc", chip.tone === "fail" && "tc-harm")}>
      <CollapsibleTrigger className="tc-head">
        <Icon name={chip.icon} className="tc-icon" />
        <code className="tc-name">{chip.lane === "tools" ? chip.label : chip.kind.replace(/_/g, " ")}</code>
        <span className="tc-summary truncate">{chip.lane === "tools" ? (chip.row === "agent" ? "" : `by ${chip.row}`) : chip.label}</span>
        {chip.mark && <span className={cn("tc-mark", chip.tone === "fail" ? "text-fail" : "text-late")}>{chip.mark}</span>}
        <span
          className="tc-time"
          role={onSeek ? "link" : undefined}
          onClick={(ev) => {
            if (!onSeek) return;
            ev.stopPropagation();
            onSeek();
          }}
        >
          {clock(chip.t)}
        </span>
        <Icon name="chevron" className="tc-chev" />
      </CollapsibleTrigger>
      <CollapsibleContent className="tc-body-wrap overflow-hidden data-open:animate-collapsible-down data-closed:animate-collapsible-up">
        <div className="tc-body">
          {error && <p className="text-fail">{error}</p>}
          {!error && !entries && <p className="empty">Loading the trace entries…</p>}
          {entries && sections(chip, entries).map((s) => <Section key={s.label} label={s.label} value={s.value} />)}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
