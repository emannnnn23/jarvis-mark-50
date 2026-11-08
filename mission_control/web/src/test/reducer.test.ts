import { describe, expect, it } from "vitest";

import { nodeFacts } from "../components/nodes/layout";
import { LOG_LIMIT, displayState, initialMissionState, reduce, toolEntries } from "../store/reducer";
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

  it("handles every Task 6 event in a realistic request, with details for each subsystem", () => {
    const task = {
      id: "t-1", goal: "Tidy the notes folder", status: "active", updated_at: "2026-11-07T18:00:00",
      untrusted_plan: true, paused_reason: null,
      steps: [
        { n: 1, text: "List the files", status: "done" },
        { n: 2, text: "Group by topic", status: "in_progress" },
        { n: 3, text: "Write the index", status: "pending" },
      ],
    };
    const after = run([
      { type: "connection", status: "open" },
      snapshot(),
      ev("request.start", 51, { source: "terminal", text: "tidy my notes" }),
      ev("llm.call", 52, { phase: "start", model: "llama-3.3-70b-versatile", attempt: 1 }),
      ev("llm.call", 53, { phase: "end", model: "llama-3.3-70b-versatile", attempt: 1, ok: false, duration_ms: 300, error_kind: "rate_limit" }),
      ev("llm.retry", 54, { reason: "rate_limit", delay: 2 }),
      ev("llm.call", 55, { phase: "start", model: "llama-3.3-70b-versatile", attempt: 2 }),
    ]);
    expect(after.subsystems.llm).toMatchObject({
      status: "degraded", in_flight: true, attempt: 2, last_status: "retry", last_retry_reason: "rate_limit",
    });
    const answered = run([
      ev("llm.call", 56, { phase: "end", model: "llama-3.3-70b-versatile", attempt: 2, ok: true, duration_ms: 1240 }),
      ev("tool.execution", 57, { tool: "list_files", status: "requested", target: "notes" }),
      ev("tool.execution", 58, { tool: "list_files", status: "awaiting_confirmation", permission: "ask" }),
    ], after);
    expect(answered.subsystems.llm).toMatchObject({ status: "online", in_flight: false, last_duration_ms: 1240, last_status: "success" });
    expect(answered.subsystems.tools).toMatchObject({
      active: ["list_files"], active_status: { list_files: "awaiting_confirmation" }, last_target: "notes",
    });
    const done = run([
      ev("tool.execution", 59, { tool: "list_files", status: "running", permission: "ask", target: "notes" }),
      ev("tool.execution", 60, { tool: "list_files", status: "success", permission: "ask", target: "notes", duration_ms: 42 }),
      ev("memory.activity", 61, { op: "retrieve", count: 3 }),
      ev("task.update", 62, { task }),
      ev("task.progress", 63, { message: "Step 2 of 3: grouping" }),
      ev("voice.input", 64, { mode: "wake", phase: "armed" }),
      ev("voice.level", 65, { rms: 0.31, phase: "armed" }),
      ev("voice.output", 66, { phase: "start", timing: "none" }),
      ev("voice.output", 67, { phase: "chunk", index: 0 }),
      ev("notice", 68, { level: "info", text: "Task paused" }),
      ev("llm.error", 69, { kind: "network", message: "Groq unreachable", retryable: false }),
    ], answered);
    expect(done.subsystems.tools).toMatchObject({ status: "online", active: [], active_status: {} });
    expect(toolEntries(done.subsystems.tools.recent)).toEqual([
      { tool: "list_files", status: "success", seq: 60, duration_ms: 42, target: "notes" },
    ]);
    expect(done.subsystems.memory).toMatchObject({ status: "online", recall_op: "retrieve", recall_count: 3 });
    expect(done.task?.steps).toHaveLength(3);
    expect(done.taskProgress).toMatchObject({ message: "Step 2 of 3: grouping" });
    expect(done.subsystems.voice).toMatchObject({ mode: "wake", input_phase: "armed", speaking: true });
    expect(done.voiceLevel).toMatchObject({ rms: 0.31, phase: "armed" });
    expect(done.subsystems.llm).toMatchObject({ status: "offline", last_error_kind: "network", last_error_message: "Groq unreachable" });
    expect(done.log.map((entry) => entry.type)).not.toContain("voice.level");

    const ended = run([
      ev("voice.output", 70, { phase: "end", duration_ms: 1800 }),
      ev("task.update", 71, { task: null }),
      ev("request.end", 72, { status: "complete", duration_ms: 4100, rounds: 2 }),
    ], done);
    expect(ended.subsystems.voice.speaking).toBe(false);
    expect(ended.task).toBeNull();
    expect(ended.taskProgress).toBeNull();
  });

  it("keeps recalled hits and the stored fact total apart (memory node wording)", () => {
    const recalled = run([snapshot(), ev("memory.activity", 51, { op: "search", count: 2 })]);
    expect(nodeFacts("memory", recalled.subsystems.memory, null)).toEqual(["Recalled 2"]);
    const stored = reduce(recalled, ev("memory.activity", 52, { op: "store", count: 42 }));
    expect(stored.subsystems.memory).toMatchObject({ recall_count: 2, facts: 42 });
    expect(nodeFacts("memory", stored.subsystems.memory, null)).toEqual(["Recalled 2", "42 facts"]);
    const again = reduce(stored, ev("memory.activity", 53, { op: "retrieve", count: 0 }));
    expect(nodeFacts("memory", again.subsystems.memory, null)).toEqual(["Recalled 0", "42 facts"]);
    // A snapshot carries only the tracker's last op and count: read it the same way.
    const fromSnapshot = reduce(initialMissionState(), snapshot({
      subsystems: { memory: { status: "online", last_op: "store", count: 17 } },
    }));
    expect(nodeFacts("memory", fromSnapshot.subsystems.memory, null)).toEqual(["17 facts"]);
    expect(nodeFacts("memory", initialMissionState().subsystems.memory, null)).toEqual([]);
  });

  it("does not double-count tool results after a snapshot plus recent_events (dedupe by seq)", () => {
    const toolEvent = (seq: number, tool: string, status = "success") => ev("tool.execution", seq, { tool, status });
    // The tracker already folded read_file (45); write_file (47) was still in flight to it.
    const after = reduce(initialMissionState(), snapshot({
      subsystems: { tools: { status: "online", active: [], recent: [
        { tool: "old_tool", status: "error" }, { tool: "read_file", status: "success" },
      ] } },
      recent_events: [toolEvent(45, "read_file"), toolEvent(47, "write_file")],
    }));
    expect(toolEntries(after.subsystems.tools.recent).map((entry) => [entry.tool, entry.seq])).toEqual([
      ["old_tool", null], ["read_file", 45], ["write_file", 47],
    ]);
    // A replayed copy of an event with a seq already listed is not added again.
    const replayed = reduce({ ...after, seen: [], seqFloor: 0 }, toolEvent(47, "write_file"));
    expect(toolEntries(replayed.subsystems.tools.recent)).toHaveLength(3);
    // Every entry the tracker folded and the ring still holds: nothing from the snapshot list is kept twice.
    const caughtUp = reduce(initialMissionState(), snapshot({
      subsystems: { tools: { status: "online", active: [], recent: [
        { tool: "read_file", status: "success" }, { tool: "read_file", status: "success" },
      ] } },
      recent_events: [toolEvent(40, "read_file"), toolEvent(45, "read_file")],
    }));
    expect(toolEntries(caughtUp.subsystems.tools.recent).map((entry) => entry.seq)).toEqual([40, 45]);
    // The list stays capped at five.
    const many = run(Array.from({ length: 8 }, (_, index) => toolEvent(100 + index, `t${index}`)), after);
    expect(toolEntries(many.subsystems.tools.recent).map((entry) => entry.tool)).toEqual(["t3", "t4", "t5", "t6", "t7"]);
  });

  it("clears live-only facts when the session ends", () => {
    const after = run([
      snapshot(),
      ev("voice.level", 51, { rms: 0.5, phase: "collecting" }),
      ev("llm.call", 52, { phase: "start", model: "m", attempt: 1 }),
      ev("session.end", 53),
    ]);
    expect(after.voiceLevel).toBeNull();
    expect(after.subsystems.llm.in_flight).toBe(false);
  });

  it("stops showing a model call in flight when the request ends (a cancelled call sends no llm.call end)", () => {
    const calling = run([snapshot(), ev("request.start", 51, { source: "terminal", text: "hi" }),
      ev("llm.call", 52, { phase: "start", model: "m", attempt: 1 })]);
    expect(calling.subsystems.llm.in_flight).toBe(true);
    const cancelled = reduce(calling, ev("request.end", 53, { status: "cancelled", duration_ms: 900 }));
    expect(cancelled.subsystems.llm.in_flight).toBe(false);
  });
});


// --- Task 10: commands, replies and approvals ------------------------------------------------------

const confirmRequest = (seq: number, id = "c1", extra: Record<string, unknown> = {}) =>
  ev("confirm.request", seq, {
    id, digest: `digest-${id}`, tool: "edit_workspace_file", label: "ACTION", operation: "edit notes.txt",
    affected_paths: ["notes.txt"], reason: "", hidden: 0, lines: [{ k: "add", t: "+hello" }], omitted: 0,
    browser_approvable: true, expires_at: null, ...extra,
  });
const resolved = (seq: number, id = "c1", approved = true, channel = "browser") =>
  ev("confirm.resolved", seq, { id, approved, channel });
const sent = (id: string, text = "what time is it?"): MissionInput => ({ type: "command.sent", id, text, at: 1 });
const ack = (seq: number, id: string) => ev("command.ack", seq, { command_id: id, status: "accepted" });
const requestStart = (seq: number, requestId: string, commandId?: string) =>
  ({ ...ev("request.start", seq, { source: commandId ? "browser" : "terminal", text: "what time is it?",
    ...(commandId ? { command_id: commandId } : {}) }), request_id: requestId }) as ServerEvent;
const requestEnd = (seq: number, requestId: string, status = "complete") =>
  ({ ...ev("request.end", seq, { status, duration_ms: 1200 }), request_id: requestId }) as ServerEvent;
const command = (state: MissionState, id: string) => state.commands.find((entry) => entry.id === id);

describe("approvals", () => {
  it("clears the panel and the answer state on confirm.resolved for the open request", () => {
    const open = run([snapshot(), confirmRequest(51)]);
    const answering = reduce(open, { type: "confirm.sent", id: "c1", approved: true });
    expect(answering.confirmAnswer).toEqual({ id: "c1", approved: true, phase: "sending", result: null });
    const done = reduce(answering, resolved(52));
    expect(done.pendingConfirmation).toBeNull();
    expect(done.confirmAnswer).toBeNull();
  });

  it("keeps the panel when another confirmation id is resolved", () => {
    const open = run([snapshot(), confirmRequest(51)]);
    const other = reduce(open, resolved(52, "older"));
    expect(other.pendingConfirmation?.id).toBe("c1");
  });

  it("records the server's answer result for the answer this page sent, and ignores others", () => {
    const open = run([snapshot(), confirmRequest(51), { type: "confirm.sent", id: "c1", approved: false }]);
    const foreign = reduce(open, { type: "confirm.answer.result", id: "zz", result: "stale" });
    expect(foreign.confirmAnswer?.phase).toBe("sending");
    const answered = reduce(open, { type: "confirm.answer.result", id: "c1", result: "digest_mismatch" });
    expect(answered.confirmAnswer).toEqual({ id: "c1", approved: false, phase: "answered", result: "digest_mismatch" });
  });

  it("ignores an answer sent for a confirmation that is not the open one", () => {
    const open = run([snapshot(), confirmRequest(51)]);
    expect(reduce(open, { type: "confirm.sent", id: "gone", approved: true }).confirmAnswer).toBeNull();
  });

  it("forgets an unanswered send when the link drops, so the buttons come back after a reconnect", () => {
    const answering = run([snapshot(), confirmRequest(51), { type: "confirm.sent", id: "c1", approved: true }]);
    const dropped = reduce(answering, { type: "connection", status: "reconnecting" });
    expect(dropped.confirmAnswer).toBeNull();
    expect(dropped.pendingConfirmation?.id).toBe("c1");
  });

  it("applies a confirmation resent after a replay with its original, older seq", () => {
    // Live up to 60; the request (seq 55, transient) was missed while the tab was away; the replay brings
    // 56-60 and then the server resends the open request under its original seq 55.
    const live = run([snapshot(), notice(51), notice(52)]);
    const replayed = run([notice(56), notice(57), notice(60)], live);
    expect(replayed.lastSeq).toBe(60);
    const resent = reduce(replayed, confirmRequest(55));
    expect(resent.pendingConfirmation?.id).toBe("c1");
    expect(resent.pendingConfirmation?.digest).toBe("digest-c1");
    expect(resent.log.map((event) => event.seq)).toEqual([51, 52, 55, 56, 57, 60]);
  });

  it("applies a resent confirmation even when its seq is already below the dedupe floor", () => {
    // A later snapshot (seq 70) raised the floor above the request's seq 55.
    const past = run([snapshot(), snapshot({ seq: 70 })]);
    expect(past.seqFloor).toBe(70);
    const resent = reduce(past, confirmRequest(55));
    expect(resent.pendingConfirmation?.id).toBe("c1");
    // Applying it twice changes nothing.
    expect(reduce(resent, confirmRequest(55))).toBe(resent);
  });

  it("never reopens a confirmation this page already saw resolved", () => {
    const done = run([snapshot(), confirmRequest(51), resolved(52)]);
    expect(reduce(done, confirmRequest(51)).pendingConfirmation).toBeNull();
    const late = run([snapshot(), resolved(52, "c9")]);
    expect(reduce(late, confirmRequest(51, "c9")).pendingConfirmation).toBeNull();
  });

  it("starts a fresh answer state when a different confirmation opens", () => {
    const first = run([snapshot(), confirmRequest(51), { type: "confirm.sent", id: "c1", approved: true }]);
    const second = run([resolved(52), confirmRequest(53, "c2")], first);
    expect(second.pendingConfirmation?.id).toBe("c2");
    expect(second.confirmAnswer).toBeNull();
  });
});

describe("browser commands", () => {
  it("goes queued, then running on ack, then done on request.end", () => {
    const queued = run([snapshot(), sent("k1")]);
    expect(command(queued, "k1")).toMatchObject({ status: "queued", text: "what time is it?" });
    const acked = reduce(queued, ack(51, "k1"));
    expect(command(acked, "k1")?.status).toBe("running");
    const started = reduce(acked, requestStart(52, "r1", "k1"));
    expect(command(started, "k1")).toMatchObject({ status: "running", requestId: "r1" });
    const ended = reduce(started, requestEnd(53, "r1"));
    expect(command(ended, "k1")).toMatchObject({ status: "done", outcome: "complete" });
  });

  it("tolerates command.ack arriving after request.start, and after request.end", () => {
    const early = run([snapshot(), sent("k1"), requestStart(51, "r1", "k1"), ack(52, "k1")]);
    expect(command(early, "k1")).toMatchObject({ status: "running", requestId: "r1" });
    const late = run([snapshot(), sent("k1"), requestStart(51, "r1", "k1"), requestEnd(52, "r1", "cancelled"), ack(53, "k1")]);
    expect(command(late, "k1")).toMatchObject({ status: "done", outcome: "cancelled" });
  });

  it("stays queued on an ack with status queued (never sent today, but allowed)", () => {
    const queued = run([snapshot(), sent("k1"), ev("command.ack", 51, { command_id: "k1", status: "queued" })]);
    expect(command(queued, "k1")?.status).toBe("queued");
  });

  it("is rejected with the server's reason, even after it was accepted (the loop re-checks)", () => {
    const busy = run([snapshot(), sent("k1"), ev("command.rejected", 51, { command_id: "k1", reason: "busy" })]);
    expect(command(busy, "k1")).toMatchObject({ status: "rejected", reason: "busy" });
    const recheck = run([snapshot(), sent("k2", "/memory reset"), ack(51, "k2"),
      ev("command.rejected", 52, { command_id: "k2", reason: "terminal_only" })]);
    expect(command(recheck, "k2")).toMatchObject({ status: "rejected", reason: "terminal_only" });
  });

  it("finishes a slash command on its command.output and shows that output as the latest reply", () => {
    const output = ev("command.output", 52, { command_id: "k1", command: "/status", text: "<b>Model</b>: llama\n" });
    const done = run([snapshot(), sent("k1", "/status"), ack(51, "k1"), output]);
    expect(command(done, "k1")).toMatchObject({ status: "done", outcome: "output" });
    expect(done.latestReply).toEqual({ kind: "output", command: "/status", text: "<b>Model</b>: llama\n", seq: 52 });
  });

  it("ignores command events for ids this page did not send (another tab's commands)", () => {
    const mine = run([snapshot(), sent("k1")]);
    const after = run([ack(51, "other"), ev("command.rejected", 52, { command_id: "other2", reason: "busy" })], mine);
    expect(after.commands).toEqual(mine.commands);
  });

  it("closes a running command with no request of its own once the next request starts", () => {
    // A plain "continue" with nothing to resume runs no request and prints a reason instead.
    const stuck = run([snapshot(), sent("k1", "continue"), ack(51, "k1")]);
    const next = run([sent("k2"), requestStart(52, "r2", "k2")], stuck);
    expect(command(next, "k1")?.status).toBe("done");
    expect(command(next, "k2")?.status).toBe("running");
  });

  it("keeps the newest assistant.message or command.output by seq as the latest reply", () => {
    const replies = run([
      snapshot(),
      ev("assistant.message", 53, { text: "It is 10:42." }),
      ev("command.output", 52, { command_id: "x", command: "/help", text: "older" }),
    ]);
    expect(replies.latestReply).toEqual({ kind: "message", text: "It is 10:42.", seq: 53 });
  });

  it("keeps at most 20 commands", () => {
    const many = run([snapshot(), ...Array.from({ length: 25 }, (_, n) => sent(`k${n}`))]);
    expect(many.commands).toHaveLength(20);
    expect(many.commands[0]?.id).toBe("k5");
  });
});
