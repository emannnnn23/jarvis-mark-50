import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  BACKOFF_BASE_MS,
  BACKOFF_CAP_MS,
  CLOSE_AUTH_TIMEOUT,
  CLOSE_GOING_AWAY,
  CLOSE_OVERLOADED,
  CLOSE_RATE_LIMITED,
  CLOSE_TOO_MANY_CLIENTS,
  CLOSE_UNAUTHORIZED,
  MissionSocket,
  SILENCE_LIMIT_MS,
  STABLE_CONNECTION_MS,
  backoffDelay,
} from "../services/socket";
import type { ConnectionStatus } from "../services/socket";
import { createStatusGate } from "../services/statusGate";
import type { ServerFrame } from "../types/events";

/** Just enough of the browser WebSocket for MissionSocket, driven by the test. */
class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  closedWith: number | undefined;
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string; wasClean: boolean }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error("send on a socket that is not open");
    this.sent.push(data);
  }

  close(code?: number): void {
    this.closedWith = code;
    this.readyState = FakeWebSocket.CLOSING;
  }

  // --- test controls ---
  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }

  serverSend(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  serverClose(code: number): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason: "", wasClean: code !== 1006 });
  }

  sentFrames(): Record<string, unknown>[] {
    return this.sent.map((text) => JSON.parse(text));
  }
}

const latest = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;

const event = (seq: number) => ({ v: 1, seq, type: "notice", timestamp: 1762500000000 + seq, level: "info", text: "hi" });

function make(random: () => number = () => 0.5) {
  const frames: ServerFrame[] = [];
  const statuses: ConnectionStatus[] = [];
  const socket = new MissionSocket(
    "ws://127.0.0.1:8765/ws",
    "the-token",
    (frame) => frames.push(frame),
    (status) => statuses.push(status),
    { WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket, random },
  );
  return { socket, frames, statuses };
}

/** Connect, authenticate, and receive the given event seqs. */
function establish(socket: MissionSocket, seqs: number[] = []): FakeWebSocket {
  socket.connect();
  const ws = latest();
  ws.serverOpen();
  ws.serverSend({ type: "auth.ok", protocol: 1 });
  for (const seq of seqs) ws.serverSend(event(seq));
  return ws;
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("backoffDelay", () => {
  it("doubles from 0.5 s, caps at 15 s and stays within ±20 % jitter", () => {
    expect(BACKOFF_BASE_MS).toBe(500);
    expect(BACKOFF_CAP_MS).toBe(15000);
    const expected = [500, 1000, 2000, 4000, 8000, 15000, 15000, 15000];
    expected.forEach((base, attempt) => {
      expect(backoffDelay(attempt, () => 0.5)).toBe(base);
      expect(backoffDelay(attempt, () => 0)).toBeGreaterThanOrEqual(Math.min(base, BACKOFF_CAP_MS) * 0.8);
      expect(backoffDelay(attempt, () => 0.999999)).toBeLessThanOrEqual(Math.min(base * 1.2, BACKOFF_CAP_MS));
    });
    expect(backoffDelay(0, () => 0)).toBeCloseTo(400);
    expect(backoffDelay(0, () => 0.999999)).toBeCloseTo(600, 0);
  });
});

describe("MissionSocket", () => {
  it("sends auth as the very first frame, with no last_seq on a fresh page", () => {
    const { socket, statuses } = make();
    socket.connect();
    expect(statuses).toEqual(["connecting"]);
    const ws = latest();
    expect(ws.url).toBe("ws://127.0.0.1:8765/ws");
    ws.serverOpen();
    expect(ws.sentFrames()).toEqual([{ type: "auth", token: "the-token", last_seq: null }]);
    ws.serverSend({ type: "auth.ok", protocol: 1 });
    expect(statuses).toEqual(["connecting", "open"]);
  });

  it("hands valid frames over and drops invalid ones", () => {
    const { socket, frames } = make();
    const ws = establish(socket);
    ws.serverSend(event(5));
    ws.serverSend({ type: "made.up", seq: 6 });
    ws.onmessage?.({ data: "not json" });
    ws.onmessage?.({ data: new ArrayBuffer(4) });
    expect(frames.map((frame) => frame.type)).toEqual(["auth.ok", "notice"]);
  });

  it("does not send application messages before auth.ok", () => {
    const { socket } = make();
    socket.connect();
    const ws = latest();
    expect(socket.send({ type: "ping" })).toBe(false);
    ws.serverOpen();
    expect(socket.send({ type: "ping" })).toBe(false);
    ws.serverSend({ type: "auth.ok", protocol: 1 });
    expect(socket.send({ type: "ping" })).toBe(true);
    expect(ws.sentFrames().map((frame) => frame.type)).toEqual(["auth", "ping"]);
  });

  it("backs off 0.5 s, 1 s, 2 s between failed attempts, within the jitter bounds", () => {
    for (const [random, factor] of [[0, 0.8], [0.999999, 1.2]] as const) {
      FakeWebSocket.instances = [];
      const { socket } = make(() => random);
      socket.connect();
      for (const base of [500, 1000, 2000]) {
        const before = FakeWebSocket.instances.length;
        latest().serverClose(1006);
        vi.advanceTimersByTime(Math.floor(base * factor) - 2);
        expect(FakeWebSocket.instances.length, `before ${base} ms × ${factor}`).toBe(before);
        vi.advanceTimersByTime(4);
        expect(FakeWebSocket.instances.length, `after ${base} ms × ${factor}`).toBe(before + 1);
      }
      socket.close();
    }
  });

  it("reports reconnecting, then open again, and resumes with last_seq", () => {
    const { socket, statuses } = make();
    const first = establish(socket, [3, 9, 7]);
    first.serverClose(1006);
    expect(statuses.at(-1)).toBe("reconnecting");
    vi.advanceTimersByTime(BACKOFF_BASE_MS);
    const second = latest();
    expect(second).not.toBe(first);
    second.serverOpen();
    expect(second.sentFrames()[0]).toEqual({ type: "auth", token: "the-token", last_seq: 9 });
    second.serverSend({ type: "auth.ok", protocol: 1 });
    expect(statuses.at(-1)).toBe("open");
  });

  it("takes last_seq from a snapshot too", () => {
    const { socket } = make();
    const ws = establish(socket);
    ws.serverSend({
      type: "snapshot", seq: 120, session: null, state: { state: "idle", detail: null, since: 1 },
      subsystems: {}, task: null, pending_confirmation: null, metrics: null, network: null, recent_events: [],
    });
    ws.serverClose(1013);
    vi.advanceTimersByTime(BACKOFF_CAP_MS);
    latest().serverOpen();
    expect(latest().sentFrames()[0]).toMatchObject({ last_seq: 120 });
  });

  it("resets the backoff once an authenticated connection has stayed up for 5 s", () => {
    const { socket } = make();
    socket.connect();
    latest().serverClose(1006);
    vi.advanceTimersByTime(500);
    latest().serverClose(1006);
    vi.advanceTimersByTime(1000);
    const ws = latest();
    ws.serverOpen();
    ws.serverSend({ type: "auth.ok", protocol: 1 });
    expect(STABLE_CONNECTION_MS).toBe(5000);
    vi.advanceTimersByTime(STABLE_CONNECTION_MS);
    const count = FakeWebSocket.instances.length;
    ws.serverClose(1006);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances.length).toBe(count + 1);
  });

  it("keeps backing off when the server accepts and then drops at once", () => {
    // A flapping server must not get reconnects at ~2 Hz: auth.ok alone does not reset the backoff.
    const { socket } = make();
    socket.connect();
    latest().serverClose(1006); // attempt 0 -> waits 0.5 s
    vi.advanceTimersByTime(500);
    for (const expected of [1000, 2000, 4000]) {
      const ws = latest();
      ws.serverOpen();
      ws.serverSend({ type: "auth.ok", protocol: 1 });
      vi.advanceTimersByTime(100);
      const count = FakeWebSocket.instances.length;
      ws.serverClose(1006);
      vi.advanceTimersByTime(expected - 1);
      expect(FakeWebSocket.instances.length, `before ${expected} ms`).toBe(count);
      vi.advanceTimersByTime(1);
      expect(FakeWebSocket.instances.length, `after ${expected} ms`).toBe(count + 1);
    }
    socket.close();
  });

  it("grows the backoff when the WebSocket constructor throws", () => {
    let throws = 3;
    class ThrowingWebSocket extends FakeWebSocket {
      constructor(url: string) {
        if (throws > 0) {
          throws -= 1;
          throw new Error("refused by CSP");
        }
        super(url);
      }
    }
    const statuses: ConnectionStatus[] = [];
    const socket = new MissionSocket("ws://127.0.0.1:8765/ws", "t", () => undefined, (s) => statuses.push(s), {
      WebSocketImpl: ThrowingWebSocket as unknown as typeof WebSocket, random: () => 0.5,
    });
    socket.connect(); // throw 1 -> 0.5 s
    expect(statuses.at(-1)).toBe("reconnecting");
    vi.advanceTimersByTime(500); // throw 2 -> 1 s
    vi.advanceTimersByTime(999);
    expect(throws).toBe(1);
    vi.advanceTimersByTime(1); // throw 3 -> 2 s
    expect(throws).toBe(0);
    vi.advanceTimersByTime(1999);
    expect(FakeWebSocket.instances).toHaveLength(0);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(1);
    socket.close();
  });

  it("stops reconnecting after 4401", () => {
    const { socket, statuses } = make();
    const ws = establish(socket, [4]);
    ws.serverClose(CLOSE_UNAUTHORIZED);
    expect(CLOSE_UNAUTHORIZED).toBe(4401);
    expect(statuses.at(-1)).toBe("auth_failed");
    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("stops after 4409 (too many tabs) until connect() is called again", () => {
    const { socket, statuses } = make();
    socket.connect();
    latest().serverOpen();
    latest().serverClose(CLOSE_TOO_MANY_CLIENTS);
    expect(statuses.at(-1)).toBe("too_many_clients");
    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    socket.connect(); // the Retry button
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("reconnects after 4408, 1013 and 1006, waits at least 2 s after 4429, and shows offline on 1001", () => {
    for (const code of [CLOSE_AUTH_TIMEOUT, CLOSE_OVERLOADED, 1006]) {
      FakeWebSocket.instances = [];
      const { socket, statuses } = make();
      establish(socket).serverClose(code);
      expect(statuses.at(-1), String(code)).toBe("reconnecting");
      vi.advanceTimersByTime(BACKOFF_BASE_MS);
      expect(FakeWebSocket.instances, String(code)).toHaveLength(2);
      socket.close();
    }
    FakeWebSocket.instances = [];
    const limited = make();
    establish(limited.socket).serverClose(CLOSE_RATE_LIMITED);
    vi.advanceTimersByTime(1999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    limited.socket.close();

    FakeWebSocket.instances = [];
    const shutdown = make();
    establish(shutdown.socket).serverClose(CLOSE_GOING_AWAY);
    expect(shutdown.statuses.at(-1)).toBe("offline");
    vi.advanceTimersByTime(BACKOFF_BASE_MS);
    expect(FakeWebSocket.instances).toHaveLength(2); // keeps trying: JARVIS may come back
    latest().serverClose(1006);
    expect(shutdown.statuses.at(-1)).toBe("offline"); // still offline, not "reconnecting"
  });

  it("reconnects after 45 s of silence with last_seq", () => {
    const { socket } = make();
    const ws = establish(socket, [11, 12]);
    expect(SILENCE_LIMIT_MS).toBe(45000);
    vi.advanceTimersByTime(SILENCE_LIMIT_MS - 1000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1000 + 5000 + BACKOFF_BASE_MS);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(ws.closedWith).toBeDefined();
    ws.serverClose(1006); // the old socket's late close must not start a second reconnect
    const next = latest();
    next.serverOpen();
    expect(next.sentFrames()[0]).toEqual({ type: "auth", token: "the-token", last_seq: 12 });
    vi.advanceTimersByTime(BACKOFF_CAP_MS);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("pings while connected, so a quiet but healthy server is not mistaken for a dead one", () => {
    const { socket } = make();
    const ws = establish(socket);
    for (let i = 0; i < 6; i += 1) {
      vi.advanceTimersByTime(15000);
      const pings = ws.sentFrames().filter((frame) => frame.type === "ping").length;
      expect(pings).toBe(i + 1);
      ws.serverSend({ type: "pong" });
    }
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("close() stops everything for good", () => {
    const { socket } = make();
    const ws = establish(socket);
    socket.close();
    expect(ws.closedWith).toBe(1000);
    ws.serverClose(1000);
    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe("createStatusGate", () => {
  it("hides a short drop: reconnecting shows only after the grace period", () => {
    const shown: ConnectionStatus[] = [];
    const gate = createStatusGate((status) => shown.push(status), 1000);
    gate("open");
    gate("reconnecting");
    vi.advanceTimersByTime(600);
    gate("open");
    vi.advanceTimersByTime(2000);
    expect(shown).toEqual(["open", "open"]);
    gate("reconnecting");
    vi.advanceTimersByTime(1000);
    expect(shown).toEqual(["open", "open", "reconnecting"]);
  });

  it("passes final statuses straight through", () => {
    const shown: ConnectionStatus[] = [];
    const gate = createStatusGate((status) => shown.push(status), 1000);
    gate("reconnecting");
    gate("auth_failed");
    vi.advanceTimersByTime(5000);
    expect(shown).toEqual(["auth_failed"]);
    gate.cancel();
  });
});
