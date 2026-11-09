import { obj, str, type Json } from "../lib/run";
import { AddButton, JsonField, RemoveButton, ValueInput } from "./fields";

const isRecordList = (value: unknown): value is unknown[] => Array.isArray(value) && value.every((item) => typeof item === "object" && item !== null && !Array.isArray(item));

function blank(value: unknown): unknown {
  if (typeof value === "string") return "";
  if (typeof value === "number") return 0;
  if (typeof value === "boolean") return false;
  if (Array.isArray(value)) return [];
  if (value && typeof value === "object") return {};
  return null;
}

function newRow(rows: Json[]): Json {
  const template = rows[0] ?? { id: "" };
  const row: Json = Object.fromEntries(Object.entries(template).map(([k, v]) => [k, blank(v)]));
  if ("id" in row) {
    let n = rows.length + 1;
    const taken = new Set(rows.map((r) => str(r.id)));
    while (taken.has(`new-${n}`)) n++;
    row.id = `new-${n}`;
  }
  return row;
}

function RecordGrid({ rows, onChange, name }: { rows: Json[]; onChange: (rows: Json[]) => void; name: string }) {
  const columns = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const setCell = (i: number, key: string, value: unknown) => onChange(rows.map((r, j) => (j === i ? { ...r, [key]: value } : r)));
  return (
    <div className="grid-edit">
      {rows.length > 0 && (
        <div className="table-wrap flush grid-scroll">
          <table className="dtable cell-table" style={{ minWidth: columns.length * 140 + 36 }}>
            <colgroup>
              {columns.map((c) => (
                <col key={c} />
              ))}
              <col style={{ width: 36 }} />
            </colgroup>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c} className="mono">
                    {c}
                  </th>
                ))}
                <th aria-label="Remove" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  {columns.map((c) => (
                    <td key={c}>{c in row ? <ValueInput label={`${name} ${i + 1} ${c}`} value={row[c]} onChange={(v) => setCell(i, c, v)} /> : <button type="button" className="cell-missing" onClick={() => setCell(i, c, "")}>Add</button>}</td>
                  ))}
                  <td className="item-remove">
                    <RemoveButton label={`Remove ${str(row.id) ?? `row ${i + 1}`}`} onClick={() => onChange(rows.filter((_, j) => j !== i))} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <AddButton label="Add row" onClick={() => onChange([...rows, newRow(rows)])} />
    </div>
  );
}

export function StateEditor({ value, onChange, name = "state", depth = 0 }: { value: unknown; onChange: (v: unknown) => void; name?: string; depth?: number }) {
  if (isRecordList(value)) return <RecordGrid rows={value.map(obj)} onChange={onChange} name={name} />;
  if (value && typeof value === "object" && !Array.isArray(value) && depth < 3) {
    const entries = Object.entries(value as Json);
    const scalars = entries.filter(([, v]) => v === null || typeof v !== "object");
    const nested = entries.filter(([, v]) => v !== null && typeof v === "object");
    const set = (key: string, v: unknown) => onChange({ ...(value as Json), [key]: v });
    return (
      <div className="state-edit">
        {scalars.length > 0 && (
          <div className="form-grid">
            {scalars.map(([key, v]) => (
              <label key={key} className="form-field">
                <span className="form-label mono">{key}</span>
                <ValueInput label={key} value={v} onChange={(nv) => set(key, nv)} />
              </label>
            ))}
          </div>
        )}
        {nested.map(([key, v]) => (
          <section key={key} className="state-section">
            <h4 className="sub mono">{key}</h4>
            <StateEditor value={v} onChange={(nv) => set(key, nv)} name={key} depth={depth + 1} />
          </section>
        ))}
      </div>
    );
  }
  return <JsonField label={name} value={value} onChange={onChange} rows={Array.isArray(value) ? 4 : 1} />;
}
