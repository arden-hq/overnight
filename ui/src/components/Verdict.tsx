import type { CheckView, TaskView } from "../api/types";
import { checkStatus, LAYERS } from "../lib/verdict";
import type { Chip, RunStatus } from "../types";
import { runStatus } from "../lib/run";
import { clock } from "../lib/time";
import { cn } from "@/lib/utils";
import { Icon } from "./Icon";
import { StatusBadge, StatusWord } from "./Status";

export function CheckLayers({ checks, chipOfLine, onSeek }: { checks: CheckView[]; chipOfLine?: (line: number) => Chip | undefined; onSeek?: (chip: Chip) => void }) {
  return (
    <>
      {LAYERS.map((l) => {
        const items = checks.filter((c) => c.layer === l.id);
        if (!items.length) return null;
        const passed = items.filter((c) => c.passed === true).length;
        const graded = items.some((c) => c.passed !== null);
        return (
          <section key={l.id} className={cn("layer", `ly-${l.id}`, graded && passed < items.length && "bad")}>
            <h3>
              <span>{l.name}</span>
              <span className="count">{graded ? `${passed} of ${items.length}` : items.length}</span>
            </h3>
            <ul>
              {items.map((c) => {
                const status = checkStatus(c);
                const evidence = chipOfLine ? [...new Map(c.evidence_lines.map((line) => [chipOfLine(line)?.id ?? `line-${line}`, [line, chipOfLine(line)] as const])).values()] : [];
                return (
                  <li key={`${c.task_id ?? ""}${c.layer}${c.index}`} className={status === "pass" ? "ok" : status === "fail" ? "no" : "na"}>
                    <Icon name={status === "pass" ? "check" : status === "fail" ? "x" : "dash"} size={12} />
                    <div className="check-body">
                      <span className="check-line">
                        <span className="check-text truncate" title={c.predicate}>
                          {c.text}
                        </span>
                        {graded && <StatusWord status={status} className="check-word" />}
                      </span>
                      {c.label && <span className="check-how mono truncate">{c.predicate}</span>}
                      {evidence.length > 0 && onSeek && (
                        <span className="evidence">
                          {evidence.map(([line, chip]) =>
                            chip ? (
                              <button key={line} type="button" className="link ev" onClick={() => onSeek(chip)}>
                                <span className="mono">{clock(chip.t)}</span> {chip.label}
                              </button>
                            ) : (
                              <span key={line} className="ev muted">
                                <span className="mono">line {line}</span> hidden by the filter
                              </span>
                            ),
                          )}
                        </span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </>
  );
}

export function VerdictPanel({ status, line, checks, chipOfLine, onSeek, pending, rejected = 0 }: { status: RunStatus; line: string; checks: CheckView[]; chipOfLine: (line: number) => Chip | undefined; onSeek: (chip: Chip) => void; pending?: string; rejected?: number }) {
  return (
    <div className="verdict">
      <div className="verdict-head">
        <StatusBadge status={status} />
        <span className="truncate">{line}</span>
      </div>
      {pending && <p className="check-note">{pending}</p>}
      {rejected > 0 && <p className="check-note">{rejected === 1 ? "1 action" : `${rejected} actions`} rejected and returned to the agent.</p>}
      <CheckLayers checks={checks} chipOfLine={chipOfLine} onSeek={onSeek} />
    </div>
  );
}

export function TaskList({ tasks, onPick }: { tasks: TaskView[]; onPick: (id: string) => void }) {
  return (
    <section className="layer">
      <h3>
        <span>Tasks</span>
        <span className="count">
          {tasks.filter((t) => t.verdict === "passed").length} of {tasks.length}
        </span>
      </h3>
      <ul>
        {tasks.map((task) => {
          const status = runStatus(task.verdict, task.status !== null);
          const passed = task.checks.filter((c) => c.passed === true).length;
          return (
            <li key={task.task_id} className={status === "pass" ? "ok" : status === "fail" ? "no" : "na"}>
              <Icon name={status === "pass" ? "check" : status === "fail" ? "x" : "dash"} size={12} />
              <div className="check-body">
                <span className="check-line">
                  <button type="button" className="link check-text truncate" onClick={() => onPick(task.task_id)}>
                    {task.task_id}
                  </button>
                  <span className="check-count muted">
                    {passed} of {task.checks.length} checks
                  </span>
                  <StatusWord status={status} className="check-word" />
                </span>
                {task.message && <span className="check-how truncate">{task.message}</span>}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
