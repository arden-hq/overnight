import type { ActorDef } from "../lib/run";
import { clock } from "../lib/time";
import { Icon } from "./Icon";

const ROLE_WORD: Record<string, string> = { person: "Person", operator: "Operator", agent: "Agent" };

const text = (value: unknown) => (typeof value === "string" ? value : JSON.stringify(value));

export function PersonaCard({ actor, last }: { actor: ActorDef; last?: { t: number; count: number } | null }) {
  const persona = Object.entries(actor.persona);
  const grants = Object.entries(actor.operations);
  return (
    <div className="persona">
      <div className="persona-head">
        <span className="avatar">{actor.id.charAt(0).toUpperCase()}</span>
        <strong className="truncate">{actor.id}</strong>
        <span className="role">{ROLE_WORD[actor.role] ?? actor.role}</span>
      </div>
      {last !== undefined && <p className="persona-line">{last ? `Acted ${last.count} ${last.count === 1 ? "time" : "times"}, last at ${clock(last.t)}` : "Has not acted yet"}</p>}
      {persona.length > 0 && (
        <ul className="facts">
          {persona.map(([key, value]) => (
            <li key={key} className="shown">
              <Icon name="lock" size={12} />
              <span className="truncate" title={text(value)}>
                {text(value)}
              </span>
              <span className="reveal">{key.replace(/_/g, " ")}</span>
            </li>
          ))}
        </ul>
      )}
      {grants.length > 0 && (
        <ul className="facts">
          {grants.map(([service, ops]) => (
            <li key={service} className="shown">
              <Icon name="tool" size={12} />
              <span className="truncate">{Array.isArray(ops) ? ops.map(String).join(", ").replace("*", "every operation") : text(ops)}</span>
              <span className="reveal">{service}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
