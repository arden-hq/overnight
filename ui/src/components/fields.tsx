import { useContext, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { parsePath, pathText } from "../lib/edit";
import { DraftInputsContext, type DraftPath } from "../lib/drafts";
import { obj, type Json } from "../lib/run";
import { Icon } from "./Icon";
import { Picker } from "./Picker";

export function FormGrid({ children }: { children: ReactNode }) {
  return <div className="form-grid">{children}</div>;
}

export function Field({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <label className={cn("form-field", wide && "wide")}>
      <span className="form-label">{label}</span>
      {children}
    </label>
  );
}

export function TextInput({ value, onChange, optional, mono, placeholder, className, label }: { value: unknown; onChange: (v: string | undefined) => void; optional?: boolean; mono?: boolean; placeholder?: string; className?: string; label?: string }) {
  return (
    <Input
      aria-label={label}
      className={cn("h-7", mono && "font-mono text-[12px]", className)}
      value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)}
      placeholder={placeholder ?? (optional ? "None" : undefined)}
      onChange={(e) => onChange(optional && e.target.value === "" ? undefined : e.target.value)}
    />
  );
}

export function NumberInput({ value, onChange, optional, step, label }: { value: unknown; onChange: (v: number | undefined) => void; optional?: boolean; step?: number; label?: string }) {
  return (
    <Input
      aria-label={label}
      type="number"
      step={step ?? 1}
      className="h-7 tabular-nums"
      value={typeof value === "number" ? value : ""}
      placeholder={optional ? "None" : undefined}
      onChange={(e) => onChange(e.target.value === "" ? (optional ? undefined : 0) : Number(e.target.value))}
    />
  );
}

export function ListInput({ value, onChange, placeholder, label }: { value: unknown; onChange: (v: string[]) => void; placeholder?: string; label?: string }) {
  const items = Array.isArray(value) ? value.map(String) : [];
  const [text, setText] = useState(items.join(", "));
  const [seen, setSeen] = useState(items.join(", "));
  if (items.join(", ") !== seen) {
    setSeen(items.join(", "));
    setText(items.join(", "));
  }
  return (
    <Input
      aria-label={label}
      className="h-7 font-mono text-[12px]"
      value={text}
      placeholder={placeholder ?? "None"}
      onChange={(e) => {
        setText(e.target.value);
        const next = e.target.value
          .split(",")
          .map((part) => part.trim())
          .filter(Boolean);
        setSeen(next.join(", "));
        onChange(next);
      }}
    />
  );
}

interface JsonFieldProps {
  value: unknown;
  onChange: (v: unknown) => void;
  path: DraftPath;
  validate?: (v: unknown) => string | undefined;
  rows?: number;
  optional?: boolean;
  label?: string;
  className?: string;
  inputClassName?: string;
}

export function JsonField({ value, onChange, path, validate, rows = 1, optional, label, className, inputClassName }: JsonFieldProps) {
  const inputs = useContext(DraftInputsContext);
  const revision = inputs?.revision ?? 0;
  const format = (value: unknown) => JSON.stringify(value, null, rows > 1 ? 2 : undefined) ?? "";
  const shown = format(value);
  const initial = { text: shown, canonical: shown, revision };
  const [buffer, setBuffer] = useState(initial);
  const current = buffer.canonical === shown && buffer.revision === revision ? buffer : initial;
  if (current !== buffer) setBuffer(current);
  if (!inputs) throw new Error("JsonField requires a scenario draft");
  const { setInvalid } = inputs;
  const invalid = inputs.invalid.get(JSON.stringify(path));

  const commit = (next: string) => {
    let parsed: unknown;
    try {
      parsed = optional && next.trim() === "" ? undefined : JSON.parse(next, (_key, value: unknown) => {
        if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Numbers must be finite");
        return value;
      });
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : "Check the syntax";
      setInvalid(path, { text: next, problem: `Invalid JSON: ${detail}` });
      return;
    }
    const problem = validate?.(parsed);
    if (problem) {
      setInvalid(path, { text: next, problem });
      return;
    }
    setInvalid(path, null);
    setBuffer({ text: next, canonical: format(parsed), revision });
    onChange(parsed);
  };
  const common = { "aria-label": label, "aria-invalid": !!invalid || undefined, value: invalid?.text ?? current.text, spellCheck: false, placeholder: optional ? "None" : undefined };
  return (
    <span className={cn("json-field", className)}>
      {rows > 1 ? (
        <Textarea {...common} className="json-input" style={{ minHeight: rows * 18 + 16 }} onChange={(e) => commit(e.target.value)} />
      ) : (
        <Input {...common} className={cn("h-7 font-mono text-[12px]", inputClassName)} onChange={(e) => commit(e.target.value)} />
      )}
      {invalid && <span className="field-problem truncate">{invalid.problem}</span>}
    </span>
  );
}

const CELL_INPUT = "border-transparent bg-transparent shadow-none hover:border-border focus-visible:border-ring focus-visible:bg-card dark:bg-transparent dark:focus-visible:bg-card";

export function ValueInput({ value, onChange, path, label }: { value: unknown; onChange: (v: unknown) => void; path: DraftPath; label?: string }) {
  if (typeof value === "string") return <TextInput label={label} value={value} onChange={(v) => onChange(v ?? "")} className={CELL_INPUT} />;
  return <JsonField path={path} label={label} value={value} onChange={onChange} inputClassName={CELL_INPUT} />;
}

export function Choice({ value, options, onChange, none, label }: { value: unknown; options: (string | { value: string; label: string })[]; onChange: (v: string | undefined) => void; none?: string; label: string }) {
  const NONE = "\u0000none";
  const items = options.map((o) => (typeof o === "string" ? { value: o, label: o } : o));
  const current = typeof value === "string" && value ? value : none ? NONE : (items[0]?.value ?? NONE);
  if (typeof value === "string" && value && !items.some((o) => o.value === value)) items.unshift({ value, label: value });
  return <Picker label={label} value={current} className="h-7 w-full" onChange={(v) => onChange(v === NONE ? undefined : v)} groups={[{ options: [...(none ? [{ value: NONE, label: none }] : []), ...items] }]} />;
}

export function PathInput({ value, onChange, label }: { value: unknown; onChange: (v: (string | number)[]) => void; label?: string }) {
  const shown = pathText(value);
  const [text, setText] = useState(shown);
  const [seen, setSeen] = useState(shown);
  if (shown !== seen) {
    setSeen(shown);
    setText(shown);
  }
  return (
    <Input
      aria-label={label}
      className="h-7 font-mono text-[12px]"
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const next = parsePath(e.target.value);
        setSeen(pathText(next));
        onChange(next);
      }}
    />
  );
}

export function KeyValues({ value, onChange, path, keyLabel, addLabel }: { value: unknown; onChange: (v: Json) => void; path: DraftPath; keyLabel: string; addLabel: string }) {
  const entries = Object.entries(obj(value));
  const rebuild = (next: [string, unknown][]) => onChange(Object.fromEntries(next));
  return (
    <div className="kv-edit">
      {entries.map(([key, v], i) => (
        <div className="kv-edit-row" key={i}>
          <TextInput label={keyLabel} mono value={key} onChange={(k) => rebuild(entries.map((e, j) => (j === i ? [k ?? "", e[1]] : e)))} />
          <ValueInput path={[...path, key]} label={key} value={v} onChange={(nv) => rebuild(entries.map((e, j) => (j === i ? [e[0], nv] : e)))} />
          <RemoveButton label={`Remove ${key}`} onClick={() => rebuild(entries.filter((_, j) => j !== i))} />
        </div>
      ))}
      <AddButton label={addLabel} onClick={() => rebuild([...entries, [`field_${entries.length + 1}`, ""]])} />
    </div>
  );
}

export function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button variant="ghost" size="xs" className="add-btn" onClick={onClick}>
      <Icon name="plus" size={12} />
      {label}
    </Button>
  );
}

export function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button
      variant="ghost"
      size="icon-xs"
      aria-label={label}
      title={label}
      className="remove-btn"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
    >
      <Icon name="trash" size={12} />
    </Button>
  );
}
