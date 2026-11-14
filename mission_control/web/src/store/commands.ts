/**
 * The command bar's and the approval panel's rules, as pure functions so they are tested as values.
 *
 * The server is the judge of what the browser may send (mission_control/bridge.py): one line, at most
 * 4000 characters, no control or line-separator characters, no terminal-only or unknown slash commands.
 * The page checks the first three itself only to say so at once, in words; it never decides that a
 * command is allowed. Every refusal the server sends has a human message here.
 */

import type { CommandRejectedEvent, ConfirmAnswerMessage, ConfirmPayload, JarvisState } from "../types/events";
import type { CommandEntry, ConfirmAnswer } from "./reducer";

/** bridge.py MAX_TEXT; counted in code points, like Python. */
export const MAX_COMMAND_LENGTH = 4000;
export const HISTORY_KEPT = 50;

const TOO_LONG = "Too long (max 4000 characters)";

const REJECTION_MESSAGES: Record<CommandRejectedEvent["reason"], string> = {
  terminal_only: "Type this in the terminal",
  busy: "JARVIS is busy — cancel or wait",
  invalid: "Not a command JARVIS accepts here",
  too_long: TOO_LONG,
  queue_full: "JARVIS already has a command waiting: try again in a moment",
};

export function rejectionMessage(reason: CommandRejectedEvent["reason"]): string {
  return REJECTION_MESSAGES[reason] ?? REJECTION_MESSAGES.invalid;
}

export interface Chip {
  label: string;
  /** quiet: nothing to do; active: JARVIS is on it; attention: the person should read the message. */
  tone: "quiet" | "active" | "attention";
  message: string | null;
}

const OUTCOME_LABELS: Record<NonNullable<CommandEntry["outcome"]>, Chip> = {
  complete: { label: "done", tone: "quiet", message: null },
  output: { label: "done", tone: "quiet", message: null },
  incomplete: { label: "incomplete", tone: "attention", message: "JARVIS stopped before finishing: see its reply" },
  cancelled: { label: "cancelled", tone: "quiet", message: null },
  error: { label: "error", tone: "attention", message: "The request failed: details are in the terminal" },
};

/** What the chip beside the input says about the latest command this page sent. */
export function commandChip(entry: CommandEntry): Chip {
  switch (entry.status) {
    case "queued":
      return { label: "queued", tone: "quiet", message: null };
    case "running":
      return { label: "running", tone: "active", message: null };
    case "done":
      return OUTCOME_LABELS[entry.outcome ?? "complete"];
    case "rejected": {
      const reason = entry.reason ?? "invalid";
      return {
        label: reason === "terminal_only" ? "terminal only" : "rejected",
        tone: "attention",
        message: rejectionMessage(reason),
      };
    }
  }
}

// Tab is allowed; every other C0/C1 control character, DEL and the Unicode line and paragraph
// separators split or reshape a line, so the server refuses them (bridge.py _has_control_characters).
const LINE_BREAKING = /[\u0000-\u0008\u000a-\u001f\u007f-\u009f\u2028\u2029]/u;

const MULTI_LINE = "One line at a time: JARVIS takes single-line commands here";

export type LineCheck = { ok: true; text: string } | { ok: false; message: string | null };

/**
 * `text` without one trailing line ending. A line copied from a terminal or an editor usually carries
 * its newline; that is still one line, so it is pasted (and sent) without it. A second line ending,
 * or one anywhere else, still means more than one line.
 */
export function withoutLineEnding(text: string): string {
  return text.replace(/\r?\n$/u, "");
}

/** The line to send, or why not (message null: nothing to send, say nothing). */
export function checkLine(value: string): LineCheck {
  const text = withoutLineEnding(value).trim();
  if (text === "") return { ok: false, message: null };
  if (LINE_BREAKING.test(text)) return { ok: false, message: MULTI_LINE };
  if ([...text].length > MAX_COMMAND_LENGTH) return { ok: false, message: TOO_LONG };
  return { ok: true, text };
}

/** Why a paste is refused instead of pasted (null: paste it). */
export function pastedProblem(text: string): string | null {
  return LINE_BREAKING.test(withoutLineEnding(text)) ? MULTI_LINE : null;
}

/** The history with `line` added last (not twice in a row), at most HISTORY_KEPT lines. */
export function rememberLine(history: readonly string[], line: string): string[] {
  if (history[history.length - 1] === line) return [...history];
  return [...history, line].slice(-HISTORY_KEPT);
}

/**
 * One step through the history with the arrow keys. index null means the person is on their draft;
 * stepping down past the newest line brings the draft back.
 */
export function browseHistory(
  history: readonly string[], index: number | null, direction: "up" | "down", draft: string,
): { index: number | null; text: string } {
  if (history.length === 0) return { index: null, text: draft };
  if (direction === "up") {
    const next = index === null ? history.length - 1 : Math.max(0, index - 1);
    return { index: next, text: history[next]! };
  }
  if (index === null || index >= history.length - 1) return { index: null, text: draft };
  return { index: index + 1, text: history[index + 1]! };
}

/**
 * Cancel works like Ctrl+C in the terminal: while JARVIS is working on a request, or waiting for an
 * approval (cancelling declines it). Only over an open link.
 */
export function cancelAvailable(state: JarvisState, confirmationOpen: boolean, connected: boolean): boolean {
  return connected && (state === "processing" || state === "tool" || confirmationOpen);
}

/** The server's answer to this page's Approve or Decline, in words. "expired": the countdown ran out. */
export function answerResultMessage(result: string, approved: boolean): string {
  switch (result) {
    case "ok":
      return approved ? "Approved" : "Declined";
    case "stale":
    case "already_answered":
    case "expired":
      return "Already answered or expired";
    case "digest_mismatch":
      return "The request changed — check the terminal";
    case "terminal_only":
      return "This one can only be approved in the terminal";
    default:
      return "JARVIS did not take that answer: check the terminal";
  }
}

/** m:ss until `expiresAt` (epoch ms), or null once it has passed. */
export function countdownText(expiresAt: number, now: number): string | null {
  const left = Math.ceil((expiresAt - now) / 1000);
  if (left <= 0) return null;
  const minutes = Math.floor(left / 60);
  const seconds = left % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/**
 * The confirm.answer frame for this request: exactly its id and digest (so the broker approves only what
 * was shown) and a strict boolean. Nothing else from the payload is echoed back.
 */
export function buildAnswer(payload: Pick<ConfirmPayload, "id" | "digest">, approved: boolean): ConfirmAnswerMessage {
  return { type: "confirm.answer", id: payload.id, digest: payload.digest, approved: approved === true };
}

export interface AnswerGateInput {
  payload: ConfirmPayload;
  answer: ConfirmAnswer | null;
  /** Epoch ms. */
  now: number;
  /** Approve has armed (the short delay after the panel opened has passed). */
  armed: boolean;
  connected: boolean;
}

/**
 * Which answers may be sent right now. The panel uses it for the buttons' disabled state, and the click
 * handler checks it again before sending, so a disabled attribute is never the only guard.
 * - Nothing while an answer is on its way, once one was taken ("ok"), or with no link.
 * - Approve also needs the panel armed, the request not expired, and the browser allowed to approve.
 */
export function answerGate({ payload, answer, now, armed, connected }: AnswerGateInput): { approve: boolean; decline: boolean } {
  const settled = answer !== null && answer.id === payload.id && (answer.phase === "sending" || answer.result === "ok");
  const decline = connected && !settled;
  const expired = payload.expires_at !== null && countdownText(payload.expires_at, now) === null;
  return { approve: decline && armed && !expired && payload.browser_approvable, decline };
}
