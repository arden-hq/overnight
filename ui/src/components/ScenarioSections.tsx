import { useState } from "react";
import type { OperationView, ScenarioDetail } from "../api/types";
import { arr, obj, str, type Json } from "../lib/run";
import { clock } from "../lib/time";
import { agentWord } from "../lib/views";
import { child, list, pathText, put } from "../lib/edit";
import { AddButton, Choice, Field, FormGrid, JsonField, KeyValues, ListInput, NumberInput, PathInput, RemoveButton, TextInput } from "./fields";
import { ItemList } from "./ItemList";
import { Picker } from "./Picker";

export type Edit = (fn: (doc: Json) => void) => void;
interface Props {
  doc: Json;
  edit: Edit;
  detail: ScenarioDetail;
}

const text = (value: unknown) => (value === undefined || value === null ? "" : typeof value === "string" ? value : JSON.stringify(value));

function uniqueId(items: Json[], stem: string): string {
  const taken = new Set(items.map((i) => str(i.id)));
  let n = items.length + 1;
  while (taken.has(`${stem}-${n}`)) n++;
  return `${stem}-${n}`;
}

function operationChoices(doc: Json, detail: ScenarioDetail): string[] {
  const byKind = new Map<string, OperationView[]>(detail.services.map((s) => [s.kind, s.operations]));
  return arr(doc.services)
    .map(obj)
    .flatMap((s) => (byKind.get(str(s.kind) ?? "") ?? []).map((op) => `${str(s.id)}.${op.name}`));
}

const serviceIds = (doc: Json) =>
  arr(doc.services)
    .map(obj)
    .map((s) => str(s.id) ?? "");

const actorIds = (doc: Json, roles?: string[]) =>
  arr(doc.actors)
    .map(obj)
    .filter((a) => !roles || roles.includes(str(a.role) ?? ""))
    .map((a) => str(a.id) ?? "");

function OperationChoice({ target, choices, onChange }: { target: Json; choices: string[]; onChange: (service: string, operation: string) => void }) {
  const current = target.service_id !== undefined ? `${text(target.service_id)}.${text(target.operation)}` : undefined;
  return (
    <Choice
      label="Operation"
      value={current}
      options={choices}
      onChange={(v) => {
        const [service, ...rest] = (v ?? "").split(".");
        onChange(service, rest.join("."));
      }}
    />
  );
}

function when(event: Json): string {
  const parts: string[] = [];
  if (typeof event.at === "number") parts.push(`at ${clock(event.at)}`);
  const deps = arr(event.dependencies).map(String);
  if (deps.length) parts.push(`after ${deps.join(", ")}${typeof event.delay === "number" && event.delay ? ` plus ${event.delay}` : ""}`);
  if (event.condition !== undefined) parts.push("if condition holds");
  return parts.join(", ") || "on start";
}

export function EventsSection({ doc, edit, detail }: Props) {
  const events = arr(doc.events).map(obj);
  const faults = arr(doc.faults).map(obj);
  const ops = operationChoices(doc, detail);
  const people = actorIds(doc, ["person", "system"]);
  const editEvent = (i: number, fn: (event: Json) => void) => edit((d) => fn(list(d, "events")[i]));
  const editFault = (i: number, fn: (fault: Json) => void) => edit((d) => fn(list(d, "faults")[i]));
  return (
    <>
      <ItemList
        title="Events"
        items={events}
        empty="No events."
        addLabel="Add event"
        itemName={(e) => str(e.id) ?? "event"}
        onAdd={() =>
          edit((d) => {
            const id = uniqueId(list(d, "events"), "event");
            const [service, operation] = (ops[0] ?? ".").split(".");
            list(d, "events").push({ id, invocation: { id: `${id}-call`, actor_id: people[0] ?? "", service_id: service, operation, args: {} }, at: typeof d.start_time === "number" ? d.start_time : 0 });
          })
        }
        onRemove={(i) => edit((d) => void list(d, "events").splice(i, 1))}
        columns={[
          { head: "Event", width: "16%", mono: true, cell: (e) => str(e.id) },
          { head: "When", width: "24%", cell: (e) => when(e), title: (e) => when(e) },
          { head: "Person", width: "12%", cell: (e) => str(obj(e.invocation).actor_id) },
          { head: "Operation", width: "20%", mono: true, cell: (e) => `${text(obj(e.invocation).service_id)}.${text(obj(e.invocation).operation)}` },
          { head: "Arguments", cell: (e) => <span className="muted">{text(obj(e.invocation).args)}</span>, title: (e) => text(obj(e.invocation).args) },
        ]}
        form={(e, i) => {
          const call = obj(e.invocation);
          return (
            <FormGrid>
              <Field label="Event id">
                <TextInput label="Event id" mono value={e.id} onChange={(v) => editEvent(i, (x) => put(x, "id", v ?? ""))} />
              </Field>
              <Field label="Invocation id">
                <TextInput label="Invocation id" mono value={call.id} onChange={(v) => editEvent(i, (x) => put(child(x, "invocation"), "id", v ?? ""))} />
              </Field>
              <Field label="Person">
                <Choice label="Person" value={call.actor_id} options={people} onChange={(v) => editEvent(i, (x) => put(child(x, "invocation"), "actor_id", v))} />
              </Field>
              <Field label="Operation">
                <OperationChoice
                  target={call}
                  choices={ops}
                  onChange={(service, operation) =>
                    editEvent(i, (x) => {
                      const inv = child(x, "invocation");
                      inv.service_id = service;
                      inv.operation = operation;
                    })
                  }
                />
              </Field>
              <Field label="At">
                <NumberInput label="At" optional value={e.at} onChange={(v) => editEvent(i, (x) => put(x, "at", v))} />
              </Field>
              <Field label="After">
                <ListInput label="After" placeholder="No dependencies" value={e.dependencies} onChange={(v) => editEvent(i, (x) => put(x, "dependencies", v.length ? v : undefined))} />
              </Field>
              <Field label="Delay">
                <NumberInput label="Delay" optional value={e.delay} onChange={(v) => editEvent(i, (x) => put(x, "delay", v))} />
              </Field>
              <Field label="Task">
                <TextInput label="Task" mono optional value={call.task_id} onChange={(v) => editEvent(i, (x) => put(child(x, "invocation"), "task_id", v))} />
              </Field>
              <Field label="Arguments" wide>
                <JsonField label="Arguments" rows={4} value={call.args ?? {}} onChange={(v) => editEvent(i, (x) => put(child(x, "invocation"), "args", v))} />
              </Field>
              <Field label="Condition" wide>
                <JsonField label="Condition" rows={3} optional value={e.condition} onChange={(v) => editEvent(i, (x) => put(x, "condition", v))} />
              </Field>
            </FormGrid>
          );
        }}
      />
      <ItemList
        title="Outages"
        items={faults}
        empty="No outages."
        addLabel="Add outage"
        itemName={(f) => str(f.id) ?? "outage"}
        onAdd={() =>
          edit((d) => {
            const [service, operation] = (ops[0] ?? ".").split(".");
            list(d, "faults").push({ id: uniqueId(list(d, "faults"), "outage"), service_id: service, operation, remaining: 1, probability: 1, error: "fault" });
          })
        }
        onRemove={(i) => edit((d) => void list(d, "faults").splice(i, 1))}
        columns={[
          { head: "Outage", width: "18%", mono: true, cell: (f) => str(f.id) },
          { head: "Operation", width: "22%", mono: true, cell: (f) => `${text(f.service_id)}.${text(f.operation)}` },
          { head: "Remaining", width: 90, cell: (f) => text(f.remaining ?? 1) },
          { head: "Probability", width: 96, cell: (f) => text(f.probability ?? 1) },
          { head: "Window", width: 120, cell: (f) => (f.start === undefined && f.end === undefined ? "Always" : `${f.start === undefined ? "start" : clock(Number(f.start))} to ${f.end === undefined ? "end" : clock(Number(f.end))}`) },
          { head: "Error", cell: (f) => text(f.error), title: (f) => text(f.error) },
        ]}
        form={(f, i) => (
          <FormGrid>
            <Field label="Outage id">
              <TextInput label="Outage id" mono value={f.id} onChange={(v) => editFault(i, (x) => put(x, "id", v ?? ""))} />
            </Field>
            <Field label="Operation">
              <OperationChoice
                target={f}
                choices={ops}
                onChange={(service, operation) =>
                  editFault(i, (x) => {
                    x.service_id = service;
                    x.operation = operation;
                  })
                }
              />
            </Field>
            <Field label="Remaining">
              <NumberInput label="Remaining" optional value={f.remaining} onChange={(v) => editFault(i, (x) => put(x, "remaining", v))} />
            </Field>
            <Field label="Probability">
              <NumberInput label="Probability" optional step={0.05} value={f.probability} onChange={(v) => editFault(i, (x) => put(x, "probability", v))} />
            </Field>
            <Field label="From">
              <NumberInput label="From" optional value={f.start} onChange={(v) => editFault(i, (x) => put(x, "start", v))} />
            </Field>
            <Field label="Until">
              <NumberInput label="Until" optional value={f.end} onChange={(v) => editFault(i, (x) => put(x, "end", v))} />
            </Field>
            <Field label="Error" wide>
              <TextInput label="Error" value={f.error} onChange={(v) => editFault(i, (x) => put(x, "error", v ?? ""))} />
            </Field>
          </FormGrid>
        )}
      />
    </>
  );
}

const ROLES = ["person", "agent", "operator", "system"];

function grantsText(grants: unknown): string {
  return Object.entries(obj(grants))
    .map(([service, ops]) => `${service}: ${arr(ops).map(String).join(", ").replace("*", "every operation")}`)
    .join("; ");
}

export function PeopleSection({ doc, edit }: Props) {
  const actors = arr(doc.actors).map(obj);
  const services = serviceIds(doc);
  const editActor = (i: number, fn: (actor: Json) => void) => edit((d) => fn(list(d, "actors")[i]));
  return (
    <ItemList
      title="People"
      items={actors}
      empty="No actors."
      addLabel="Add person"
      itemName={(a) => str(a.id) ?? "actor"}
      onAdd={() => edit((d) => void list(d, "actors").push({ id: uniqueId(list(d, "actors"), "person"), role: "person", persona: {} }))}
      onRemove={(i) => edit((d) => void list(d, "actors").splice(i, 1))}
      columns={[
        { head: "Actor", width: "16%", mono: true, cell: (a) => str(a.id) },
        { head: "Role", width: 90, cell: (a) => str(a.role) },
        { head: "Persona", width: "34%", cell: (a) => Object.keys(obj(a.persona)).join(", ").replace(/_/g, " ") || <span className="muted">None</span> },
        { head: "May do", cell: (a) => grantsText(a.operations) || <span className="muted">Nothing</span>, title: (a) => grantsText(a.operations) },
      ]}
      form={(a, i) => (
        <FormGrid>
          <Field label="Actor id">
            <TextInput label="Actor id" mono value={a.id} onChange={(v) => editActor(i, (x) => put(x, "id", v ?? ""))} />
          </Field>
          <Field label="Role">
            <Choice label="Role" value={a.role} options={ROLES} onChange={(v) => editActor(i, (x) => put(x, "role", v))} />
          </Field>
          <Field label="Reads" wide>
            <ListInput label="Reads" placeholder="No services" value={a.read_services} onChange={(v) => editActor(i, (x) => put(x, "read_services", v.length ? v : undefined))} />
          </Field>
          <Field label="Persona" wide>
            <KeyValues keyLabel="Persona field" addLabel="Add persona field" value={a.persona} onChange={(v) => editActor(i, (x) => put(x, "persona", v))} />
          </Field>
          <Field label="May do" wide>
            <Grants services={services} value={a.operations} onChange={(v) => editActor(i, (x) => put(x, "operations", Object.keys(v).length ? v : undefined))} />
          </Field>
        </FormGrid>
      )}
    />
  );
}

function Grants({ services, value, onChange }: { services: string[]; value: unknown; onChange: (v: Json) => void }) {
  const entries = Object.entries(obj(value));
  const rebuild = (next: [string, unknown][]) => onChange(Object.fromEntries(next));
  const unused = services.filter((s) => !entries.some(([k]) => k === s));
  return (
    <div className="kv-edit">
      {entries.map(([service, ops], i) => (
        <div className="kv-edit-row" key={service}>
          <Choice label="Service" value={service} options={[service, ...unused]} onChange={(v) => rebuild(entries.map((e, j) => (j === i ? [v ?? service, e[1]] : e)))} />
          <ListInput label={`${service} operations`} placeholder="No operations" value={ops} onChange={(v) => rebuild(entries.map((e, j) => (j === i ? [e[0], v] : e)))} />
          <RemoveButton label={`Remove ${service} grant`} onClick={() => rebuild(entries.filter((_, j) => j !== i))} />
        </div>
      ))}
      {unused.length > 0 && <AddButton label="Add grant" onClick={() => rebuild([...entries, [unused[0], ["*"]]])} />}
    </div>
  );
}

const STEP_KINDS = ["message", "status", "approve", "deny", "policy"];
// A persona decides approvals itself, so its scenario takes no timed approve or deny steps.
const PERSONA_STEP_KINDS = ["message", "status", "policy"];
const MODES = ["allow", "approval", "deny"];

function stepDetail(step: Json): string {
  const kind = str(step.kind);
  if (kind === "message") return text(step.message);
  if (kind === "approve" || kind === "deny") return step.match !== undefined ? `Matching ${text(step.match)}` : `Invocation ${text(step.invocation_id)}`;
  if (kind === "policy") return `${text(step.service_id)}.${text(step.operation)} becomes ${text(step.mode)}`;
  return "";
}

export function OperatorSection({ doc, edit, detail }: Props) {
  const operator = obj(doc.operator);
  const policy = obj(operator.policy);
  const rules = arr(policy.rules).map(obj);
  const steps = arr(operator.steps).map(obj);
  const persona = operator.persona !== undefined && operator.persona !== null ? obj(operator.persona) : undefined;
  const tasks = arr(doc.tasks).map(obj);
  const agent = obj(doc.agent);
  const ops = operationChoices(doc, detail);
  const editPolicy = (fn: (p: Json) => void) => edit((d) => fn(child(child(d, "operator"), "policy")));
  const editRule = (i: number, fn: (r: Json) => void) => editPolicy((p) => fn(list(p, "rules")[i]));
  const editStep = (i: number, fn: (s: Json) => void) => edit((d) => fn(list(child(d, "operator"), "steps")[i]));
  const editTask = (i: number, fn: (t: Json) => void) => edit((d) => fn(list(d, "tasks")[i]));
  return (
    <>
      <div className="item-list-head">
        <h2 className="pane-title">Policy</h2>
      </div>
      <div className="policy-default">
        <span className="form-label">When no rule matches</span>
        <Choice label="Default policy" value={policy.default} options={["allow", "deny"]} onChange={(v) => editPolicy((p) => put(p, "default", v))} />
      </div>
      <ItemList
        items={rules}
        empty="No rules."
        addLabel="Add rule"
        itemName={(r) => `${text(r.service_id)}.${text(r.operation)}`}
        onAdd={() =>
          editPolicy((p) => {
            const [service, operation] = (ops[0] ?? ".").split(".");
            list(p, "rules").push({ service_id: service, operation, mode: "approval" });
          })
        }
        onRemove={(i) => editPolicy((p) => void list(p, "rules").splice(i, 1))}
        columns={[
          { head: "Rule", width: "40%", mono: true, cell: (r) => `${text(r.service_id)}.${text(r.operation)}` },
          { head: "Mode", cell: (r) => text(r.mode) },
        ]}
        form={(r, i) => (
          <FormGrid>
            <Field label="Operation">
              <OperationChoice
                target={r}
                choices={["*.*", ...ops]}
                onChange={(service, operation) =>
                  editRule(i, (x) => {
                    x.service_id = service;
                    x.operation = operation;
                  })
                }
              />
            </Field>
            <Field label="Mode">
              <Choice label="Mode" value={r.mode} options={MODES} onChange={(v) => editRule(i, (x) => put(x, "mode", v))} />
            </Field>
          </FormGrid>
        )}
      />
      <ItemList
        title="Operator steps"
        items={steps}
        empty="No operator steps."
        addLabel="Add step"
        itemName={(_, i) => `step ${i + 1}`}
        onAdd={() => edit((d) => void list(child(d, "operator"), "steps").push({ at: typeof d.start_time === "number" ? d.start_time : 0, kind: "status" }))}
        onRemove={(i) => edit((d) => void list(child(d, "operator"), "steps").splice(i, 1))}
        columns={[
          { head: "At", width: 70, mono: true, cell: (s) => (typeof s.at === "number" ? clock(s.at) : "") },
          { head: "Step", width: 90, cell: (s) => str(s.kind) },
          { head: "Task", width: 120, mono: true, cell: (s) => str(s.task_id) ?? "" },
          { head: "Detail", cell: (s) => stepDetail(s), title: (s) => stepDetail(s) },
        ]}
        form={(s, i) => {
          const kind = str(s.kind);
          return (
            <FormGrid>
              <Field label="At">
                <NumberInput label="At" value={s.at} onChange={(v) => editStep(i, (x) => put(x, "at", v))} />
              </Field>
              <Field label="Step">
                <Choice
                  label="Step"
                  value={kind}
                  options={persona ? PERSONA_STEP_KINDS : STEP_KINDS}
                  onChange={(v) =>
                    editStep(i, (x) => {
                      for (const key of Object.keys(x)) if (key !== "at") delete x[key];
                      x.kind = v;
                      if (v === "message") x.message = "";
                      if (v === "approve" || v === "deny") x.match = { service_id: "", operation: "", args: {} };
                      if (v === "policy") Object.assign(x, { service_id: "*", operation: "*", mode: "allow" });
                    })
                  }
                />
              </Field>
              {kind === "message" && (
                <>
                  <Field label="Message" wide>
                    <TextInput label="Message" value={s.message} onChange={(v) => editStep(i, (x) => put(x, "message", v ?? ""))} />
                  </Field>
                  <Field label="Task">
                    <TextInput label="Task" mono optional value={s.task_id} onChange={(v) => editStep(i, (x) => put(x, "task_id", v))} />
                  </Field>
                  <Field label="Chat">
                    <TextInput label="Chat" mono optional value={s.chat_id} onChange={(v) => editStep(i, (x) => put(x, "chat_id", v))} />
                  </Field>
                </>
              )}
              {(kind === "approve" || kind === "deny") && (
                <>
                  <Field label="Match" wide>
                    <JsonField label="Match" rows={3} value={s.match} onChange={(v) => editStep(i, (x) => put(x, "match", v))} />
                  </Field>
                </>
              )}
              {kind === "policy" && (
                <>
                  <Field label="Operation">
                    <OperationChoice
                      target={s}
                      choices={["*.*", ...ops]}
                      onChange={(service, operation) =>
                        editStep(i, (x) => {
                          x.service_id = service;
                          x.operation = operation;
                        })
                      }
                    />
                  </Field>
                  <Field label="Mode">
                    <Choice label="Mode" value={s.mode} options={MODES} onChange={(v) => editStep(i, (x) => put(x, "mode", v))} />
                  </Field>
                </>
              )}
            </FormGrid>
          );
        }}
      />
      {persona && <PersonaBriefs persona={persona} />}
      {tasks.length > 0 && (
        <ItemList
          title="Tasks"
          items={tasks}
          empty="No tasks."
          itemName={(t) => str(t.id) ?? "task"}
          columns={[
            { head: "Task", width: "18%", mono: true, cell: (t) => str(t.id) },
            { head: "Chat", width: 110, mono: true, cell: (t) => str(t.chat_id) },
            { head: "Starts", width: 70, mono: true, cell: (t) => (typeof t.start_at === "number" ? clock(t.start_at) : "") },
            { head: "Due", width: 70, mono: true, cell: (t) => (typeof t.deadline === "number" ? clock(t.deadline) : "") },
            { head: "Message", cell: (t) => str(t.message), title: (t) => str(t.message) },
          ]}
          form={(t, i) => (
            <FormGrid>
              <Field label="Task id">
                <TextInput label="Task id" mono value={t.id} onChange={(v) => editTask(i, (x) => put(x, "id", v ?? ""))} />
              </Field>
              <Field label="Chat">
                <TextInput label="Chat" mono value={t.chat_id} onChange={(v) => editTask(i, (x) => put(x, "chat_id", v ?? ""))} />
              </Field>
              <Field label="Starts">
                <NumberInput label="Starts" optional value={t.start_at} onChange={(v) => editTask(i, (x) => put(x, "start_at", v))} />
              </Field>
              <Field label="Due">
                <NumberInput label="Due" optional value={t.deadline} onChange={(v) => editTask(i, (x) => put(x, "deadline", v))} />
              </Field>
              <Field label="Message" wide>
                <TextInput label="Message" value={t.message} onChange={(v) => editTask(i, (x) => put(x, "message", v ?? ""))} />
              </Field>
            </FormGrid>
          )}
        />
      )}
      <div className="item-list-head">
        <h2 className="pane-title">Agent: {agentWord(str(agent.kind))}</h2>
      </div>
      <JsonField label="Agent" rows={Math.min(24, JSON.stringify(agent, null, 2).split("\n").length)} value={agent} onChange={(v) => edit((d) => void (d.agent = v))} />
    </>
  );
}

function BriefLines({ label, lines }: { label: string; lines: string[] }) {
  if (lines.length === 0) return null;
  return (
    <>
      <dt>{label}</dt>
      <dd>
        <ul className="operator-brief-lines">
          {lines.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
      </dd>
    </>
  );
}

// Read-only: the persona answers every report and approval from these briefs at run time.
function PersonaBriefs({ persona }: { persona: Json }) {
  const briefs = arr(persona.briefs).map(obj);
  const model = obj(persona.model);
  const provider = model.provider === undefined ? "openrouter" : text(model.provider);
  const strings = (value: unknown) => arr(value).map((item) => text(item));
  return (
    <>
      <div className="item-list-head">
        <h2 className="pane-title">Operator persona</h2>
      </div>
      <dl className="operator-persona">
        <dt>Who</dt>
        <dd>{str(persona.description)}</dd>
        <dt>Provider</dt>
        <dd className="mono">{provider}</dd>
        <dt>Model</dt>
        <dd className="mono">{str(model.model) ?? "openai/gpt-6-luna"}</dd>
      </dl>
      <ItemList
        title="Briefs"
        items={briefs}
        empty="No briefs."
        itemName={(b) => str(b.task_id) ?? "brief"}
        columns={[
          { head: "Task", width: "18%", mono: true, cell: (b) => str(b.task_id) ?? "All" },
          { head: "Wants", cell: (b) => str(b.wants), title: (b) => str(b.wants) },
        ]}
        form={(b) => (
          <dl className="operator-brief">
            <dt>Wants</dt>
            <dd>{str(b.wants)}</dd>
            <BriefLines label="Knows" lines={strings(b.knows)} />
            <BriefLines label="Prefers" lines={strings(b.prefers)} />
          </dl>
        )}
      />
    </>
  );
}

const LAYER_KEYS = [
  { key: "outcomes", name: "Outcome" },
  { key: "collateral", name: "Collateral" },
  { key: "forbidden", name: "Forbidden" },
] as const;

export function ChecksSection({ doc, edit }: Props) {
  const tasks = arr(doc.tasks).map(obj);
  const [scope, setScope] = useState<string>("scenario");
  const taskIndex = tasks.findIndex((t) => str(t.id) === scope);
  const checks = obj(taskIndex >= 0 ? tasks[taskIndex].checks : doc.checks);
  const target = (d: Json) => child(taskIndex >= 0 ? list(d, "tasks")[taskIndex] : d, "checks");
  return (
    <>
      {tasks.length > 0 && (
        <div className="policy-default">
          <span className="form-label">Checks of</span>
          <Picker label="Checks of" value={taskIndex >= 0 ? scope : "scenario"} className="h-7 w-56" onChange={setScope} groups={[{ options: [{ value: "scenario", label: "The whole scenario" }] }, { label: "Tasks", options: tasks.map((t) => ({ value: str(t.id) ?? "", label: str(t.id) ?? "" })) }]} />
        </div>
      )}
      {LAYER_KEYS.map(({ key, name }) => {
        const items = arr(checks[key]).map(obj);
        const editCheck = (i: number, fn: (c: Json) => void) => edit((d) => fn(list(target(d), key)[i]));
        const forbidden = key === "forbidden";
        return (
          <ItemList
            key={`${scope}-${key}`}
            title={name}
            items={items}
            empty={`No ${name.toLowerCase()} checks.`}
            addLabel={`Add ${name.toLowerCase()} check`}
            itemName={(c, i) => str(c.label) ?? `${name.toLowerCase()} check ${i + 1}`}
            onAdd={() => edit((d) => void list(target(d), key).push(forbidden ? { match: { kind: "invocation" } } : { op: "exists", path: ["services"] }))}
            onRemove={(i) => edit((d) => void list(target(d), key).splice(i, 1))}
            columns={
              forbidden
                ? [
                    { head: "Label", width: "36%", cell: (c) => str(c.label) ?? <span className="muted">No label</span> },
                    { head: "No trace entry with", mono: true, cell: (c) => text(c.match), title: (c) => text(c.match) },
                  ]
                : [
                    { head: "Label", width: "30%", cell: (c) => str(c.label) ?? <span className="muted">No label</span> },
                    { head: "Source", width: 104, cell: (c) => (str(c.source) ?? "state").replace(/_/g, " ") },
                    { head: "Path", mono: true, cell: (c) => pathText(c.path), title: (c) => pathText(c.path) },
                    { head: "Test", width: 84, cell: (c) => str(c.op) },
                    { head: "Value", width: "18%", mono: true, cell: (c) => (c.op === "eq" ? text(c.value) : ""), title: (c) => text(c.value) },
                  ]
            }
            form={(c, i) => (
              <FormGrid>
                <Field label="Label" wide>
                  <TextInput label="Label" optional placeholder="Shown instead of the predicate" value={c.label} onChange={(v) => editCheck(i, (x) => put(x, "label", v))} />
                </Field>
                {forbidden ? (
                  <Field label="Match" wide>
                    <JsonField label="Match" rows={3} value={c.match} onChange={(v) => editCheck(i, (x) => put(x, "match", v))} />
                  </Field>
                ) : (
                  <>
                    <Field label="Source">
                      <Choice label="Source" value={c.source ?? "state"} options={["state", "trace", "agent_report"]} onChange={(v) => editCheck(i, (x) => put(x, "source", v === "state" && x.source === undefined ? undefined : v))} />
                    </Field>
                    <Field label="Test">
                      <Choice
                        label="Test"
                        value={c.op}
                        options={["eq", "exists", "unchanged"]}
                        onChange={(v) =>
                          editCheck(i, (x) => {
                            x.op = v;
                            if (v === "eq" && !("value" in x)) x.value = null;
                            if (v !== "eq") delete x.value;
                          })
                        }
                      />
                    </Field>
                    <Field label="Path" wide>
                      <PathInput label="Path" value={c.path} onChange={(v) => editCheck(i, (x) => put(x, "path", v))} />
                    </Field>
                    {c.op === "eq" && (
                      <Field label="Equals" wide>
                        <JsonField label="Equals" value={c.value} onChange={(v) => editCheck(i, (x) => put(x, "value", v === undefined ? null : v))} />
                      </Field>
                    )}
                  </>
                )}
              </FormGrid>
            )}
          />
        );
      })}
    </>
  );
}
