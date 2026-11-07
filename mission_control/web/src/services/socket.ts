/**
 * The page's one connection to JARVIS: authenticate, hand over valid frames, and come back after a drop.
 *
 * Protocol (mission_control/server.py): the first frame must be {type:"auth", token, last_seq} within 3 s.
 * The server answers auth.ok, then either a replay of the events after last_seq or, when it cannot replay
 * (fresh page, history lost), a snapshot. So a reconnect sends the highest seq this page has seen and the
 * picture resumes where it stopped.
 *
 * Close codes decide what happens next:
 * - 4401 the token was refused (usually a tab left open across a JARVIS restart): stop, ask for the link;
 * - 4409 too many Mission Control tabs: stop until the user presses Retry (connect() again);
 * - 4429 rate limited: retry, but not sooner than 2 s;
 * - 1001 JARVIS is shutting down: show offline, keep retrying (it may be restarted);
 * - 4408, 1013 (this tab fell behind), 1006 (dropped, or refused before the upgrade) and anything else: retry.
 * Retries back off 0.5 s × 2^n, capped at 15 s, with ±20 % jitter so several tabs do not retry in step.
 *
 * Silence: a ping goes out every 15 s and the server answers; if nothing at all arrives for 45 s the
 * connection is presumed dead (a sleeping laptop, a half-open TCP link) and is replaced.
 */

import { isServerFrame } from "../types/guards";
import type { ClientMessage, ConnectionStatus, ServerFrame } from "../types/events";

export type { ConnectionStatus } from "../types/events";

export const CLOSE_NORMAL = 1000;
export const CLOSE_GOING_AWAY = 1001;
export const CLOSE_OVERLOADED = 1013;
export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_AUTH_TIMEOUT = 4408;
export const CLOSE_TOO_MANY_CLIENTS = 4409;
export const CLOSE_RATE_LIMITED = 4429;
/** Our own code when we give up on a silent connection. */
export const CLOSE_STALE = 4000;

export const BACKOFF_BASE_MS = 500;
export const BACKOFF_CAP_MS = 15000;
export const BACKOFF_JITTER = 0.2;
export const RATE_LIMITED_MIN_MS = 2000;
export const PING_INTERVAL_MS = 15000;
export const SILENCE_LIMIT_MS = 45000;
const SILENCE_CHECK_MS = 5000;

/** The wait before retry number `attempt` (0-based): 0.5 s × 2^attempt ±20 %, never above 15 s. */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempt));
  const jitter = 1 - BACKOFF_JITTER + 2 * BACKOFF_JITTER * random();
  return Math.min(BACKOFF_CAP_MS, base * jitter);
}

export interface MissionSocketOptions {
  /** Injected in tests; the browser's WebSocket otherwise. */
  WebSocketImpl?: typeof WebSocket;
  random?: () => number;
}

export class MissionSocket {
  private ws: WebSocket | null = null;
  private authenticated = false;
  private stopped = true;
  private attempt = 0;
  private lastSeq: number | null = null;
  private lastMessageAt = 0;
  private status: ConnectionStatus | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private silenceTimer: ReturnType<typeof setInterval> | null = null;
  private readonly WebSocketImpl: typeof WebSocket;
  private readonly random: () => number;

  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly onEvent: (frame: ServerFrame) => void,
    private readonly onStatus: (status: ConnectionStatus) => void,
    options: MissionSocketOptions = {},
  ) {
    this.WebSocketImpl = options.WebSocketImpl ?? WebSocket;
    this.random = options.random ?? Math.random;
  }

  /** Start (or, after 4401/4409 or close(), start again). Safe to call while already connected. */
  connect(): void {
    if (!this.stopped && this.ws !== null) return;
    this.stopped = false;
    this.attempt = 0;
    this.clearReconnect();
    this.setStatus("connecting");
    this.open();
  }

  /** Send an application message. False when not authenticated yet (nothing is queued). */
  send(message: ClientMessage): boolean {
    if (message.type === "auth" || !this.authenticated) return false;
    return this.write(message);
  }

  /** Stop for good: no reconnect, no timers. */
  close(): void {
    this.stopped = true;
    this.clearReconnect();
    this.drop(CLOSE_NORMAL);
  }

  // --- internals -------------------------------------------------------------------------------------

  private open(): void {
    let ws: WebSocket;
    try {
      ws = new this.WebSocketImpl(this.url);
    } catch {
      // A malformed URL or a CSP refusal throws synchronously; treat it like a failed attempt.
      this.scheduleReconnect(this.attempt, this.status === "offline" ? "offline" : "reconnecting");
      return;
    }
    this.ws = ws;
    this.authenticated = false;
    this.lastMessageAt = Date.now();
    this.startSilenceWatch();
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.lastMessageAt = Date.now();
      this.write({ type: "auth", token: this.token, last_seq: this.lastSeq });
    };
    ws.onmessage = (message: MessageEvent) => {
      if (this.ws !== ws) return;
      this.lastMessageAt = Date.now();
      this.receive(message.data);
    };
    ws.onerror = () => {
      // Always followed by onclose, which decides what to do.
    };
    ws.onclose = (event: CloseEvent) => {
      if (this.ws !== ws) return;
      this.detach();
      this.handleClose(event.code);
    };
  }

  private receive(data: unknown): void {
    if (typeof data !== "string") return;
    let frame: unknown;
    try {
      frame = JSON.parse(data);
    } catch {
      return;
    }
    if (!isServerFrame(frame)) return;
    if (frame.type === "auth.ok") {
      this.authenticated = true;
      this.attempt = 0;
      this.startPing();
      this.setStatus("open");
    } else if ("seq" in frame && typeof frame.seq === "number") {
      this.lastSeq = Math.max(this.lastSeq ?? 0, frame.seq);
    }
    this.onEvent(frame);
  }

  private handleClose(code: number): void {
    if (this.stopped) return;
    if (code === CLOSE_UNAUTHORIZED) {
      this.stopped = true;
      this.setStatus("auth_failed");
      return;
    }
    if (code === CLOSE_TOO_MANY_CLIENTS) {
      this.stopped = true;
      this.setStatus("too_many_clients");
      return;
    }
    const offline = code === CLOSE_GOING_AWAY || this.status === "offline";
    const attempt = this.attempt;
    this.attempt += 1;
    const minimum = code === CLOSE_RATE_LIMITED ? RATE_LIMITED_MIN_MS : 0;
    this.scheduleReconnect(attempt, offline ? "offline" : "reconnecting", minimum);
  }

  private scheduleReconnect(attempt: number, status: ConnectionStatus, minimumMs = 0): void {
    this.setStatus(status);
    this.clearReconnect();
    const delay = Math.max(minimumMs, backoffDelay(attempt, this.random));
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.stopped) this.open();
    }, delay);
  }

  private write(message: ClientMessage): boolean {
    const ws = this.ws;
    if (ws === null || ws.readyState !== this.WebSocketImpl.OPEN) return false;
    try {
      ws.send(JSON.stringify(message));
      return true;
    } catch {
      return false;
    }
  }

  /** Forget the current socket (its late events are ignored) and close it if it is still there. */
  private drop(code: number): void {
    const ws = this.ws;
    this.detach();
    if (ws !== null) {
      try {
        ws.close(code);
      } catch {
        // Already closed.
      }
    }
  }

  private detach(): void {
    const ws = this.ws;
    if (ws !== null) {
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
    }
    this.ws = null;
    this.authenticated = false;
    this.stopPing();
    this.stopSilenceWatch();
  }

  private startPing(): void {
    this.stopPing();
    this.pingTimer = setInterval(() => this.write({ type: "ping" }), PING_INTERVAL_MS);
  }

  private stopPing(): void {
    if (this.pingTimer !== null) clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private startSilenceWatch(): void {
    this.stopSilenceWatch();
    this.silenceTimer = setInterval(() => {
      if (this.ws === null || Date.now() - this.lastMessageAt < SILENCE_LIMIT_MS) return;
      this.drop(CLOSE_STALE);
      this.handleClose(CLOSE_STALE);
    }, SILENCE_CHECK_MS);
  }

  private stopSilenceWatch(): void {
    if (this.silenceTimer !== null) clearInterval(this.silenceTimer);
    this.silenceTimer = null;
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private setStatus(status: ConnectionStatus): void {
    if (status === this.status) return;
    this.status = status;
    this.onStatus(status);
  }
}
