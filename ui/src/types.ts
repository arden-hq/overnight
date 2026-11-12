import type { Lane } from "./api/types";

export type { Lane };

export interface RowDef {
  lane: Lane;
  id: string;
  label: string;
  icon: string;
  tag?: string;
}

export type Tone = "fail" | "late" | undefined;

export interface Chip {
  id: string;
  lane: Lane;
  row: string;
  t: number;
  sequence: number;
  kind: string;
  label: string;
  mark?: string;
  tone: Tone;
  icon: string;
  lines: number[];
  task: string | null;
  service?: string;
}

export type RunStatus = "pass" | "fail" | "unknown" | "incomplete" | "ungraded" | "running";
