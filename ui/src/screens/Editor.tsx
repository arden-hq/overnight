import { useState } from "react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs } from "@/components/ui/tabs";
import type { ScenarioDetail } from "../api/types";
import { scenarioPath, useCatalog, useResource } from "../lib/api";
import { useDraft, type DraftHandle } from "../lib/drafts";
import { href, type Section } from "../lib/router";
import { arr, obj, runStatus, str, type Json } from "../lib/run";
import { Icon, serviceIcon } from "../components/Icon";
import { JsonField } from "../components/fields";
import { MovingTabs } from "../components/MovingTabs";
import { RunButton } from "../components/OperationBar";
import { ChecksSection, EventsSection, OperatorSection, PeopleSection } from "../components/ScenarioSections";
import { StateEditor } from "../components/StateEditor";
import { StatusWord } from "../components/Status";
import { Unavailable } from "../components/Unavailable";

const SECTION_TITLE: Record<Section, string> = { services: "Services", events: "Events", people: "People", operator: "Operator and agent", checks: "Checks" };

export function Editor({ id, section }: { id: string; section: Section }) {
  const { data, error, reload } = useResource<ScenarioDetail>(scenarioPath(id));
  if (error && !data) return <Unavailable what={`scenario ${id}`} error={error} onRetry={reload} />;
  if (!data) return <p className="empty pad">Loading {id}…</p>;
  return <EditorPage id={id} section={section} detail={data} />;
}

function Validity({ draft, detail }: { draft: DraftHandle; detail: ScenarioDetail }) {
  const invalid = draft.dirty ? draft.problem?.kind === "invalid" : !detail.summary.valid;
  const valid = draft.dirty ? draft.valid : detail.summary.valid;
  const word = invalid ? "Invalid" : valid ? "Valid" : "Not checked";
  return (
    <span className={cn("sound", invalid ? "s-blocked" : valid ? "s-sound" : "s-unchecked")} title={detail.summary.error ?? undefined}>
      <Icon name={invalid ? "x" : valid ? "check" : "dash"} size={12} />
      {word}
    </span>
  );
}

function EditorPage({ id, section, detail }: { id: string; section: Section; detail: ScenarioDetail }) {
  const { refresh } = useCatalog();
  const draft = useDraft(id, detail, () => void refresh());
  const [raw, setRaw] = useState(false);
  const { summary } = detail;
  const last = summary.last_run;
  const doc = draft.doc;
  const problem = draft.problem;
  return (
    <div className="editor">
      <div className="pagehead">
        <h1 className="truncate">{SECTION_TITLE[section]}</h1>
        <span className="meta mono truncate">{summary.path}</span>
        {draft.dirty && <Badge className="unsaved-badge">Unsaved</Badge>}
        <span className="grow" />
        <Validity draft={draft} detail={detail} />
        {last && !draft.dirty && (
          <a className="meta link" href={href.run(last.id)}>
            Last run <StatusWord status={runStatus(last.verdict)} />
          </a>
        )}
        <Button variant={raw ? "secondary" : "ghost"} size="sm" aria-pressed={raw} onClick={() => setRaw(!raw)}>
          <Icon name="json" />
          JSON
        </Button>
        {draft.dirty && (
          <>
            <Button variant="ghost" size="sm" onClick={draft.discard} disabled={!!draft.busy}>
              Discard
            </Button>
            <Button variant="outline" size="sm" className="shadow-sm" onClick={() => void draft.validate()} disabled={!!draft.busy}>
              {draft.busy === "validate" ? "Checking…" : "Validate"}
            </Button>
          </>
        )}
        <Button size="sm" onClick={() => void draft.save()} disabled={!draft.dirty || !!draft.busy}>
          {draft.busy === "save" ? "Saving…" : "Save"}
        </Button>
        <RunButton scenario={summary} dirty={draft.dirty} />
      </div>
      {!summary.valid && !draft.dirty && (
        <div className="banner notice">
          <Icon name="x" />
          <span className="truncate" title={summary.error ?? undefined}>
            {summary.error}
          </span>
        </div>
      )}
      {problem && (
        <div className={cn("banner edit-problem", problem.kind === "stale" ? "notice" : "failure")} role="alert">
          <Icon name={problem.kind === "stale" ? "outage" : "x"} />
          <div className="edit-problem-body">
            <span className="truncate">{problem.kind === "stale" ? "The file changed on disk since it was opened. Saving would overwrite that change." : problem.message}</span>
            {problem.kind === "invalid" && problem.detail && <pre className="json small">{problem.detail}</pre>}
          </div>
          {problem.kind === "stale" && (
            <Button variant="outline" size="xs" className="ml-auto" onClick={draft.reloadFromDisk}>
              Reload from disk
            </Button>
          )}
        </div>
      )}
      <div className="editor-body">
        <div className="scenario-page">
          {raw ? (
            <JsonField label="Scenario JSON" rows={40} className="raw-json" value={doc} onChange={(v) => draft.replace(obj(v))} />
          ) : (
            <>
              {section === "services" && <Services detail={detail} doc={doc} edit={draft.edit} />}
              {section === "events" && <EventsSection doc={doc} edit={draft.edit} detail={detail} />}
              {section === "people" && <PeopleSection doc={doc} edit={draft.edit} detail={detail} />}
              {section === "operator" && <OperatorSection doc={doc} edit={draft.edit} detail={detail} />}
              {section === "checks" && <ChecksSection doc={doc} edit={draft.edit} detail={detail} />}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

type ServiceTab = "interface" | "data" | "json";

function Services({ detail, doc, edit }: { detail: ScenarioDetail; doc: Json; edit: (fn: (doc: Json) => void) => void }) {
  const [picked, setPicked] = useState(detail.services[0]?.id ?? "");
  const [tab, setTab] = useState<ServiceTab>("data");
  const svc = detail.services.find((s) => s.id === picked) ?? detail.services[0];
  if (!svc) return <p className="empty">No services.</p>;
  const index = arr(doc.services).findIndex((s) => str(obj(s).id) === svc.id);
  const state = index >= 0 ? obj(arr(doc.services)[index]).state : svc.state;
  const setState = (value: unknown) => edit((d) => void (obj(arr(d.services)[index]).state = value));
  return (
    <div className="svced">
      <div className="svced-list" role="tablist" aria-label="Services">
        {detail.services.map((s) => (
          <button key={s.id} type="button" role="tab" aria-selected={s.id === svc.id} className={cn("svced-item", s.id === svc.id && "on")} onClick={() => setPicked(s.id)}>
            <Icon name={serviceIcon(s.kind)} />
            <span className="truncate">{s.id}</span>
            <span className="svced-meta">{s.operations.length} operations</span>
          </button>
        ))}
      </div>
      <section className="svced-main">
        <div className="svced-head">
          <Icon name={serviceIcon(svc.kind)} className="svced-icon" />
          <strong>{svc.id}</strong>
          <span className="svced-id mono">{svc.kind}</span>
          <Tabs value={tab} onValueChange={(v) => setTab(v as ServiceTab)} className="ml-auto">
            <MovingTabs
              variant="line"
              value={tab}
              items={[
                { value: "data", label: "Data" },
                { value: "json", label: "JSON" },
                { value: "interface", label: "Interface" },
              ]}
            />
          </Tabs>
        </div>
        <div className="svced-pad">
          {tab === "interface" && (
            <table className="dtable">
              <colgroup>
                <col style={{ width: "22%" }} />
                <col style={{ width: 70 }} />
                <col />
                <col style={{ width: "28%" }} />
              </colgroup>
              <thead>
                <tr>
                  <th>Operation</th>
                  <th>Kind</th>
                  <th>Description</th>
                  <th>Arguments</th>
                </tr>
              </thead>
              <tbody>
                {svc.operations.map((op) => {
                  const props = Object.keys(obj(op.input_schema.properties));
                  const required = new Set(arr(op.input_schema.required).map(String));
                  return (
                    <tr key={op.name}>
                      <td className="mono truncate">{op.name}</td>
                      <td>{op.read_only ? <Badge variant="secondary">Read</Badge> : <Badge className="bg-agent/12 text-agent">Write</Badge>}</td>
                      <td className="truncate" title={op.description}>
                        {op.description}
                      </td>
                      <td className="mono muted truncate" title={props.join(", ")}>
                        {props.length ? props.map((p) => (required.has(p) ? p : `${p}?`)).join(", ") : "none"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {tab === "data" && (index >= 0 ? <StateEditor key={svc.id} value={state} onChange={setState} name={svc.id} /> : <p className="empty">This service is not in the draft.</p>)}
          {tab === "json" && index >= 0 && <JsonField key={svc.id} label={`${svc.id} state`} rows={24} value={state} onChange={setState} />}
        </div>
      </section>
    </div>
  );
}
