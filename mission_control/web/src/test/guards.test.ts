import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  EVENT_ENUMS,
  EVENT_TYPE_NAMES,
  MAX_TEXT,
  REQUIRED_FIELDS,
  isAuthOk,
  isConfirmAnswerResult,
  isPong,
  isServerError,
  isServerEvent,
  isServerFrame,
  isSnapshot,
} from "../types/guards";

// The contract shared with the backend: protocol.py's tests validate the same file.
const FIXTURES_PATH = fileURLToPath(new URL("../../../protocol_fixtures.json", import.meta.url));
const fixtures: Record<string, unknown>[] = JSON.parse(readFileSync(FIXTURES_PATH, "utf-8")).events;

const clone = <T>(value: T): T => structuredClone(value);

describe("isServerEvent against the shared fixtures", () => {
  it("accepts every fixture", () => {
    for (const event of fixtures) {
      expect(isServerEvent(event), String(event.type)).toBe(true);
    }
  });

  it("has one fixture per event type and one guard per fixture type", () => {
    const types = fixtures.map((event) => event.type).sort();
    expect(types).toEqual([...EVENT_TYPE_NAMES].sort());
  });

  it("rejects each fixture with any required field removed", () => {
    for (const event of fixtures) {
      for (const field of REQUIRED_FIELDS[event.type as keyof typeof REQUIRED_FIELDS]) {
        const mutated = clone(event);
        delete mutated[field];
        expect(isServerEvent(mutated), `${String(event.type)} without ${field}`).toBe(false);
      }
    }
  });

  it("rejects each fixture with a broken envelope", () => {
    for (const event of fixtures) {
      for (const [field, value] of [
        ["v", 2], ["v", "1"], ["seq", -1], ["seq", 1.5], ["seq", "7"],
        ["timestamp", "now"], ["type", "bogus.event"], ["request_id", 42],
      ] as const) {
        const mutated: Record<string, unknown> = { ...clone(event), [field]: value };
        expect(isServerEvent(mutated), `${String(event.type)} with ${field}=${String(value)}`).toBe(false);
      }
      for (const field of ["v", "seq", "type", "timestamp"]) {
        const mutated = clone(event);
        delete mutated[field];
        expect(isServerEvent(mutated), `${String(event.type)} without ${field}`).toBe(false);
      }
    }
  });

  it("rejects an enumerated field outside its allowed values", () => {
    let checked = 0;
    for (const event of fixtures) {
      for (const [key] of Object.entries(EVENT_ENUMS)) {
        const [type, field] = key.split(":");
        if (type !== event.type || !field || !(field in event)) continue;
        expect(isServerEvent({ ...clone(event), [field]: "not-a-real-value" }), key).toBe(false);
        expect(isServerEvent({ ...clone(event), [field]: 3 }), key).toBe(false);
        checked += 1;
      }
    }
    expect(checked).toBeGreaterThan(15);
  });

  it("rejects text longer than the protocol allows", () => {
    for (const [key, limit] of Object.entries(MAX_TEXT)) {
      const [type, field] = key.split(":");
      const event = fixtures.find((candidate) => candidate.type === type);
      expect(event, key).toBeDefined();
      expect(isServerEvent({ ...clone(event!), [field!]: "x".repeat(limit) }), key).toBe(true);
      expect(isServerEvent({ ...clone(event!), [field!]: "x".repeat(limit + 1) }), key).toBe(false);
    }
  });

  it("rejects wrong shapes the UI would trip over", () => {
    const byType = Object.fromEntries(fixtures.map((event) => [event.type, event]));
    const bad: [string, string, unknown][] = [
      ["session.start", "voice_available", "yes"],
      ["llm.call", "ok", 1],
      ["llm.error", "retryable", "false"],
      ["confirm.resolved", "approved", null],
      ["voice.level", "rms", 1.5],
      ["voice.level", "rms", "0.2"],
      ["task.update", "task", "t-1"],
      ["task.update", "task", { id: "t-1" }],
      ["voice.output", "words", [[0, 1, 2]]],
      ["confirm.request", "lines", [{ k: "add" }]],
      ["confirm.request", "affected_paths", "notes/todo.txt"],
      ["confirm.request", "hidden", 1.5],
      ["system.metrics", "cpu_percent", "23%"],
      ["system.metrics", "load_avg", "0.4"],
      ["request.end", "duration_ms", "4210"],
      ["jarvis.state", "since", "now"],
      ["jarvis.state", "detail", 7],
      ["tool.execution", "tool", 12],
    ];
    for (const [type, field, value] of bad) {
      expect(isServerEvent({ ...clone(byType[type]!), [field]: value }), `${type}.${field}`).toBe(false);
    }
  });

  it("accepts the nulls the backend really sends", () => {
    const state = fixtures.find((event) => event.type === "jarvis.state")!;
    expect(isServerEvent({ ...clone(state), detail: null })).toBe(true);
    const task = fixtures.find((event) => event.type === "task.update")!;
    expect(isServerEvent({ ...clone(task), task: null })).toBe(true);
  });

  it("rejects things that are not events at all", () => {
    for (const value of [null, undefined, 1, "session.start", [], [fixtures[0]]]) {
      expect(isServerEvent(value)).toBe(false);
    }
  });
});

describe("server-only control frames", () => {
  const snapshot = () => ({
    type: "snapshot",
    seq: 40,
    session: clone(fixtures.find((event) => event.type === "session.start")),
    state: { state: "idle", detail: null, since: 1762500000000 },
    subsystems: {
      llm: { status: "online", model: "llama-3.3-70b-versatile" },
      memory: { status: "unknown" },
      tools: { status: "unknown", active: [], recent: [] },
      system: { status: "unavailable", reason: "psutil not installed" },
      voice: { status: "online" },
      network: { status: "unknown" },
    },
    task: null,
    pending_confirmation: null,
    metrics: clone(fixtures.find((event) => event.type === "system.metrics")),
    network: null,
    recent_events: clone(fixtures.slice(0, 5)),
  });

  it("accepts auth.ok, pong, error and confirm.answer.result", () => {
    expect(isAuthOk({ type: "auth.ok", protocol: 1 })).toBe(true);
    expect(isPong({ type: "pong" })).toBe(true);
    expect(isServerError({ type: "error", code: "invalid" })).toBe(true);
    expect(isConfirmAnswerResult({ type: "confirm.answer.result", id: "b7e2", result: "approved" })).toBe(true);
  });

  it("rejects mutated control frames", () => {
    expect(isAuthOk({ type: "auth.ok", protocol: 2 })).toBe(false);
    expect(isAuthOk({ type: "auth.ok" })).toBe(false);
    expect(isPong({ type: "ping" })).toBe(false);
    expect(isServerError({ type: "error" })).toBe(false);
    expect(isServerError({ type: "error", code: 4 })).toBe(false);
    expect(isConfirmAnswerResult({ type: "confirm.answer.result", id: 1, result: "x" })).toBe(false);
  });

  it("accepts a snapshot and rejects a broken one", () => {
    expect(isSnapshot(snapshot())).toBe(true);
    expect(isSnapshot({ ...snapshot(), session: null, metrics: null })).toBe(true);
    const broken: [string, unknown][] = [
      ["seq", "40"], ["seq", -1], ["state", "idle"], ["subsystems", null], ["recent_events", {}],
      ["state", { state: "dancing", detail: null, since: 1 }],
      ["state", { state: "idle", detail: null, since: "now" }],
      ["state", { state: "offline", detail: null, since: 1 }], // offline is the browser's call, never the server's
    ];
    for (const [field, value] of broken) {
      expect(isSnapshot({ ...snapshot(), [field]: value }), field).toBe(false);
    }
    const missing = snapshot() as Record<string, unknown>;
    delete missing.recent_events;
    expect(isSnapshot(missing)).toBe(false);
  });

  it("routes every valid frame through isServerFrame and nothing else", () => {
    expect(isServerFrame(snapshot())).toBe(true);
    expect(isServerFrame({ type: "auth.ok", protocol: 1 })).toBe(true);
    expect(isServerFrame({ type: "pong" })).toBe(true);
    expect(isServerFrame(fixtures[3])).toBe(true);
    expect(isServerFrame({ type: "auth", token: "x" })).toBe(false);
    expect(isServerFrame({ type: "mystery" })).toBe(false);
  });
});
