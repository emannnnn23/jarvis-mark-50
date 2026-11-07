/**
 * Runtime checks for every frame the server sends, written by hand against mission_control/protocol.py.
 *
 * Nothing from the socket is trusted until it passes here: a frame that fails is dropped, never half-used.
 * The checks follow the backend's own validate_event (required fields, exact enumerations, text limits,
 * shapes) plus the primitive type of each field the UI reads, so a consumer can rely on its TypeScript type.
 * Unknown event types are rejected; unknown extra fields are allowed, so the backend can add optional
 * fields without breaking an older page. The shared fixtures file is checked against these guards in vitest.
 */

import { PROTOCOL_VERSION } from "./events";
import type {
  AuthOk,
  ConfirmAnswerResult,
  ConfirmPayload,
  ControlFrame,
  Pong,
  ServerError,
  ServerEvent,
  ServerEventType,
  ServerFrame,
  Snapshot,
  SubsystemStatus,
  TaskView,
} from "./events";

type Check = (value: unknown) => boolean;
type Fields = Record<string, Check>;

// --- primitive checks ------------------------------------------------------------------------------

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const str: Check = (value) => typeof value === "string";
const num: Check = (value) => typeof value === "number" && Number.isFinite(value);
const int: Check = (value) => Number.isInteger(value);
const bool: Check = (value) => typeof value === "boolean";
const nullable = (check: Check): Check => (value) => value === null || check(value);
const arrayOf = (check: Check): Check => (value) => Array.isArray(value) && value.every(check);
const oneOf = (...allowed: string[]): Check => (value) => typeof value === "string" && allowed.includes(value);
// The backend cuts text in code points (Python str), so count them the same way: an emoji is one, not two.
const maxText = (limit: number): Check => (value) => typeof value === "string" && [...value].length <= limit;
const shape = (fields: Fields, optional: Fields = {}): Check => (value) =>
  isObject(value) && checkFields(value, fields, optional);

function checkFields(value: Record<string, unknown>, required: Fields, optional: Fields): boolean {
  for (const [field, check] of Object.entries(required)) {
    if (!(field in value) || !check(value[field])) return false;
  }
  for (const [field, check] of Object.entries(optional)) {
    if (field in value && value[field] !== undefined && !check(value[field])) return false;
  }
  return true;
}

// --- the contract ----------------------------------------------------------------------------------

/** Exact enumerations, keyed "type:field" (protocol.py ENUMS). Optional fields are listed too. */
export const EVENT_ENUMS = {
  "session.start:provider": ["groq"],
  "session.start:memory_status": ["ok", "corrupted", "disabled"],
  "session.start:tasks_status": ["ok", "corrupted", "disabled"],
  "jarvis.state:state": ["idle", "listening", "processing", "speaking", "tool", "error"],
  "request.start:source": ["terminal", "voice", "browser"],
  "request.end:status": ["complete", "incomplete", "cancelled", "error"],
  "llm.call:phase": ["start", "end"],
  "tool.execution:status": [
    "requested", "awaiting_confirmation", "running", "success", "error", "denied", "partial", "unverified",
  ],
  "memory.activity:op": ["retrieve", "search", "store"],
  "voice.output:phase": ["start", "chunk", "end", "error"],
  "voice.output:timing": ["exact", "none"],
  "voice.input:mode": ["off", "ptt", "wake"],
  "voice.input:phase": ["armed", "wake_detected", "collecting", "transcribing", "recording", "idle", "stopped"],
  "voice.level:phase": ["armed", "collecting"],
  "confirm.resolved:channel": ["terminal", "browser", "timeout", "cancelled"],
  "command.ack:status": ["accepted", "queued"],
  "command.rejected:reason": ["busy", "queue_full", "terminal_only", "invalid", "too_long"],
  "network.status:groq": ["up", "down", "unknown"],
  "network.status:source": ["probe", "passive"],
  "notice:level": ["info", "warn", "error"],
} as const satisfies Record<string, readonly string[]>;

/** Longest text the protocol allows in these fields (protocol.py MAX_TEXT). */
export const MAX_TEXT = {
  "request.start:text": 500,
  "assistant.message:text": 4000,
  "command.output:text": 8000,
} as const satisfies Record<string, number>;

const e = (key: keyof typeof EVENT_ENUMS): Check => oneOf(...EVENT_ENUMS[key]);
const text = (key: keyof typeof MAX_TEXT): Check => maxText(MAX_TEXT[key]);

const taskStep = shape({ n: num, text: str, status: str });
const taskView = shape(
  { id: str, goal: str, status: str, steps: arrayOf(taskStep), updated_at: (v) => num(v) || str(v), untrusted_plan: bool },
  { paused_reason: nullable(str) },
);
const reviewLine = shape({ k: str, t: str });
const wordPair: Check = (value) => Array.isArray(value) && value.length === 2 && value.every(num);

const confirmFields: Fields = {
  id: str, digest: str, tool: str, label: str, operation: str, affected_paths: arrayOf(str), reason: str,
  hidden: int, lines: arrayOf(reviewLine), omitted: int, browser_approvable: bool, expires_at: nullable(num),
};

interface Spec {
  required: Fields;
  optional?: Fields;
}

/** Required and optional payload fields per event type (protocol.py EVENT_TYPES + event-fields.md). */
const SPECS: Record<ServerEventType, Spec> = {
  "session.start": {
    required: {
      version: str, provider: e("session.start:provider"), model: str, permission_mode: str,
      voice_available: bool, voice_enabled: bool, mic_available: bool, wake_available: bool,
      memory_status: e("session.start:memory_status"), tasks_status: e("session.start:tasks_status"),
      developer_mode: bool,
    },
  },
  "session.end": { required: {} },
  "jarvis.state": { required: { state: e("jarvis.state:state"), since: num }, optional: { detail: nullable(str) } },
  "request.start": {
    required: { source: e("request.start:source"), text: text("request.start:text") },
    optional: { command_id: str },
  },
  "request.end": {
    required: { status: e("request.end:status"), duration_ms: num },
    optional: { rounds: int, error_kind: nullable(str) },
  },
  "assistant.message": { required: { text: text("assistant.message:text") } },
  "llm.call": {
    required: { phase: e("llm.call:phase"), model: str, attempt: int },
    optional: { duration_ms: num, ok: bool, error_kind: nullable(str) },
  },
  "llm.retry": { required: { reason: str, delay: num } },
  "llm.error": { required: { kind: str, message: str, retryable: bool } },
  "tool.execution": {
    required: { tool: str, status: e("tool.execution:status") },
    optional: { permission: str, target: str, duration_ms: num },
  },
  "memory.activity": { required: { op: e("memory.activity:op") }, optional: { count: int } },
  "task.update": { required: { task: nullable(taskView) } },
  "task.progress": { required: { message: str } },
  "voice.output": {
    required: { phase: e("voice.output:phase") },
    optional: { index: int, duration_ms: num, words: arrayOf(wordPair), timing: e("voice.output:timing") },
  },
  "voice.input": {
    required: { mode: e("voice.input:mode"), phase: e("voice.input:phase") },
    optional: { message: str },
  },
  "voice.level": {
    required: { rms: (v) => num(v) && (v as number) >= 0 && (v as number) <= 1, phase: e("voice.level:phase") },
  },
  "confirm.request": { required: confirmFields },
  "confirm.resolved": { required: { id: str, approved: bool, channel: e("confirm.resolved:channel") } },
  "command.ack": { required: { command_id: str, status: e("command.ack:status") } },
  "command.rejected": { required: { command_id: str, reason: e("command.rejected:reason") } },
  "command.output": { required: { command_id: str, command: str, text: text("command.output:text") } },
  "system.metrics": {
    required: {
      cpu_percent: num, memory_percent: num, memory_total: num, memory_available: num, memory_used: num,
      disk_percent: num, disk_total: num, disk_used: num, uptime_s: num, process_uptime_s: num,
      load_avg: nullable(arrayOf(num)), os: str,
    },
  },
  "network.status": {
    required: { groq: e("network.status:groq"), checked_at: num, source: e("network.status:source") },
    optional: { latency_ms: nullable(num) },
  },
  notice: { required: { level: e("notice:level"), text: str } },
};

export const EVENT_TYPE_NAMES = Object.keys(SPECS) as ServerEventType[];

export const REQUIRED_FIELDS = Object.fromEntries(
  Object.entries(SPECS).map(([type, spec]) => [type, Object.keys(spec.required)]),
) as Record<ServerEventType, string[]>;

// --- guards ----------------------------------------------------------------------------------------

function validEnvelope(value: Record<string, unknown>): boolean {
  if (value.v !== PROTOCOL_VERSION) return false;
  if (!int(value.seq) || (value.seq as number) < 0) return false;
  if (typeof value.type !== "string") return false;
  if (!int(value.timestamp) || (value.timestamp as number) < 0) return false;
  if ("request_id" in value && typeof value.request_id !== "string") return false;
  return true;
}

/** True only for a complete, well-formed bus event of a known type. */
export function isServerEvent(value: unknown): value is ServerEvent {
  if (!isObject(value) || !validEnvelope(value)) return false;
  const spec = Object.hasOwn(SPECS, value.type as string) ? SPECS[value.type as ServerEventType] : undefined;
  return spec !== undefined && checkFields(value, spec.required, spec.optional ?? {});
}

export const isTaskView = (value: unknown): value is TaskView => taskView(value);

export function isConfirmPayload(value: unknown): value is ConfirmPayload {
  return isObject(value) && checkFields(value, confirmFields, {});
}

export function isAuthOk(value: unknown): value is AuthOk {
  return isObject(value) && value.type === "auth.ok" && value.protocol === PROTOCOL_VERSION;
}

export function isPong(value: unknown): value is Pong {
  return isObject(value) && value.type === "pong";
}

export function isServerError(value: unknown): value is ServerError {
  return isObject(value) && value.type === "error" && typeof value.code === "string";
}

export function isConfirmAnswerResult(value: unknown): value is ConfirmAnswerResult {
  return isObject(value) && value.type === "confirm.answer.result" && str(value.id) && str(value.result);
}

const BACKEND_STATES = EVENT_ENUMS["jarvis.state:state"];
const SNAPSHOT_KEYS = ["session", "task", "pending_confirmation", "metrics", "network"] as const;

/**
 * The frame's skeleton only. Its nested parts (session, metrics, network, task, pending confirmation and
 * each recent event) are re-checked one by one by the reducer, so one odd event cannot void a snapshot.
 */
export function isSnapshot(value: unknown): value is Snapshot {
  if (!isObject(value) || value.type !== "snapshot") return false;
  if (!int(value.seq) || (value.seq as number) < 0) return false;
  const state = value.state;
  if (!isObject(state) || !oneOf(...BACKEND_STATES)(state.state) || !num(state.since)) return false;
  if ("detail" in state && !nullable(str)(state.detail)) return false;
  if (!isObject(value.subsystems) || !Array.isArray(value.recent_events)) return false;
  return SNAPSHOT_KEYS.every((key) => key in value);
}

const CONTROL_GUARDS: Record<ControlFrame["type"], (value: unknown) => boolean> = {
  "auth.ok": isAuthOk,
  pong: isPong,
  error: isServerError,
  "confirm.answer.result": isConfirmAnswerResult,
  snapshot: isSnapshot,
};

/** Any frame the server may send: a control frame or a bus event. */
export function isServerFrame(value: unknown): value is ServerFrame {
  if (!isObject(value) || typeof value.type !== "string") return false;
  const control = Object.hasOwn(CONTROL_GUARDS, value.type)
    ? CONTROL_GUARDS[value.type as ControlFrame["type"]]
    : undefined;
  return control !== undefined ? control(value) : isServerEvent(value);
}

const SUBSYSTEM_STATUSES: readonly SubsystemStatus[] = ["online", "degraded", "offline", "unknown", "unavailable"];

export const isSubsystemStatus = (value: unknown): value is SubsystemStatus =>
  typeof value === "string" && (SUBSYSTEM_STATUSES as readonly string[]).includes(value);
