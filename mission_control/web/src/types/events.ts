/**
 * The Mission Control wire protocol, as the browser sees it.
 *
 * The backend's mission_control/protocol.py is the source of truth; these types mirror it and the binding
 * field list (event-fields.md). Every server event carries the envelope {v, seq, type, timestamp, request_id?}.
 * Frames are checked at runtime by ./guards.ts before anything reads them, so a type here is a promise the
 * guards keep, not a hope.
 */

export const PROTOCOL_VERSION = 1;

/** What JARVIS is doing. The backend sends all but "offline", which the browser shows when its socket is down. */
export type JarvisState = "idle" | "listening" | "processing" | "speaking" | "tool" | "error" | "offline";
export type BackendState = Exclude<JarvisState, "offline">;

export interface Envelope<T extends string> {
  v: 1;
  seq: number;
  type: T;
  timestamp: number;
  request_id?: string;
}

export type StoreStatus = "ok" | "corrupted" | "disabled";

export interface TaskStep {
  n: number;
  text: string;
  status: string;
}

export interface TaskView {
  id: string;
  goal: string;
  status: string;
  steps: TaskStep[];
  updated_at: number | string;
  paused_reason?: string | null;
  untrusted_plan: boolean;
}

export interface ReviewLine {
  k: string;
  t: string;
}

/** The approval a tool is waiting for: identical content to what the terminal prints. */
export interface ConfirmPayload {
  id: string;
  digest: string;
  tool: string;
  label: string;
  operation: string;
  affected_paths: string[];
  reason: string;
  hidden: number;
  lines: ReviewLine[];
  omitted: number;
  browser_approvable: boolean;
  /** Epoch ms; null when approvals never time out (JARVIS_CONFIRM_TIMEOUT=0). */
  expires_at: number | null;
}

export type SessionStartEvent = Envelope<"session.start"> & {
  version: string;
  provider: "groq";
  model: string;
  permission_mode: string;
  voice_available: boolean;
  voice_enabled: boolean;
  mic_available: boolean;
  wake_available: boolean;
  memory_status: StoreStatus;
  tasks_status: StoreStatus;
  developer_mode: boolean;
};
export type SessionEndEvent = Envelope<"session.end">;
export type JarvisStateEvent = Envelope<"jarvis.state"> & { state: BackendState; since: number; detail?: string | null };
export type RequestStartEvent = Envelope<"request.start"> & {
  source: "terminal" | "voice" | "browser";
  text: string;
  command_id?: string;
};
export type RequestEndEvent = Envelope<"request.end"> & {
  status: "complete" | "incomplete" | "cancelled" | "error";
  duration_ms: number;
  rounds?: number;
  error_kind?: string | null;
};
export type AssistantMessageEvent = Envelope<"assistant.message"> & { text: string };
export type LlmCallEvent = Envelope<"llm.call"> & {
  phase: "start" | "end";
  model: string;
  attempt: number;
  duration_ms?: number;
  ok?: boolean;
  error_kind?: string | null;
};
export type LlmRetryEvent = Envelope<"llm.retry"> & { reason: string; delay: number };
export type LlmErrorEvent = Envelope<"llm.error"> & { kind: string; message: string; retryable: boolean };
export type ToolStatus =
  | "requested" | "awaiting_confirmation" | "running" | "success" | "error" | "denied" | "partial" | "unverified";
export type ToolExecutionEvent = Envelope<"tool.execution"> & {
  tool: string;
  status: ToolStatus;
  permission?: string;
  target?: string;
  duration_ms?: number;
};
export type MemoryActivityEvent = Envelope<"memory.activity"> & { op: "retrieve" | "search" | "store"; count?: number };
export type TaskUpdateEvent = Envelope<"task.update"> & { task: TaskView | null };
export type TaskProgressEvent = Envelope<"task.progress"> & { message: string };
export type VoiceOutputEvent = Envelope<"voice.output"> & {
  phase: "start" | "chunk" | "end" | "error";
  index?: number;
  duration_ms?: number;
  words?: [number, number][];
  timing?: "exact" | "none";
};
export type VoiceInputPhase =
  | "armed" | "wake_detected" | "collecting" | "transcribing" | "recording" | "idle" | "stopped";
export type VoiceInputEvent = Envelope<"voice.input"> & { mode: "off" | "ptt" | "wake"; phase: VoiceInputPhase; message?: string };
export type VoiceLevelEvent = Envelope<"voice.level"> & { rms: number; phase: "armed" | "collecting" };
export type ConfirmRequestEvent = Envelope<"confirm.request"> & ConfirmPayload;
export type ConfirmResolvedEvent = Envelope<"confirm.resolved"> & {
  id: string;
  approved: boolean;
  channel: "terminal" | "browser" | "timeout" | "cancelled";
};
export type CommandAckEvent = Envelope<"command.ack"> & { command_id: string; status: "accepted" | "queued" };
export type CommandRejectedEvent = Envelope<"command.rejected"> & {
  command_id: string;
  reason: "busy" | "queue_full" | "terminal_only" | "invalid" | "too_long";
};
export type CommandOutputEvent = Envelope<"command.output"> & { command_id: string; command: string; text: string };
export type SystemMetricsEvent = Envelope<"system.metrics"> & {
  cpu_percent: number;
  memory_percent: number;
  memory_total: number;
  memory_available: number;
  memory_used: number;
  disk_percent: number;
  disk_total: number;
  disk_used: number;
  uptime_s: number;
  process_uptime_s: number;
  load_avg: number[] | null;
  os: string;
};
export type NetworkStatusEvent = Envelope<"network.status"> & {
  groq: "up" | "down" | "unknown";
  checked_at: number;
  source: "probe" | "passive";
  latency_ms?: number | null;
};
export type NoticeEvent = Envelope<"notice"> & { level: "info" | "warn" | "error"; text: string };

/** One member per backend EVENT_TYPES key. */
export type ServerEvent =
  | SessionStartEvent
  | SessionEndEvent
  | JarvisStateEvent
  | RequestStartEvent
  | RequestEndEvent
  | AssistantMessageEvent
  | LlmCallEvent
  | LlmRetryEvent
  | LlmErrorEvent
  | ToolExecutionEvent
  | MemoryActivityEvent
  | TaskUpdateEvent
  | TaskProgressEvent
  | VoiceOutputEvent
  | VoiceInputEvent
  | VoiceLevelEvent
  | ConfirmRequestEvent
  | ConfirmResolvedEvent
  | CommandAckEvent
  | CommandRejectedEvent
  | CommandOutputEvent
  | SystemMetricsEvent
  | NetworkStatusEvent
  | NoticeEvent;

export type ServerEventType = ServerEvent["type"];
export type EventOf<T extends ServerEventType> = Extract<ServerEvent, { type: T }>;

// --- subsystems (the six 3D nodes) -----------------------------------------------------------------

export type SubsystemId = "llm" | "memory" | "tools" | "system" | "voice" | "network";
export type SubsystemStatus = "online" | "degraded" | "offline" | "unknown" | "unavailable";

/** One node's block from the backend StateTracker: a status plus whatever facts that subsystem has. */
export type SubsystemView = { status: SubsystemStatus } & Record<string, unknown>;
export type Subsystems = Record<SubsystemId, SubsystemView>;

// --- server-only control frames (not bus events, no envelope) --------------------------------------

export interface AuthOk {
  type: "auth.ok";
  protocol: 1;
}

export interface Pong {
  type: "pong";
}

export interface ServerError {
  type: "error";
  code: string;
}

export type ConfirmAnswerOutcome = "ok" | "stale" | "already_answered" | "digest_mismatch" | "terminal_only";

/** The server's reply to this page's confirm.answer (a control frame, sent only to the tab that answered). */
export interface ConfirmAnswerResult {
  type: "confirm.answer.result";
  id: string;
  result: ConfirmAnswerOutcome;
}

/**
 * Everything needed to draw the picture from nothing. `recent_events` may run ahead of the other fields
 * (events still in flight to the backend tracker), so they are folded on top in seq order.
 * Snapshot fields arrive from the backend as-is; the reducer re-checks the nested events with the guards.
 */
export interface Snapshot {
  type: "snapshot";
  seq: number;
  session: SessionStartEvent | null;
  state: { state: BackendState; detail: string | null; since: number };
  subsystems: Record<string, unknown>;
  task: TaskView | null;
  pending_confirmation: ConfirmPayload | null;
  metrics: SystemMetricsEvent | null;
  network: NetworkStatusEvent | null;
  recent_events: unknown[];
}

export type ControlFrame = AuthOk | Pong | ServerError | ConfirmAnswerResult | Snapshot;
export type ServerFrame = ServerEvent | ControlFrame;

// --- browser -> server -----------------------------------------------------------------------------

export interface AuthMessage {
  type: "auth";
  token: string;
  /** The last event seq this page saw; null on a fresh page, which gets a snapshot instead of a replay. */
  last_seq: number | null;
}

export interface CommandSubmitMessage {
  type: "command.submit";
  id: string;
  text: string;
}

export interface RequestCancelMessage {
  type: "request.cancel";
}

export interface ConfirmAnswerMessage {
  type: "confirm.answer";
  id: string;
  digest: string;
  approved: boolean;
}

export interface PingMessage {
  type: "ping";
}

export type ClientMessage = AuthMessage | CommandSubmitMessage | RequestCancelMessage | ConfirmAnswerMessage | PingMessage;

// --- the browser's own view of the connection ------------------------------------------------------

/**
 * connecting: the first attempt; open: authenticated; reconnecting: dropped, retrying;
 * offline: JARVIS said it is shutting down (still retrying); auth_failed: the token was refused (stale tab);
 * too_many_clients: the server's tab limit; no_token: the page was opened without the terminal's link.
 */
export type ConnectionStatus =
  | "connecting" | "open" | "reconnecting" | "offline" | "auth_failed" | "too_many_clients" | "no_token";
