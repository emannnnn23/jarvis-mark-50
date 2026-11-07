/**
 * The event stream as people read it: one row per event with a time, a category chip, a short status
 * line in plain words, and the full payload as text for anyone who wants the detail.
 *
 * Meter-like events (voice.level, system.metrics) never become rows: they arrive several times a second
 * and would bury everything else; the panels show them as live meters instead.
 *
 * The view state is pure, so pause/resume/clear are testable: pausing freezes the rows on screen (they
 * stay even if the store's 500-entry ring moves on) and counts what arrives meanwhile; clearing hides
 * everything up to the newest seq the store holds, buffered rows included.
 */

import { LOG_LIMIT } from "./reducer";
import type { ServerEvent } from "../types/events";

export const STREAM_CATEGORIES = [
  "session", "request", "llm", "tool", "memory", "task", "voice", "network", "notice",
] as const;
export type StreamCategory = (typeof STREAM_CATEGORIES)[number];
export type StreamFilter = StreamCategory | "all";
export type RowTone = "normal" | "attention";

export interface EventRow {
  event: ServerEvent;
  category: StreamCategory;
  status: string;
  tone: RowTone;
  /** The payload as indented text, rendered verbatim (never as HTML). */
  details: string;
}

const STATUS_MAX = 120;
const QUOTE_MAX = 80;

// --- formatting ------------------------------------------------------------------------------------

const pad = (value: number) => String(value).padStart(2, "0");

/** HH:MM:SS, local time, 24-hour, whatever the browser's locale. */
export function formatHms(epochMs: number): string {
  const date = new Date(epochMs);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 42 ms, 1.2 s, 2 min 5 s. */
export function formatMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
}

function clip(text: string, max: number): string {
  const chars = [...text.replace(/\s+/g, " ").trim()];
  return chars.length <= max ? chars.join("") : `${chars.slice(0, max - 1).join("")}…`;
}

const quote = (text: string) => `“${clip(text, QUOTE_MAX)}”`;

const STATE_WORD: Record<string, string> = {
  idle: "Idle", listening: "Listening", processing: "Processing", speaking: "Speaking", tool: "Running a tool", error: "Error",
};
const SOURCE_WORD: Record<string, string> = { terminal: "Typed", voice: "Spoken", browser: "Browser" };
const VOICE_INPUT_WORD: Record<string, string> = {
  armed: "Wake word armed",
  wake_detected: "Wake word heard",
  collecting: "Listening",
  transcribing: "Transcribing",
  recording: "Recording (push to talk)",
  idle: "Microphone idle",
  stopped: "Hands-free listening stopped",
};
const CHANNEL_WORD: Record<string, string> = { terminal: "in the terminal", browser: "in the browser" };
const REJECT_WORD: Record<string, string> = {
  busy: "JARVIS is busy", queue_full: "a command is already waiting", terminal_only: "terminal only",
  invalid: "not a valid command", too_long: "too long",
};

function toolStatus(event: Extract<ServerEvent, { type: "tool.execution" }>): string {
  const target = event.target ? `: ${event.target}` : "";
  const took = typeof event.duration_ms === "number" ? ` in ${formatMs(event.duration_ms)}` : "";
  switch (event.status) {
    case "requested":
      return `${event.tool} requested${target}`;
    case "awaiting_confirmation":
      return `${event.tool} is waiting for approval${target}`;
    case "running":
      return `Running ${event.tool}${target}`;
    case "success":
      return `${event.tool} succeeded${took}`;
    case "error":
      return `${event.tool} failed${took}`;
    case "denied":
      return `${event.tool} was denied`;
    case "partial":
      return `${event.tool} partly succeeded${took}`;
    case "unverified":
      return `${event.tool} finished, unverified${took}`;
    default:
      return event.tool;
  }
}

type Described = [StreamCategory, string, RowTone?];

function describe(event: ServerEvent): Described | null {
  switch (event.type) {
    case "session.start":
      return ["session", `Session started with ${event.model}`];
    case "session.end":
      return ["session", "Session ended"];
    case "jarvis.state":
      return ["session", `State: ${STATE_WORD[event.state] ?? event.state}${event.detail ? ` (${event.detail})` : ""}`,
        event.state === "error" ? "attention" : "normal"];
    case "request.start":
      return ["request", `${SOURCE_WORD[event.source] ?? "New"} request ${quote(event.text)}`];
    case "request.end": {
      const took = formatMs(event.duration_ms);
      if (event.status === "complete") return ["request", `Request complete in ${took}`];
      if (event.status === "cancelled") return ["request", `Request cancelled after ${took}`];
      if (event.status === "incomplete") return ["request", `Request incomplete after ${took}`];
      return ["request", `Request failed${event.error_kind ? ` (${event.error_kind})` : ""} after ${took}`, "attention"];
    }
    case "assistant.message":
      return ["request", `Reply ${quote(event.text)}`];
    case "command.ack":
      return ["request", event.status === "queued" ? "Command queued" : "Command accepted"];
    case "command.rejected":
      return ["request", `Command rejected: ${REJECT_WORD[event.reason] ?? event.reason}`, "attention"];
    case "command.output":
      return ["request", `Output of ${clip(event.command, 40)}`];
    case "llm.call":
      if (event.phase === "start") return ["llm", `Calling ${event.model}${event.attempt > 1 ? ` (attempt ${event.attempt})` : ""}`];
      if (event.ok === false) {
        const took = typeof event.duration_ms === "number" ? ` after ${formatMs(event.duration_ms)}` : "";
        return ["llm", `Model call failed${event.error_kind ? `: ${event.error_kind}` : ""}${took}`, "attention"];
      }
      return ["llm", typeof event.duration_ms === "number" ? `Model answered in ${formatMs(event.duration_ms)}` : "Model answered"];
    case "llm.retry":
      return ["llm", `Retrying in ${formatMs(event.delay * 1000)}: ${event.reason}`];
    case "llm.error":
      return ["llm", `Model error (${event.kind}): ${event.message}`, "attention"];
    case "tool.execution":
      return ["tool", toolStatus(event),
        event.status === "error" || event.status === "denied" || event.status === "awaiting_confirmation" ? "attention" : "normal"];
    case "confirm.request":
      return ["tool", `Approval needed: ${event.label}`, "attention"];
    case "confirm.resolved":
      if (event.channel === "timeout") return ["tool", "Approval timed out, declined"];
      if (event.channel === "cancelled") return ["tool", "Approval cancelled, declined"];
      return ["tool", `${event.approved ? "Approved" : "Declined"} ${CHANNEL_WORD[event.channel] ?? ""}`.trim()];
    case "memory.activity": {
      const count = typeof event.count === "number" ? event.count : null;
      if (event.op === "store") return ["memory", count === null ? "Saved to memory" : `Saved to memory: ${count} facts`];
      return ["memory", count === null ? (event.op === "search" ? "Searched memory" : "Recalled memories") : `Recalled ${count}`];
    }
    case "task.update":
      return ["task", event.task === null ? "No task in progress" : `Task ${event.task.status}: ${event.task.goal}`];
    case "task.progress":
      return ["task", event.message];
    case "voice.output":
      if (event.phase === "start") return ["voice", "Speaking"];
      if (event.phase === "end") return ["voice", typeof event.duration_ms === "number" ? `Finished speaking (${formatMs(event.duration_ms)})` : "Finished speaking"];
      if (event.phase === "error") return ["voice", "Speech failed", "attention"];
      return ["voice", `Speech part ${typeof event.index === "number" ? event.index + 1 : ""}`.trim()];
    case "voice.input":
      return ["voice", event.message ? `${VOICE_INPUT_WORD[event.phase] ?? event.phase}: ${event.message}` : VOICE_INPUT_WORD[event.phase] ?? event.phase];
    case "network.status":
      if (event.groq === "up") {
        return ["network", typeof event.latency_ms === "number" ? `Groq reachable (${Math.round(event.latency_ms)} ms)` : "Groq reachable"];
      }
      return event.groq === "down" ? ["network", "Groq unreachable", "attention"] : ["network", "Groq status unknown"];
    case "notice":
      return ["notice", event.text, event.level === "info" ? "normal" : "attention"];
    case "voice.level":
    case "system.metrics":
      return null;
    default:
      return null;
  }
}

/** Everything but the envelope's bookkeeping, as indented text. */
function detailsText(event: ServerEvent): string {
  const { v: _v, seq, type: _type, timestamp: _timestamp, ...rest } = event as ServerEvent & Record<string, unknown>;
  return `seq ${seq}\n${JSON.stringify(rest, null, 2)}`;
}

/** The row for one event, or null for meter-like events that never enter the stream. */
export function describeEvent(event: ServerEvent): EventRow | null {
  const described = describe(event);
  if (described === null) return null;
  const [category, status, tone = "normal"] = described;
  return { event, category, status: clip(status, STATUS_MAX), tone, details: detailsText(event) };
}

// --- view state ------------------------------------------------------------------------------------

export interface StreamView {
  paused: boolean;
  /** The rows on screen when paused; null while live. */
  frozen: EventRow[] | null;
  /** Events at or below this seq were cleared. */
  clearedThrough: number;
  filter: StreamFilter;
}

export function initialStreamView(): StreamView {
  return { paused: false, frozen: null, clearedThrough: 0, filter: "all" };
}

// Rows are described once per event object; the log keeps the same objects, so this stays small.
const rowCache = new WeakMap<ServerEvent, EventRow | null>();

function rowOf(event: ServerEvent): EventRow | null {
  let row = rowCache.get(event);
  if (row === undefined) {
    row = describeEvent(event);
    rowCache.set(event, row);
  }
  return row;
}

function liveRows(log: readonly ServerEvent[], clearedThrough: number): EventRow[] {
  const rows: EventRow[] = [];
  for (const event of log) {
    if (event.seq <= clearedThrough) continue;
    const row = rowOf(event);
    if (row !== null) rows.push(row);
  }
  return rows.length > LOG_LIMIT ? rows.slice(rows.length - LOG_LIMIT) : rows;
}

const highestSeq = (rows: readonly EventRow[], fallback: number) =>
  rows.reduce((max, row) => Math.max(max, row.event.seq), fallback);

/** The rows to show (oldest first) and, while paused, how many arrived since. */
export function streamRows(log: readonly ServerEvent[], view: StreamView): { rows: EventRow[]; buffered: number } {
  const keep = (row: EventRow) => view.filter === "all" || row.category === view.filter;
  if (!view.paused || view.frozen === null) {
    return { rows: liveRows(log, view.clearedThrough).filter(keep), buffered: 0 };
  }
  const frozenThrough = highestSeq(view.frozen, view.clearedThrough);
  const buffered = liveRows(log, frozenThrough).filter(keep).length;
  return { rows: view.frozen.filter(keep), buffered };
}

export function pauseStream(view: StreamView, log: readonly ServerEvent[]): StreamView {
  if (view.paused) return view;
  return { ...view, paused: true, frozen: liveRows(log, view.clearedThrough) };
}

export function resumeStream(view: StreamView): StreamView {
  return { ...view, paused: false, frozen: null };
}

export function clearStream(view: StreamView, log: readonly ServerEvent[]): StreamView {
  const newest = log.reduce((max, event) => Math.max(max, event.seq), view.clearedThrough);
  return { ...view, clearedThrough: newest, frozen: view.paused ? [] : null };
}

export function filterStream(view: StreamView, filter: StreamFilter): StreamView {
  return { ...view, filter };
}
