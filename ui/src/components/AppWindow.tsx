import { useState } from "react";
import { Tabs } from "@/components/ui/tabs";
import type { Chip } from "../types";
import { arr, obj, str, type Json, type ServiceDef } from "../lib/run";
import { clock } from "../lib/time";
import { MovingTabs } from "./MovingTabs";
import { Icon, serviceIcon } from "./Icon";
import { cn } from "@/lib/utils";

type Tab = "Data" | "Calls" | "State";

const cell = (value: unknown): string => (value === null || value === undefined ? "" : typeof value === "string" ? value : typeof value === "number" || typeof value === "boolean" ? String(value) : JSON.stringify(value));

export function RecordTable({ rows, max = 6 }: { rows: unknown[]; max?: number }) {
  const items = rows.map(obj);
  const columns = [...new Set(items.flatMap((r) => Object.keys(r)))].slice(0, max);
  if (!items.length) return <p className="empty">None.</p>;
  return (
    <table className="dtable">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c}>{c.replace(/_/g, " ")}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {items.map((r, i) => (
          <tr key={str(r.id) ?? i}>
            {columns.map((c) => (
              <td key={c} className={cn("truncate", c === "id" && "mono")} title={cell(r[c])}>
                {cell(r[c])}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Messages({ messages, open, setOpen, prefix }: { messages: Json[]; open: string | null; setOpen: (id: string | null) => void; prefix: string }) {
  if (!messages.length) return <p className="empty">No messages.</p>;
  return (
    <div className="maillist">
      {messages.map((m, i) => {
        const id = prefix + (str(m.id) ?? i);
        const to = arr(m.to).map(String).join(", ");
        return (
          <div key={id} className={`mail${open === id ? " open" : ""}`}>
            <button type="button" className="mail-row" aria-expanded={open === id} onClick={() => setOpen(open === id ? null : id)}>
              <span className="who truncate">{cell(m.from)}</span>
              <span className="subj truncate">{cell(m.subject ?? m.body)}</span>
              <span className="time">{typeof m.date === "number" ? clock(m.date) : cell(m.date)}</span>
            </button>
            <div className="mail-body" aria-hidden={open !== id}>
              <div>
                {to && <p className="muted">To {to}</p>}
                <p>{cell(m.body)}</p>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function DataView({ kind, state }: { kind: string; state: unknown }) {
  const [open, setOpen] = useState<string | null>(null);
  const s = obj(state);
  if (kind === "mail") return <Messages messages={arr(s.messages).map(obj)} open={open} setOpen={setOpen} prefix="m" />;
  if (kind === "chat")
    return (
      <>
        {arr(s.conversations)
          .map(obj)
          .map((c) => (
            <section key={str(c.id)}>
              <h4 className="sub">
                {str(c.id)} <span className="muted">{arr(c.participants).map(String).join(", ")}</span>
              </h4>
              <Messages messages={arr(c.messages).map(obj)} open={open} setOpen={setOpen} prefix={`${str(c.id)}:`} />
            </section>
          ))}
      </>
    );
  const sections: [string, unknown[]][] = [];
  for (const [key, value] of Object.entries(s)) {
    if (Array.isArray(value)) sections.push([key, value]);
    else if (key === "collections") for (const [name, coll] of Object.entries(obj(value))) sections.push([name, arr(obj(coll).records)]);
  }
  if (!sections.length) return <pre className="json">{JSON.stringify(state, null, 2)}</pre>;
  return (
    <>
      {sections.map(([name, rows]) => (
        <section key={name}>
          <h4 className="sub">{name.replace(/_/g, " ")}</h4>
          <RecordTable rows={rows} />
        </section>
      ))}
    </>
  );
}

export function AppWindow({ service, state, changedAt, t, calls, onSeek }: { service: ServiceDef; state: unknown; changedAt?: number; t: number; calls: Chip[]; onSeek: (chip: Chip) => void }) {
  const [tab, setTab] = useState<Tab>("Data");
  const past = calls.filter((c) => c.t <= t);
  return (
    <div className="appwin">
      <div className="appwin-head">
        <Icon name={serviceIcon(service.kind)} />
        <strong className="truncate">{service.id}</strong>
        <span className="via">{changedAt === undefined ? "Initial state" : `Changed at ${clock(changedAt)}`}</span>
        <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)} className="ml-auto">
          <MovingTabs variant="pill" value={tab} className="h-7" triggerClassName="px-2.5 text-xs" items={(["Data", "Calls", "State"] as Tab[]).map((x) => ({ value: x, label: x }))} />
        </Tabs>
      </div>
      <div className="appwin-body">
        {tab === "Data" && <DataView kind={service.kind} state={state} />}
        {tab === "Calls" &&
          (past.length === 0 ? (
            <p className="empty">No calls yet at this time.</p>
          ) : (
            <table className="dtable">
              <tbody>
                {past.map((c) => (
                  <tr key={c.id}>
                    <td className="mono truncate">{c.label}</td>
                    <td className="muted truncate">{c.row}</td>
                    <td className={c.tone === "fail" ? "text-fail" : c.tone === "late" ? "text-late" : "muted"}>{c.mark ?? "succeeded"}</td>
                    <td className="num">
                      <button type="button" className="link mono" onClick={() => onSeek(c)}>
                        {clock(c.t)}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
        {tab === "State" && <pre className="json">{JSON.stringify(state, null, 2)}</pre>}
      </div>
    </div>
  );
}
