/**
 * The whole Mission Control picture as one pure function of the frames received so far.
 *
 * Three rules keep it honest across drops and reconnects:
 * - every event is applied at most once (dedupe by seq), because a replay and live frames can overlap;
 * - a snapshot's fields are applied first and its recent_events folded on top in seq order, because the
 *   events can run ahead of the fields (still in flight to the backend tracker);
 * - each field remembers the seq that last set it, so an event delivered late never overwrites newer data.
 * The headline state comes from the backend (jarvis.state); only "offline" is decided here, from the socket.
 */

import { isConfirmPayload, isServerEvent, isSubsystemStatus, isTaskView } from "../types/guards";
import type {
  BackendState,
  ConfirmPayload,
  ConnectionStatus,
  JarvisState,
  NetworkStatusEvent,
  ServerEvent,
  ServerFrame,
  SessionStartEvent,
  Snapshot,
  SubsystemId,
  SubsystemStatus,
  SubsystemView,
  Subsystems,
  SystemMetricsEvent,
  TaskView,
} from "../types/events";

export const LOG_LIMIT = 500;
/** Seqs above the floor remembered for dedupe; past this the oldest half is folded into the floor. */
const SEEN_LIMIT = 1024;
/** Meter-like events: they update panels but would drown the log. */
const UNLOGGED: ReadonlySet<string> = new Set(["system.metrics", "voice.level"]);
const RECENT_TOOLS = 5;

export const SUBSYSTEM_IDS: readonly SubsystemId[] = ["llm", "memory", "tools", "system", "voice", "network"];

/** Status changes of the socket, from the browser itself. */
export interface ConnectionEvent {
  type: "connection";
  status: ConnectionStatus;
}
export type LocalEvent = ConnectionEvent;
export type MissionInput = ServerFrame | LocalEvent;

export interface HeadlineState {
  state: BackendState;
  detail: string | null;
  since: number;
}

interface FieldSeq {
  state: number;
  session: number;
  metrics: number;
  network: number;
  task: number;
}

export interface MissionState {
  connection: ConnectionStatus;
  /** null until the first snapshot or jarvis.state arrives. */
  jarvis: HeadlineState | null;
  session: SessionStartEvent | null;
  sessionEnded: boolean;
  subsystems: Subsystems;
  task: TaskView | null;
  pendingConfirmation: ConfirmPayload | null;
  metrics: SystemMetricsEvent | null;
  network: NetworkStatusEvent | null;
  /** Recent events in seq order, newest last, at most LOG_LIMIT. */
  log: ServerEvent[];
  /** Highest seq seen: sent as last_seq when the socket resumes. */
  lastSeq: number;
  /** Every seq at or below this is covered (applied, or superseded by a snapshot). */
  seqFloor: number;
  /** Seqs above the floor that were applied. */
  seen: number[];
  fieldSeq: FieldSeq;
}

function unknownSubsystems(): Subsystems {
  return {
    llm: { status: "unknown", model: null, last_duration_ms: null, last_status: null, last_error_kind: null },
    memory: { status: "unknown", last_op: null, count: null },
    tools: { status: "unknown", active: [], recent: [] },
    system: { status: "unknown", cpu_percent: null, memory_percent: null, disk_percent: null },
    voice: { status: "unknown", mode: null, input_phase: null, speaking: false },
    network: { status: "unknown", groq: null, source: null, latency_ms: null, checked_at: null },
  };
}

export function initialMissionState(): MissionState {
  return {
    connection: "connecting",
    jarvis: null,
    session: null,
    sessionEnded: false,
    subsystems: unknownSubsystems(),
    task: null,
    pendingConfirmation: null,
    metrics: null,
    network: null,
    log: [],
    lastSeq: 0,
    seqFloor: 0,
    seen: [],
    fieldSeq: { state: 0, session: 0, metrics: 0, network: 0, task: 0 },
  };
}

/** What the core should show: the backend's state while connected, "offline" once the link is lost. */
export function displayState(state: MissionState): JarvisState {
  if (state.connection === "open" || state.connection === "connecting") return state.jarvis?.state ?? "idle";
  return "offline";
}

export function reduce(state: MissionState, input: MissionInput): MissionState {
  switch (input.type) {
    case "connection":
      return input.status === state.connection ? state : { ...state, connection: input.status };
    case "snapshot":
      return applySnapshot(state, input);
    case "auth.ok":
    case "pong":
    case "error":
    case "confirm.answer.result":
      return state;
    default:
      return applyEvent(state, input);
  }
}

// --- snapshot --------------------------------------------------------------------------------------

function readSubsystems(raw: Record<string, unknown>): Subsystems {
  const result = unknownSubsystems();
  for (const id of SUBSYSTEM_IDS) {
    const block = raw[id];
    if (typeof block === "object" && block !== null && !Array.isArray(block)) {
      const status = (block as Record<string, unknown>).status;
      result[id] = { ...result[id], ...(block as Record<string, unknown>), status: isSubsystemStatus(status) ? status : "unknown" };
    }
  }
  return result;
}

function eventOfType<T extends ServerEvent["type"]>(value: unknown, type: T): Extract<ServerEvent, { type: T }> | null {
  return isServerEvent(value) && value.type === type ? (value as Extract<ServerEvent, { type: T }>) : null;
}

function applySnapshot(state: MissionState, snap: Snapshot): MissionState {
  const session = eventOfType(snap.session, "session.start");
  const metrics = eventOfType(snap.metrics, "system.metrics");
  const network = eventOfType(snap.network, "network.status");
  let next: MissionState = {
    ...state,
    jarvis: { state: snap.state.state, detail: snap.state.detail ?? null, since: snap.state.since },
    session,
    sessionEnded: false,
    subsystems: readSubsystems(snap.subsystems),
    task: isTaskView(snap.task) ? snap.task : null,
    pendingConfirmation: isConfirmPayload(snap.pending_confirmation) ? snap.pending_confirmation : null,
    metrics,
    network,
    // The snapshot's state is the tracker's latest, so no jarvis.state at or below its seq may replace it.
    // The task has no seq of its own: recent task.update events, folded in order, end on the newest.
    fieldSeq: {
      state: snap.seq,
      session: session?.seq ?? 0,
      metrics: metrics?.seq ?? 0,
      network: network?.seq ?? 0,
      task: 0,
    },
  };
  const recent = snap.recent_events.filter(isServerEvent).sort((a, b) => a.seq - b.seq);
  for (const event of recent) next = applyEvent(next, event);
  const floor = Math.max(next.seqFloor, snap.seq);
  return { ...next, seqFloor: floor, seen: next.seen.filter((seq) => seq > floor), lastSeq: Math.max(next.lastSeq, snap.seq) };
}

// --- events ----------------------------------------------------------------------------------------

function remember(state: MissionState, seq: number): Pick<MissionState, "seen" | "seqFloor" | "lastSeq"> {
  let seen = [...state.seen, seq];
  let seqFloor = state.seqFloor;
  if (seen.length > SEEN_LIMIT) {
    seen.sort((a, b) => a - b);
    const dropped = seen.slice(0, SEEN_LIMIT / 2);
    seen = seen.slice(SEEN_LIMIT / 2);
    seqFloor = Math.max(seqFloor, dropped[dropped.length - 1] ?? seqFloor);
  }
  return { seen, seqFloor, lastSeq: Math.max(state.lastSeq, seq) };
}

function insertBySeq(log: ServerEvent[], event: ServerEvent): ServerEvent[] {
  let index = log.length;
  while (index > 0 && log[index - 1]!.seq > event.seq) index -= 1;
  const next = [...log.slice(0, index), event, ...log.slice(index)];
  return next.length > LOG_LIMIT ? next.slice(next.length - LOG_LIMIT) : next;
}

function patch(subsystems: Subsystems, id: SubsystemId, fields: Partial<SubsystemView>): Subsystems {
  return { ...subsystems, [id]: { ...subsystems[id], ...fields } };
}

function applyEvent(state: MissionState, event: ServerEvent): MissionState {
  if (event.seq <= state.seqFloor || state.seen.includes(event.seq)) return state;
  const next: MissionState = { ...state, ...remember(state, event.seq) };
  if (!UNLOGGED.has(event.type)) next.log = insertBySeq(state.log, event);
  const newer = (field: keyof FieldSeq) => event.seq > state.fieldSeq[field];
  const claim = (field: keyof FieldSeq) => {
    next.fieldSeq = { ...next.fieldSeq, [field]: event.seq };
  };
  let subsystems = state.subsystems;

  switch (event.type) {
    case "jarvis.state":
      if (newer("state")) {
        next.jarvis = { state: event.state, detail: event.detail ?? null, since: event.since };
        claim("state");
      }
      break;
    case "session.start":
      if (newer("session")) {
        next.session = event;
        next.sessionEnded = false;
        claim("session");
      }
      subsystems = patch(subsystems, "llm", { model: event.model });
      if (!event.voice_available) subsystems = patch(subsystems, "voice", { status: "unavailable" });
      else if (subsystems.voice.status === "unknown") subsystems = patch(subsystems, "voice", { status: "online" });
      subsystems = patch(subsystems, "memory", { status: MEMORY_STATUS[event.memory_status] });
      break;
    case "session.end":
      next.sessionEnded = true;
      subsystems = patch(subsystems, "tools", { active: [] });
      subsystems = patch(subsystems, "voice", { speaking: false });
      break;
    case "request.start":
    case "request.end":
      // A request that started or ended cannot still be running a tool.
      subsystems = patch(subsystems, "tools", { active: [] });
      break;
    case "llm.call":
      subsystems = patch(subsystems, "llm", { model: event.model });
      if (event.phase === "end" && typeof event.ok === "boolean") {
        subsystems = patch(subsystems, "llm", {
          ...(typeof event.duration_ms === "number" ? { last_duration_ms: event.duration_ms } : {}),
          ...(event.ok
            ? { status: "online", last_status: "success", last_error_kind: null }
            : { status: "degraded", last_status: "error", last_error_kind: event.error_kind ?? null }),
        });
      }
      break;
    case "llm.retry":
      subsystems = patch(subsystems, "llm", { status: "degraded", last_status: "retry" });
      break;
    case "llm.error":
      subsystems = patch(subsystems, "llm", {
        status: event.kind === "network" ? "offline" : "degraded", last_status: "error", last_error_kind: event.kind,
      });
      break;
    case "tool.execution":
      subsystems = foldTool(subsystems, event.tool, event.status);
      break;
    case "memory.activity":
      subsystems = patch(subsystems, "memory", { status: "online", last_op: event.op, count: event.count ?? null });
      break;
    case "task.update":
      if (newer("task")) {
        next.task = event.task;
        claim("task");
      }
      break;
    case "voice.output":
      subsystems = patch(subsystems, "voice",
        event.phase === "start" ? { status: "online", speaking: true }
          : event.phase === "end" ? { status: "online", speaking: false }
            : event.phase === "error" ? { status: "degraded", speaking: false } : {});
      break;
    case "voice.input": {
      const status = subsystems.voice.status;
      subsystems = patch(subsystems, "voice", {
        mode: event.mode, input_phase: event.phase,
        ...(status === "unknown" || status === "degraded" ? { status: "online" } : {}),
      });
      break;
    }
    case "confirm.request":
      next.pendingConfirmation = event;
      break;
    case "confirm.resolved":
      if (state.pendingConfirmation?.id === event.id) next.pendingConfirmation = null;
      break;
    case "system.metrics":
      if (newer("metrics")) {
        next.metrics = event;
        claim("metrics");
        subsystems = patch(subsystems, "system", {
          status: "online", cpu_percent: event.cpu_percent, memory_percent: event.memory_percent,
          disk_percent: event.disk_percent,
        });
      }
      break;
    case "network.status":
      if (newer("network")) {
        next.network = event;
        claim("network");
        subsystems = patch(subsystems, "network", {
          status: event.groq === "up" ? "online" : event.groq === "down" ? "offline" : "unknown",
          groq: event.groq, source: event.source, latency_ms: event.latency_ms ?? null, checked_at: event.checked_at,
        });
      }
      break;
    default:
      break;
  }
  if (subsystems !== state.subsystems) next.subsystems = subsystems;
  return next;
}

const MEMORY_STATUS: Record<SessionStartEvent["memory_status"], SubsystemStatus> = {
  ok: "online",
  corrupted: "degraded",
  disabled: "unavailable",
};

const ACTIVE_TOOL = new Set(["running", "awaiting_confirmation"]);
const FINAL_TOOL = new Set(["success", "error", "denied", "partial", "unverified"]);

function foldTool(subsystems: Subsystems, tool: string, status: string): Subsystems {
  const block = subsystems.tools;
  const active = Array.isArray(block.active) ? (block.active as string[]).filter((name) => name !== tool) : [];
  if (ACTIVE_TOOL.has(status) && tool) {
    return patch(subsystems, "tools", { active: [...active, tool], ...(block.status === "unknown" ? { status: "online" } : {}) });
  }
  if (FINAL_TOOL.has(status)) {
    const recent = Array.isArray(block.recent) ? (block.recent as unknown[]) : [];
    return patch(subsystems, "tools", {
      active,
      recent: [...recent, { tool, status }].slice(-RECENT_TOOLS),
      status: status === "error" ? "degraded" : "online",
    });
  }
  return subsystems;
}
