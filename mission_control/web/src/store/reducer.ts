/**
 * The whole Mission Control picture as one pure function of the frames received so far.
 *
 * Three rules keep it honest across drops and reconnects:
 * - every event is applied at most once (dedupe by seq), because a replay and live frames can overlap;
 * - a snapshot's fields are applied first and its recent_events folded on top in seq order, because the
 *   events can run ahead of the fields (still in flight to the backend tracker);
 * - each field remembers the seq that last set it, so an event delivered late never overwrites newer data.
 * The headline state comes from the backend (jarvis.state); only "offline" is decided here, from the socket.
 *
 * Subsystem blocks start from the backend tracker's snapshot and are then folded from live events with
 * the same rules, plus a few browser-only details the node panels show (call in flight, retry reason,
 * active tool status, recalled hits versus the stored fact total).
 *
 * The command bar and the approval panel keep their state here too, because it is folded from the same
 * events: this page's own commands (matched by command_id, since command.ack and command.rejected reach
 * every tab), the latest reply, and the answer this page sent to the open confirmation. An open
 * confirmation resent after a replay keeps its original, older seq, so confirm.request is the one event
 * that is still applied when the seq dedupe would drop it, unless this page already saw it resolved.
 */

import { isConfirmPayload, isServerEvent, isSubsystemStatus, isTaskView } from "../types/guards";
import type {
  BackendState,
  CommandRejectedEvent,
  ConfirmAnswerOutcome,
  ConfirmPayload,
  ConnectionStatus,
  ConfirmRequestEvent,
  JarvisState,
  NetworkStatusEvent,
  RequestEndEvent,
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
  ToolExecutionEvent,
  VoiceLevelEvent,
} from "../types/events";

export const LOG_LIMIT = 500;
/** Seqs above the floor remembered for dedupe; past this the oldest half is folded into the floor. */
const SEEN_LIMIT = 1024;
/** Meter-like events: they update panels but would drown the log. */
const UNLOGGED: ReadonlySet<string> = new Set(["system.metrics", "voice.level"]);
const RECENT_TOOLS = 5;
/** This page's commands kept for the command bar's chip. */
export const COMMANDS_KEPT = 20;
/** Confirmation ids seen resolved, so a late or resent request for one never reopens the panel. */
const RESOLVED_KEPT = 32;

export const SUBSYSTEM_IDS: readonly SubsystemId[] = ["llm", "memory", "tools", "system", "voice", "network"];
/** llm error kinds that mean Groq could not be reached at all (mission_control/state.py LLM_OFFLINE_KINDS). */
const LLM_OFFLINE_KINDS: ReadonlySet<string> = new Set(["network"]);
/** voice.input phases during which the microphone level is meaningful. */
const LEVEL_PHASES: ReadonlySet<string> = new Set(["armed", "wake_detected", "collecting"]);

/** One finished tool call in the TOOLS node's recent list. seq is null for entries from a snapshot. */
export interface ToolEntry {
  tool: string;
  status: string;
  seq: number | null;
  duration_ms?: number;
  target?: string;
}

/** The recent list as entries, whatever the backend or an older state put there. */
export function toolEntries(value: unknown): ToolEntry[] {
  if (!Array.isArray(value)) return [];
  const entries: ToolEntry[] = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null) continue;
    const raw = item as Record<string, unknown>;
    if (typeof raw.tool !== "string" || typeof raw.status !== "string") continue;
    const entry: ToolEntry = { tool: raw.tool, status: raw.status, seq: typeof raw.seq === "number" ? raw.seq : null };
    if (typeof raw.duration_ms === "number") entry.duration_ms = raw.duration_ms;
    if (typeof raw.target === "string") entry.target = raw.target;
    entries.push(entry);
  }
  return entries;
}

/** The latest microphone level (voice.level), shown only as a live meter. */
export interface VoiceLevel {
  rms: number;
  phase: VoiceLevelEvent["phase"];
  at: number;
}

/** Status changes of the socket, from the browser itself. */
export interface ConnectionEvent {
  type: "connection";
  status: ConnectionStatus;
}
/** This page sent command.submit (dispatched only once the socket took the frame). */
export interface CommandSentEvent {
  type: "command.sent";
  id: string;
  text: string;
  at: number;
}
/** This page sent confirm.answer for the open confirmation. */
export interface ConfirmSentEvent {
  type: "confirm.sent";
  id: string;
  approved: boolean;
}
export type LocalEvent = ConnectionEvent | CommandSentEvent | ConfirmSentEvent;

/**
 * One command this page sent. queued: sent, not yet accepted; running: accepted or its request started;
 * done: its request ended or its output arrived (outcome says how); rejected: refused, with the reason.
 */
export type CommandStatus = "queued" | "running" | "done" | "rejected";
export interface CommandEntry {
  id: string;
  text: string;
  sentAt: number;
  status: CommandStatus;
  reason: CommandRejectedEvent["reason"] | null;
  /** The request it started (request.start with this command_id). */
  requestId: string | null;
  outcome: RequestEndEvent["status"] | "output" | null;
}

/** The newest thing JARVIS said back: an assistant.message, or a browser slash command's output. */
export type LatestReply =
  | { kind: "message"; text: string; seq: number }
  | { kind: "output"; command: string; text: string; seq: number };

/** This page's answer to the open confirmation: on its way, then the server's result. */
export interface ConfirmAnswer {
  id: string;
  approved: boolean;
  phase: "sending" | "answered";
  result: ConfirmAnswerOutcome | null;
}
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
  /** The latest microphone level while listening for the wake word or collecting speech. */
  voiceLevel: VoiceLevel | null;
  /** The latest task.progress line for the current task. */
  taskProgress: { message: string; at: number } | null;
  /** This page's recent commands, oldest first. */
  commands: CommandEntry[];
  latestReply: LatestReply | null;
  confirmAnswer: ConfirmAnswer | null;
  /** Confirmation ids seen resolved (newest last). */
  resolvedConfirmIds: string[];
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
    llm: {
      status: "unknown", model: null, last_duration_ms: null, last_status: null, last_error_kind: null,
      in_flight: false, attempt: null, last_retry_reason: null, last_error_message: null,
    },
    memory: { status: "unknown", last_op: null, count: null, recall_op: null, recall_count: null, facts: null },
    tools: { status: "unknown", active: [], active_status: {}, recent: [], last_tool: null, last_target: null },
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
    voiceLevel: null,
    taskProgress: null,
    commands: [],
    latestReply: null,
    confirmAnswer: null,
    resolvedConfirmIds: [],
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
      if (input.status === state.connection) return state;
      // An answer still on its way when the link dropped may never get its result: give the buttons back.
      // If it did reach JARVIS, confirm.resolved arrives with the reconnect and closes the panel.
      return {
        ...state,
        connection: input.status,
        ...(state.confirmAnswer?.phase === "sending" && input.status !== "open" ? { confirmAnswer: null } : {}),
      };
    case "command.sent":
      return {
        ...state,
        commands: [...state.commands, {
          id: input.id, text: input.text, sentAt: input.at, status: "queued" as const, reason: null, requestId: null,
          outcome: null,
        }].slice(-COMMANDS_KEPT),
      };
    case "confirm.sent":
      if (state.pendingConfirmation?.id !== input.id) return state;
      return { ...state, confirmAnswer: { id: input.id, approved: input.approved, phase: "sending", result: null } };
    case "confirm.answer.result":
      if (state.confirmAnswer === null || state.confirmAnswer.id !== input.id) return state;
      return { ...state, confirmAnswer: { ...state.confirmAnswer, phase: "answered", result: input.result } };
    case "snapshot":
      return applySnapshot(state, input);
    case "auth.ok":
    case "pong":
    case "error":
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
  // The tracker keeps one op and count; the panels keep recalled hits and the stored total apart.
  const memory = result.memory;
  if (typeof memory.count === "number") {
    result.memory = memory.last_op === "store"
      ? { ...memory, facts: memory.count }
      : { ...memory, recall_op: memory.last_op ?? null, recall_count: memory.count };
  }
  const tools = result.tools;
  result.tools = {
    ...tools,
    active: Array.isArray(tools.active) ? tools.active.filter((name) => typeof name === "string") : [],
    active_status: {},
    recent: toolEntries(tools.recent),
  };
  return result;
}

/**
 * The snapshot's recent tool list was folded by the tracker from the same events that recent_events
 * replays, so keep only the part of it the replay does not cover. The tracker's entries carry no seq;
 * the overlap is the longest tail of its list that matches the head of the replayed results.
 */
function withoutReplayedTools(subsystems: Subsystems, recent: ServerEvent[]): Subsystems {
  const replayed = recent.filter(
    (event): event is ToolExecutionEvent => event.type === "tool.execution" && FINAL_TOOL.has(event.status),
  );
  const listed = toolEntries(subsystems.tools.recent);
  let overlap = Math.min(listed.length, replayed.length);
  while (overlap > 0) {
    const tail = listed.slice(listed.length - overlap);
    if (tail.every((entry, index) => entry.tool === replayed[index]!.tool && entry.status === replayed[index]!.status)) break;
    overlap -= 1;
  }
  return overlap === 0 ? subsystems : patch(subsystems, "tools", { recent: listed.slice(0, listed.length - overlap) });
}

function eventOfType<T extends ServerEvent["type"]>(value: unknown, type: T): Extract<ServerEvent, { type: T }> | null {
  return isServerEvent(value) && value.type === type ? (value as Extract<ServerEvent, { type: T }>) : null;
}

function applySnapshot(state: MissionState, snap: Snapshot): MissionState {
  const session = eventOfType(snap.session, "session.start");
  const metrics = eventOfType(snap.metrics, "system.metrics");
  const network = eventOfType(snap.network, "network.status");
  const recent = snap.recent_events.filter(isServerEvent).sort((a, b) => a.seq - b.seq);
  let next: MissionState = {
    ...state,
    jarvis: { state: snap.state.state, detail: snap.state.detail ?? null, since: snap.state.since },
    session,
    sessionEnded: false,
    subsystems: withoutReplayedTools(readSubsystems(snap.subsystems), recent),
    task: isTaskView(snap.task) ? snap.task : null,
    pendingConfirmation: isConfirmPayload(snap.pending_confirmation) ? snap.pending_confirmation : null,
    confirmAnswer: isConfirmPayload(snap.pending_confirmation) && snap.pending_confirmation.id === state.confirmAnswer?.id
      ? state.confirmAnswer : null,
    metrics,
    network,
    voiceLevel: null,
    taskProgress: null,
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

/**
 * The server resends an open confirmation to a tab that replayed past it, under the request's original
 * seq, which may already be covered by this page's dedupe. It is applied anyway, unless this page saw it
 * resolved or already shows it.
 */
function reopenConfirmation(state: MissionState, event: ConfirmRequestEvent): MissionState {
  if (state.resolvedConfirmIds.includes(event.id) || state.pendingConfirmation?.id === event.id) return state;
  return { ...state, pendingConfirmation: event, confirmAnswer: null };
}

function updateCommands(state: MissionState, change: (entry: CommandEntry) => CommandEntry): CommandEntry[] {
  let changed = false;
  const commands = state.commands.map((entry) => {
    const next = change(entry);
    if (next !== entry) changed = true;
    return next;
  });
  return changed ? commands : state.commands;
}

function newerReply(state: MissionState, seq: number): boolean {
  return state.latestReply === null || seq > state.latestReply.seq;
}

function applyEvent(state: MissionState, event: ServerEvent): MissionState {
  if (event.seq <= state.seqFloor || state.seen.includes(event.seq)) {
    return event.type === "confirm.request" ? reopenConfirmation(state, event) : state;
  }
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
      next.voiceLevel = null;
      subsystems = patch(subsystems, "tools", { active: [], active_status: {} });
      subsystems = patch(subsystems, "voice", { speaking: false });
      subsystems = patch(subsystems, "llm", { in_flight: false });
      break;
    case "request.start": {
      // A new request cannot still be running a tool from the last one.
      subsystems = patch(subsystems, "tools", { active: [], active_status: {} });
      const own = event.command_id;
      const requestId = event.request_id ?? null;
      next.commands = updateCommands(state, (entry) => {
        if (own !== undefined && entry.id === own) {
          return entry.status === "queued" || entry.status === "running" ? { ...entry, status: "running", requestId } : entry;
        }
        // An accepted command that ran no request of its own (a "continue" with nothing to resume) is
        // over once another request starts: the loop handles one line at a time.
        return entry.status === "running" && entry.requestId === null ? { ...entry, status: "done" } : entry;
      });
      break;
    }
    case "request.end":
      // Nor can an ended one still be calling the model: a call cut short (Ctrl-C, cancel) sends no
      // llm.call end, so the request's end is the last word on it.
      subsystems = patch(subsystems, "tools", { active: [], active_status: {} });
      subsystems = patch(subsystems, "llm", { in_flight: false });
      if (event.request_id !== undefined) {
        next.commands = updateCommands(state, (entry) =>
          entry.requestId === event.request_id && entry.status === "running"
            ? { ...entry, status: "done", outcome: event.status } : entry);
      }
      break;
    case "assistant.message":
      if (newerReply(state, event.seq)) next.latestReply = { kind: "message", text: event.text, seq: event.seq };
      break;
    case "command.ack":
      // Only ever moves a command forward: the ack may arrive after its request started, or even ended.
      next.commands = updateCommands(state, (entry) =>
        entry.id === event.command_id && entry.status === "queued" && event.status === "accepted"
          ? { ...entry, status: "running" } : entry);
      break;
    case "command.rejected":
      // Also after an ack: the loop checks a browser line again before it runs it.
      next.commands = updateCommands(state, (entry) =>
        entry.id === event.command_id && entry.status !== "done"
          ? { ...entry, status: "rejected", reason: event.reason } : entry);
      break;
    case "command.output":
      next.commands = updateCommands(state, (entry) =>
        entry.id === event.command_id && entry.status !== "rejected"
          ? { ...entry, status: "done", outcome: entry.outcome ?? "output" } : entry);
      if (newerReply(state, event.seq)) {
        next.latestReply = { kind: "output", command: event.command, text: event.text, seq: event.seq };
      }
      break;
    case "llm.call":
      subsystems = patch(subsystems, "llm", { model: event.model, attempt: event.attempt, in_flight: event.phase === "start" });
      if (event.phase === "end" && typeof event.ok === "boolean") {
        const kind = event.error_kind ?? null;
        subsystems = patch(subsystems, "llm", {
          ...(typeof event.duration_ms === "number" ? { last_duration_ms: event.duration_ms } : {}),
          ...(event.ok
            ? { status: "online", last_status: "success", last_error_kind: null, last_error_message: null }
            : {
              status: kind !== null && LLM_OFFLINE_KINDS.has(kind) ? "offline" : "degraded",
              last_status: "error", last_error_kind: kind,
            }),
        });
      }
      break;
    case "llm.retry":
      subsystems = patch(subsystems, "llm", { status: "degraded", last_status: "retry", last_retry_reason: event.reason });
      break;
    case "llm.error":
      subsystems = patch(subsystems, "llm", {
        status: LLM_OFFLINE_KINDS.has(event.kind) ? "offline" : "degraded", last_status: "error",
        last_error_kind: event.kind, last_error_message: event.message, in_flight: false,
      });
      break;
    case "tool.execution":
      subsystems = foldTool(subsystems, event);
      break;
    case "memory.activity": {
      const count = typeof event.count === "number" ? event.count : null;
      subsystems = patch(subsystems, "memory", {
        status: "online", last_op: event.op, count,
        ...(event.op === "store"
          ? (count === null ? {} : { facts: count })
          : { recall_op: event.op, recall_count: count }),
      });
      break;
    }
    case "task.update":
      if (newer("task")) {
        if (event.task === null || event.task.id !== state.task?.id) next.taskProgress = null;
        next.task = event.task;
        claim("task");
      }
      break;
    case "task.progress":
      next.taskProgress = { message: event.message, at: event.timestamp };
      break;
    case "voice.level":
      next.voiceLevel = { rms: event.rms, phase: event.phase, at: event.timestamp };
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
        ...(event.message !== undefined ? { input_message: event.message } : {}),
        ...(status === "unknown" || status === "degraded" ? { status: "online" } : {}),
      });
      if (event.mode === "off" || !LEVEL_PHASES.has(event.phase)) next.voiceLevel = null;
      break;
    }
    case "confirm.request":
      if (!state.resolvedConfirmIds.includes(event.id)) {
        if (state.confirmAnswer !== null && state.confirmAnswer.id !== event.id) next.confirmAnswer = null;
        next.pendingConfirmation = event;
      }
      break;
    case "confirm.resolved":
      next.resolvedConfirmIds = [...state.resolvedConfirmIds.filter((id) => id !== event.id), event.id].slice(-RESOLVED_KEPT);
      if (state.pendingConfirmation?.id === event.id) next.pendingConfirmation = null;
      if (state.confirmAnswer?.id === event.id) next.confirmAnswer = null;
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

function foldTool(subsystems: Subsystems, event: ToolExecutionEvent): Subsystems {
  const { tool, status } = event;
  const block = subsystems.tools;
  const active = Array.isArray(block.active) ? (block.active as string[]).filter((name) => name !== tool) : [];
  const activeStatus: Record<string, string> = { ...(block.active_status as Record<string, string> | undefined) };
  delete activeStatus[tool];
  const last = { last_tool: tool, ...(event.target !== undefined ? { last_target: event.target } : {}) };
  if (ACTIVE_TOOL.has(status) && tool) {
    return patch(subsystems, "tools", {
      ...last, active: [...active, tool], active_status: { ...activeStatus, [tool]: status },
      ...(block.status === "unknown" ? { status: "online" } : {}),
    });
  }
  if (FINAL_TOOL.has(status)) {
    const recent = toolEntries(block.recent);
    const entry: ToolEntry = { tool, status, seq: event.seq };
    if (event.duration_ms !== undefined) entry.duration_ms = event.duration_ms;
    if (event.target !== undefined) entry.target = event.target;
    const listed = recent.some((item) => item.seq === event.seq);
    return patch(subsystems, "tools", {
      ...last,
      active,
      active_status: activeStatus,
      recent: listed ? recent : [...recent, entry].slice(-RECENT_TOOLS),
      status: status === "error" ? "degraded" : "online",
    });
  }
  return patch(subsystems, "tools", last);
}
