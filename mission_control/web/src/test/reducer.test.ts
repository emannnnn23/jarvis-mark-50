import { describe, expect, it } from "vitest";

import { LOG_LIMIT, displayState, initialMissionState, reduce } from "../store/reducer";
import type { MissionInput, MissionState } from "../store/reducer";
import type { ServerEvent, Snapshot } from "../types/events";

let nextTs = 1762500000000;

function ev<T extends ServerEvent["type"]>(type: T, seq: number, payload: Record<string, unknown> = {}): ServerEvent {
  nextTs += 1;
  return { v: 1, seq, type, timestamp: nextTs, ...payload } as unknown as ServerEvent;
}

const state = (seq: number, value: string, detail: string | null = null) =>
  ev("jarvis.state", seq, { state: value, since: 1762500000000 + seq, detail });

const metrics = (seq: number, cpu: number) =>
  ev("system.metrics", seq, {
    cpu_percent: cpu, memory_percent: 40, memory_total: 8e9, memory_available: 4e9, memory_used: 4e9,
    disk_percent: 50, disk_total: 2.5e11, disk_used: 1.2e11, uptime_s: 1000, process_uptime_s: 30,
    load_avg: [0.5, 0.4, 0.3], os: "Ubuntu 24.04",
  });

const notice = (seq: number) => ev("notice", seq, { level: "info", text: `notice ${seq}` });

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    type: "snapshot",
    seq: 50,
    session: ev("session.start", 3, {
      version: "0.5.0", provider: "groq", model: "llama-3.3-70b-versatile", permission_mode: "ask",
      voice_available: true, voice_enabled: false, mic_available: true, wake_available: false,
      memory_status: "ok", tasks_status: "ok", developer_mode: false,
    }) as Snapshot["session"],
    state: { state: "processing", detail: null, since: 1762500000040 },
    subsystems: {
      llm: { status: "online", model: "llama-3.3-70b-versatile" },
      memory: { status: "online" },
      tools: { status: "unknown", active: [], recent: [] },
      system: { status: "unavailable", reason: "psutil not installed" },
      voice: { status: "online" },
      network: { status: "unknown" },
    },
    task: null,
    pending_confirmation: null,
    metrics: metrics(49, 12.5) as Snapshot["metrics"],
    network: null,
    recent_events: [],
    ...overrides,
  };
}

const run = (inputs: MissionInput[], from: MissionState = initialMissionState()) =>
  inputs.reduce<MissionState>((current, input) => reduce(current, input), from);

describe("reduce", () => {
  it("starts with no data and is not 'offline' before the first connection attempt fails", () => {
    const initial = initialMissionState();
    expect(initial.jarvis).toBeNull();
    expect(initial.connection).toBe("connecting");
    expect(displayState(initial)).toBe("idle");
  });

  it("applies the snapshot fields", () => {
    const after = reduce(initialMissionState(), snapshot());
    expect(after.jarvis?.state).toBe("processing");
    expect(after.session?.model).toBe("llama-3.3-70b-versatile");
    expect(after.metrics?.cpu_percent).toBe(12.5);
    expect(after.subsystems.system.status).toBe("unavailable");
    expect(after.subsystems.llm.status).toBe("online");
    expect(after.lastSeq).toBe(50);
  });

  it("folds recent_events in seq order on top of the snapshot, whatever order they came in", () => {
    const after = reduce(initialMissionState(), snapshot({
      recent_events: [
        ev("task.update", 47, { task: { id: "t-2", goal: "newer", status: "active", steps: [], updated_at: 2, untrusted_plan: false } }),
        ev("task.update", 44, { task: { id: "t-1", goal: "older", status: "active", steps: [], updated_at: 1, untrusted_plan: false } }),
        ev("network.status", 45, { groq: "down", checked_at: 1, source: "probe" }),
        ev("llm.call", 46, { phase: "end", model: "m", attempt: 1, ok: true, duration_ms: 900 }),
      ],
    }));
    expect(after.task?.goal).toBe("newer");
    expect(after.network?.groq).toBe("down");
    expect(after.subsystems.network.status).toBe("offline");
    expect(after.subsystems.llm.last_duration_ms).toBe(900);
    expect(after.log.map((entry) => entry.seq)).toEqual([44, 45, 46, 47]);
  });

  it("keeps the snapshot's headline state over older jarvis.state events in recent_events", () => {
    const after = reduce(initialMissionState(), snapshot({
      recent_events: [state(10, "tool", "read_file"), state(20, "idle")],
    }));
    expect(after.jarvis?.state).toBe("processing");
  });

  it("applies live events after the snapshot: state and metrics", () => {
    const after = run([
      { type: "connection", status: "open" },
      snapshot(),
      state(51, "tool", "read_file"),
      metrics(52, 77),
    ]);
    expect(after.jarvis).toMatchObject({ state: "tool", detail: "read_file" });
    expect(displayState(after)).toBe("tool");
    expect(after.metrics?.cpu_percent).toBe(77);
    expect(after.subsystems.system).toMatchObject({ status: "online", cpu_percent: 77 });
    expect(after.lastSeq).toBe(52);
  });

  it("drops duplicates and frames the snapshot already covered", () => {
    const after = run([
      snapshot(),
      state(40, "error", "late duplicate"), // seq below the snapshot
      notice(60),
      notice(60), // replay and live overlap
      state(61, "speaking"),
      state(61, "error", "same seq again"),
    ]);
    expect(after.jarvis?.state).toBe("speaking");
    expect(after.log.filter((entry) => entry.seq === 60)).toHaveLength(1);
  });

  it("does not let an out-of-order older state overwrite a newer one", () => {
    const after = run([snapshot(), state(70, "speaking"), state(65, "tool", "late")]);
    expect(after.jarvis?.state).toBe("speaking");
    expect(after.log.map((entry) => entry.seq)).toEqual([65, 70]); // the log stays in seq order
  });

  it("caps the event log at 500, dropping the oldest", () => {
    const events = Array.from({ length: 600 }, (_, index) => notice(index + 1));
    const after = run(events);
    expect(LOG_LIMIT).toBe(500);
    expect(after.log).toHaveLength(500);
    expect(after.log[0]?.seq).toBe(101);
    expect(after.log[499]?.seq).toBe(600);
  });

  it("keeps meter-like events (metrics, voice levels) out of the log", () => {
    const after = run([metrics(1, 5), ev("voice.level", 2, { rms: 0.2, phase: "armed" }), notice(3)]);
    expect(after.log.map((entry) => entry.type)).toEqual(["notice"]);
  });

  it("shows offline when the connection is lost, and the live state once it is back", () => {
    let current = run([{ type: "connection", status: "open" }, snapshot()]);
    expect(displayState(current)).toBe("processing");
    for (const status of ["reconnecting", "offline", "auth_failed", "too_many_clients", "no_token"] as const) {
      expect(displayState(reduce(current, { type: "connection", status }))).toBe("offline");
    }
    current = reduce(reduce(current, { type: "connection", status: "reconnecting" }), { type: "connection", status: "open" });
    expect(displayState(current)).toBe("processing");
  });

  it("a later snapshot (history lost on reconnect) replaces the picture but keeps seq order", () => {
    const first = run([snapshot(), notice(55)]);
    const second = reduce(first, snapshot({
      seq: 90,
      state: { state: "idle", detail: null, since: 1762500000090 },
      recent_events: [notice(55), notice(80)],
    }));
    expect(second.jarvis?.state).toBe("idle");
    expect(second.lastSeq).toBe(90);
    expect(second.log.map((entry) => entry.seq)).toEqual([55, 80]);
  });

  it("folds subsystem health like the backend tracker does", () => {
    const after = run([
      snapshot(),
      ev("llm.error", 51, { kind: "rate_limit", message: "slow down", retryable: true }),
      ev("tool.execution", 52, { tool: "read_file", status: "running" }),
      ev("memory.activity", 53, { op: "search", count: 2 }),
      ev("voice.output", 54, { phase: "error" }),
    ]);
    expect(after.subsystems.llm.status).toBe("degraded");
    expect(after.subsystems.tools).toMatchObject({ status: "online", active: ["read_file"] });
    expect(after.subsystems.memory).toMatchObject({ status: "online", last_op: "search", count: 2 });
    expect(after.subsystems.voice.status).toBe("degraded");
    const finished = reduce(after, ev("tool.execution", 55, { tool: "read_file", status: "error" }));
    expect(finished.subsystems.tools).toMatchObject({ status: "degraded", active: [] });
  });

  it("tracks a pending confirmation until it is resolved", () => {
    const request = ev("confirm.request", 51, {
      id: "c1", digest: "d", tool: "write_file", label: "Write file", operation: "write", affected_paths: [],
      reason: "r", hidden: 0, lines: [], omitted: 0, browser_approvable: true, expires_at: 1,
    });
    const pending = run([snapshot(), request]);
    expect(pending.pendingConfirmation?.id).toBe("c1");
    const other = reduce(pending, ev("confirm.resolved", 52, { id: "other", approved: false, channel: "timeout" }));
    expect(other.pendingConfirmation?.id).toBe("c1");
    const done = reduce(other, ev("confirm.resolved", 53, { id: "c1", approved: true, channel: "terminal" }));
    expect(done.pendingConfirmation).toBeNull();
  });

  it("never mutates its input", () => {
    const before = run([snapshot()]);
    const frozen = structuredClone(before);
    reduce(before, state(51, "tool", "x"));
    reduce(before, metrics(52, 99));
    expect(before).toEqual(frozen);
  });
});
