import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import type { RunStatus } from "../types";
import { Icon } from "./Icon";
import { STATUS_ICON, STATUS_TONE, STATUS_WORD } from "../lib/verdict";


const TINT: Record<RunStatus, string> = {
  ungraded: "border-dashed border-border text-muted-foreground bg-transparent",
  pass: "bg-pass/12 text-pass",
  fail: "bg-fail/12 text-fail",
  unknown: "border-dashed border-border text-muted-foreground bg-transparent",
  incomplete: "border-dashed border-border text-muted-foreground bg-transparent",
  running: "border-border text-foreground bg-transparent",
};

export function StatusBadge({ status, className }: { status: RunStatus; className?: string }) {
  return (
    <Badge variant="outline" className={cn("gap-1 border-transparent font-semibold", TINT[status], className)}>
      <Icon name={STATUS_ICON[status]} size={12} />
      {STATUS_WORD[status]}
    </Badge>
  );
}

export function StatusWord({ status, className }: { status: RunStatus; className?: string }) {
  return <span className={cn("font-medium whitespace-nowrap", STATUS_TONE[status], className)}>{STATUS_WORD[status]}</span>;
}

export function TrialsSummary({ trials }: { trials: RunStatus[] }) {
  const counted = trials.filter((t) => t === "pass" || t === "fail");
  const passed = counted.filter((t) => t === "pass").length;
  const unscored = trials.length - counted.length;
  const tone = counted.length === 0 ? "text-muted-foreground" : passed === counted.length ? "text-pass" : passed === 0 ? "text-fail" : "text-foreground";
  return (
    <span className="inline-flex min-w-0 items-baseline gap-2 whitespace-nowrap text-[12.5px]">
      <span className={cn("font-medium tabular-nums", tone)}>{counted.length ? `${passed} of ${counted.length} pass` : trials.length ? "None graded" : "No runs"}</span>
      {unscored > 0 && counted.length > 0 && <span className="text-muted-foreground">{unscored} ungraded</span>}
    </span>
  );
}
