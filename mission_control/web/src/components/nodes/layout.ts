/**
 * Where the six subsystem nodes sit around the core, and how each status looks and reads.
 *
 * They form a flattened hexagon: LLM CORE above and NETWORK below the core, the other four at its sides,
 * pushed slightly towards or away from the camera so the ring has depth. Nothing sits in front of the core.
 */

import type { SubsystemId, SubsystemStatus, SubsystemView } from "../../types/events";

export interface NodePlacement {
  id: SubsystemId;
  label: string;
  position: [number, number, number];
}

export const NODE_LAYOUT: readonly NodePlacement[] = [
  { id: "llm", label: "LLM CORE", position: [0, 2.3, -0.6] },
  { id: "memory", label: "MEMORY", position: [-2.95, 1.15, -0.2] },
  { id: "tools", label: "TOOLS", position: [2.95, 1.15, -0.2] },
  { id: "voice", label: "VOICE", position: [-2.95, -1.2, 0.5] },
  { id: "system", label: "SYSTEM", position: [2.95, -1.2, 0.5] },
  { id: "network", label: "NETWORK", position: [0, -2.3, 0.3] },
];

export interface StatusLook {
  color: string;
  /** 0..1 brightness of the marker, halo and link. */
  glow: number;
  word: string;
}

// Amber marks what needs attention (degraded, offline); unavailable is grey and still: there is nothing
// to watch. "No data" is a quiet cyan, so the UI never shows health it has not been told about.
export const STATUS_LOOK: Record<SubsystemStatus, StatusLook> = {
  online: { color: "#6fe7f5", glow: 1, word: "ONLINE" },
  degraded: { color: "#ffb547", glow: 0.95, word: "DEGRADED" },
  offline: { color: "#ff8a3d", glow: 0.85, word: "OFFLINE" },
  unknown: { color: "#3f8b98", glow: 0.45, word: "NO DATA" },
  unavailable: { color: "#4b5864", glow: 0.3, word: "UNAVAILABLE" },
};

const MEMORY_OP: Record<string, string> = { retrieve: "Recalled", search: "Searched", store: "Stored" };
const VOICE_PHASE: Record<string, string> = {
  armed: "Wake word armed",
  wake_detected: "Wake word heard",
  collecting: "Listening",
  recording: "Recording",
  transcribing: "Transcribing",
  idle: "Mic idle",
  stopped: "Mic off",
};

const text = (value: unknown): string | null => (typeof value === "string" && value ? value : null);
const number = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** One short fact under a node's name, from its own block; null when there is nothing real to say. */
export function nodeFact(id: SubsystemId, view: SubsystemView, model: string | null): string | null {
  if (view.status === "unavailable") return text(view.reason);
  switch (id) {
    case "llm":
      return text(view.model) ?? model;
    case "memory": {
      const op = text(view.last_op);
      if (!op) return null;
      const count = number(view.count);
      return count === null ? MEMORY_OP[op] ?? op : `${MEMORY_OP[op] ?? op} ${count}`;
    }
    case "tools": {
      const active = Array.isArray(view.active) ? (view.active as unknown[]).filter((t) => typeof t === "string") : [];
      if (active.length) return `Running ${String(active[active.length - 1])}`;
      const recent = Array.isArray(view.recent) ? (view.recent as { tool?: unknown; status?: unknown }[]) : [];
      const last = recent[recent.length - 1];
      return last && text(last.tool) ? `${String(last.tool)}: ${String(last.status)}` : null;
    }
    case "system": {
      const cpu = number(view.cpu_percent);
      return cpu === null ? null : `CPU ${Math.round(cpu)}%`;
    }
    case "voice":
      if (view.speaking === true) return "Speaking";
      return VOICE_PHASE[text(view.input_phase) ?? ""] ?? (view.mode === "off" ? "Mic off" : null);
    case "network": {
      const groq = text(view.groq);
      if (groq === "down") return "Groq unreachable";
      const latency = number(view.latency_ms);
      if (groq === "up") return latency === null ? "Groq reachable" : `Groq ${Math.round(latency)} ms`;
      return null;
    }
    default:
      return null;
  }
}
