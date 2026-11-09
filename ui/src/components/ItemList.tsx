import { useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { Json } from "../lib/run";
import { AddButton, RemoveButton } from "./fields";

export interface Column {
  head: string;
  width?: number | string;
  mono?: boolean;
  cell: (item: Json, index: number) => ReactNode;
  title?: (item: Json, index: number) => string | undefined;
}

export function ItemList({ title, items, columns, form, onAdd, onRemove, addLabel, empty, itemName }: { title?: ReactNode; items: Json[]; columns: Column[]; form: (item: Json, index: number) => ReactNode; onAdd?: () => void; onRemove?: (index: number) => void; addLabel?: string; empty: string; itemName: (item: Json, index: number) => string }) {
  const [picked, setPicked] = useState<number | null>(null);
  const open = picked !== null && picked < items.length ? picked : null;
  return (
    <section className="item-list">
      {(title || onAdd) && (
        <div className="item-list-head">
          {title && <h2 className="pane-title">{title}</h2>}
          {onAdd && (
            <AddButton
              label={addLabel ?? "Add"}
              onClick={() => {
                onAdd();
                setPicked(items.length);
              }}
            />
          )}
        </div>
      )}
      {items.length === 0 ? (
        <p className="empty">{empty}</p>
      ) : (
        <div className="table-wrap flush">
          <table className="dtable item-table">
            <colgroup>
              {columns.map((c) => (
                <col key={c.head} style={c.width !== undefined ? { width: c.width } : undefined} />
              ))}
              {onRemove && <col style={{ width: 36 }} />}
            </colgroup>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c.head}>{c.head}</th>
                ))}
                {onRemove && <th aria-label="Remove" />}
              </tr>
            </thead>
            <tbody>
              {items.map((item, i) => [
                <tr key={i} className={cn("item-row", open === i && "on")} aria-selected={open === i} onClick={() => setPicked(open === i ? null : i)}>
                  {columns.map((c) => (
                    <td key={c.head} className={cn("truncate", c.mono && "mono")} title={c.title?.(item, i)}>
                      {c.cell(item, i)}
                    </td>
                  ))}
                  {onRemove && (
                    <td className="item-remove">
                      <RemoveButton
                        label={`Remove ${itemName(item, i)}`}
                        onClick={() => {
                          onRemove(i);
                          setPicked(null);
                        }}
                      />
                    </td>
                  )}
                </tr>,
                open === i && (
                  <tr key={`${i}-form`} className="item-form-row">
                    <td colSpan={columns.length + (onRemove ? 1 : 0)}>
                      <div className="item-form" aria-label={`Edit ${itemName(item, i)}`}>
                        {form(item, i)}
                      </div>
                    </td>
                  </tr>
                ),
              ])}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
