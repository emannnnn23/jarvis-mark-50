import { describe, expect, it } from "vitest";

import { PACKET_LIMIT, packetsFor } from "../components/nodes/packets";
import {
  STREAM_CATEGORIES,
  clearStream,
  describeEvent,
  formatHms,
  initialStreamView,
  pauseStream,
  resumeStream,
  streamRows,
} from "../store/eventStream";
import type { ServerEvent } from "../types/events";

let nextTs = 1762500000000;

function ev(type: ServerEvent["type"], seq: number, payload: Record<string, unknown> = {}): ServerEvent {
  nextTs += 1;
  return { v: 1, seq, type, timestamp: nextTs, ...payload } as unknown as ServerEvent;
}

const notice = (seq: number, text = `notice ${seq}`) => ev("notice", seq, { level: "info", text });

/** One event of every type the backend sends, as a request would produce them. */
const EVERY_TYPE: ServerEvent[] = [
  ev("session.start", 1, {
    version: "0.5.0", provider: "groq", model: "llama-3.3-70b-versatile", permission_mode: "ask",
    voice_available: true, voice_enabled: true, mic_available: true, wake_available: true,
    memory_status: "ok", tasks_status: "ok", developer_mode: false,
  }),
  ev("jarvis.state", 2, { state: "processing", since: 1, detail: null }),
  ev("request.start", 3, { source: "voice", text: "what time is it" }),
  ev("llm.call", 4, { phase: "start", model: "llama-3.3-70b-versatile", attempt: 1 }),
  ev("llm.call", 5, { phase: "end", model: "llama-3.3-70b-versatile", attempt: 1, ok: true, duration_ms: 1240 }),
  ev("llm.retry", 6, { reason: "rate_limit", delay: 2 }),
  ev("llm.error", 7, { kind: "network", message: "Groq unreachable", retryable: false }),
  ev("tool.execution", 8, { tool: "read_file", status: "running", target: "notes.txt" }),
  ev("memory.activity", 9, { op: "store", count: 42 }),
  ev("task.update", 10, { task: { id: "t", goal: "Tidy notes", status: "active", steps: [], updated_at: 1, untrusted_plan: false } }),
  ev("task.progress", 11, { message: "Step 2 of 3" }),
  ev("voice.output", 12, { phase: "end", duration_ms: 2100 }),
  ev("voice.input", 13, { mode: "wake", phase: "collecting" }),
  ev("confirm.request", 14, {
    id: "c", digest: "d", tool: "write_file", label: "Write notes.txt", operation: "write", affected_paths: [],
    reason: "", hidden: 0, lines: [], omitted: 0, browser_approvable: true, expires_at: null,
  }),
  ev("confirm.resolved", 15, { id: "c", approved: true, channel: "browser" }),
  ev("command.ack", 16, { command_id: "x", status: "accepted" }),
  ev("command.rejected", 17, { command_id: "x", reason: "busy" }),
  ev("command.output", 18, { command_id: "x", command: "/help", text: "..." }),
  ev("assistant.message", 19, { text: "It is 18:25." }),
  ev("network.status", 20, { groq: "up", checked_at: 1, source: "probe", latency_ms: 42 }),
  ev("notice", 21, { level: "warn", text: "Developer mode is on" }),
  ev("request.end", 22, { status: "complete", duration_ms: 4100 }),
  ev("session.end", 23),
];

describe("describeEvent", () => {
  it("gives every streamed event a category chip, a short status line and text details", () => {
    for (const event of EVERY_TYPE) {
      const row = describeEvent(event);
      expect(row, event.type).not.toBeNull();
      expect(STREAM_CATEGORIES, event.type).toContain(row!.category);
      expect(row!.status.length, event.type).toBeGreaterThan(0);
      expect(row!.status.length, event.type).toBeLessThanOrEqual(120);
      expect(typeof row!.details, event.type).toBe("string");
    }
  });

  it("uses exactly the nine categories", () => {
    expect(STREAM_CATEGORIES).toEqual(["session", "request", "llm", "tool", "memory", "task", "voice", "network", "notice"]);
  });

  it("maps types to categories", () => {
    const category = (event: ServerEvent) => describeEvent(event)?.category;
    expect(category(EVERY_TYPE[0]!)).toBe("session");
    expect(category(EVERY_TYPE[2]!)).toBe("request");
    expect(category(EVERY_TYPE[3]!)).toBe("llm");
    expect(category(EVERY_TYPE[7]!)).toBe("tool");
    expect(category(EVERY_TYPE[8]!)).toBe("memory");
    expect(category(EVERY_TYPE[9]!)).toBe("task");
    expect(category(EVERY_TYPE[11]!)).toBe("voice");
    expect(category(EVERY_TYPE[19]!)).toBe("network");
    expect(category(EVERY_TYPE[20]!)).toBe("notice");
  });

  it("leaves meter-like events (voice levels, system metrics) out of the stream", () => {
    expect(describeEvent(ev("voice.level", 30, { rms: 0.4, phase: "armed" }))).toBeNull();
    expect(describeEvent(ev("system.metrics", 31, { cpu_percent: 3 }))).toBeNull();
  });

  it("writes human status lines", () => {
    const status = (event: ServerEvent) => describeEvent(event)!.status;
    expect(status(EVERY_TYPE[4]!)).toBe("Model answered in 1.2 s");
    expect(status(EVERY_TYPE[7]!)).toBe("Running read_file: notes.txt");
    expect(status(ev("tool.execution", 40, { tool: "read_file", status: "success", duration_ms: 42 }))).toBe("read_file succeeded in 42 ms");
    expect(status(ev("memory.activity", 41, { op: "retrieve", count: 3 }))).toBe("Recalled 3");
    expect(status(EVERY_TYPE[8]!)).toBe("Saved to memory: 42 facts");
    expect(status(EVERY_TYPE[21]!)).toBe("Request complete in 4.1 s");
    expect(status(ev("request.start", 42, { source: "terminal", text: "x".repeat(300) })).length).toBeLessThanOrEqual(120);
  });

  it("flags what needs attention", () => {
    expect(describeEvent(EVERY_TYPE[6]!)!.tone).toBe("attention"); // llm.error
    expect(describeEvent(ev("tool.execution", 50, { tool: "rm", status: "error" }))!.tone).toBe("attention");
    expect(describeEvent(ev("tool.execution", 51, { tool: "rm", status: "awaiting_confirmation" }))!.tone).toBe("attention");
    expect(describeEvent(EVERY_TYPE[7]!)!.tone).toBe("normal");
  });

  it("renders details as plain text, markup included verbatim", () => {
    const row = describeEvent(ev("assistant.message", 60, { text: "<img src=x onerror=alert(1)>", request_id: "r-1" }))!;
    expect(row.details).toContain("<img src=x onerror=alert(1)>");
    expect(row.details).toContain("r-1");
    expect(row.details).not.toContain("\"v\"");
  });
});

describe("formatHms", () => {
  it("is HH:MM:SS in local time, zero-padded, 24-hour", () => {
    const at = new Date(2026, 10, 7, 9, 5, 3).getTime();
    expect(formatHms(at)).toBe("09:05:03");
    expect(formatHms(new Date(2026, 10, 7, 23, 59, 59).getTime())).toBe("23:59:59");
  });
});

describe("event stream view", () => {
  const log = [notice(1), ev("voice.level", 2, { rms: 0.1, phase: "armed" }), notice(3), notice(4)];

  it("shows streamable events, oldest first", () => {
    const { rows, buffered } = streamRows(log, initialStreamView());
    expect(rows.map((row) => row.event.seq)).toEqual([1, 3, 4]);
    expect(buffered).toBe(0);
  });

  it("freezes while paused and counts what arrives meanwhile", () => {
    const paused = pauseStream(initialStreamView(), log);
    const later = [...log, notice(5), notice(6)];
    const { rows, buffered } = streamRows(later, paused);
    expect(rows.map((row) => row.event.seq)).toEqual([1, 3, 4]);
    expect(buffered).toBe(2);
    // Older entries falling out of the 500-entry ring do not vanish from a paused view.
    expect(streamRows([notice(6)], paused).rows.map((row) => row.event.seq)).toEqual([1, 3, 4]);
    const resumed = resumeStream(paused);
    expect(streamRows(later, resumed)).toMatchObject({ buffered: 0 });
    expect(streamRows(later, resumed).rows.map((row) => row.event.seq)).toEqual([1, 3, 4, 5, 6]);
  });

  it("clear empties the list, including what was buffered, and later events still show", () => {
    const cleared = clearStream(initialStreamView(), log);
    expect(streamRows(log, cleared).rows).toEqual([]);
    expect(streamRows([...log, notice(7)], cleared).rows.map((row) => row.event.seq)).toEqual([7]);

    const paused = pauseStream(initialStreamView(), log);
    const withMore = [...log, notice(5)];
    const pausedCleared = clearStream(paused, withMore);
    expect(streamRows(withMore, pausedCleared)).toMatchObject({ rows: [], buffered: 0 });
    expect(streamRows([...withMore, notice(8)], pausedCleared).buffered).toBe(1);
  });

  it("filters by category", () => {
    const mixed = [notice(1), ev("llm.call", 2, { phase: "start", model: "m", attempt: 1 }), notice(3)];
    const view = { ...initialStreamView(), filter: "llm" as const };
    expect(streamRows(mixed, view).rows.map((row) => row.event.seq)).toEqual([2]);
  });

  it("never holds more than the 500 entries the store keeps", () => {
    const many = Array.from({ length: 700 }, (_, index) => notice(index + 1));
    expect(streamRows(many, initialStreamView()).rows).toHaveLength(500);
  });
});

describe("packetsFor", () => {
  it("sends data packets for tool, llm, memory and voice events only", () => {
    expect(packetsFor(EVERY_TYPE[3]!)).toEqual([{ node: "llm", direction: "out", tone: "normal" }]);
    expect(packetsFor(EVERY_TYPE[4]!)).toEqual([{ node: "llm", direction: "in", tone: "normal" }]);
    expect(packetsFor(EVERY_TYPE[7]!)).toEqual([{ node: "tools", direction: "out", tone: "normal" }]);
    expect(packetsFor(ev("tool.execution", 70, { tool: "x", status: "error" }))).toEqual([
      { node: "tools", direction: "in", tone: "attention" },
    ]);
    expect(packetsFor(EVERY_TYPE[8]!)).toEqual([{ node: "memory", direction: "out", tone: "normal" }]);
    expect(packetsFor(ev("memory.activity", 71, { op: "retrieve", count: 1 }))).toEqual([
      { node: "memory", direction: "in", tone: "normal" },
    ]);
    expect(packetsFor(EVERY_TYPE[11]!)).toEqual([{ node: "voice", direction: "out", tone: "normal" }]);
    expect(packetsFor(EVERY_TYPE[12]!)).toEqual([{ node: "voice", direction: "in", tone: "normal" }]);
    expect(packetsFor(EVERY_TYPE[0]!)).toEqual([]);
    expect(packetsFor(EVERY_TYPE[20]!)).toEqual([]);
    expect(packetsFor(ev("voice.level", 72, { rms: 0.4, phase: "armed" }))).toEqual([]);
    expect(PACKET_LIMIT).toBe(32);
  });
});
