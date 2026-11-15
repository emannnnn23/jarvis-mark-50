import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CommandBarView } from "../components/layout/CommandBar";
import type { CommandBarViewProps } from "../components/layout/CommandBar";
import {
  MAX_COMMAND_LENGTH,
  answerGate,
  answerResultMessage,
  buildAnswer,
  browseHistory,
  cancelAvailable,
  checkLine,
  commandChip,
  countdownText,
  pastedProblem,
  rejectionMessage,
  rememberLine,
} from "../store/commands";
import type { CommandEntry } from "../store/reducer";
import type { ConfirmPayload } from "../types/events";
import { ReplyPanelView } from "../components/panels/ReplyPanel";

const entry = (extra: Partial<CommandEntry>): CommandEntry => ({
  id: "k1", text: "what time is it?", sentAt: 1, status: "queued", reason: null, requestId: null, outcome: null,
  ...extra,
});

describe("rejection messages", () => {
  it("has a human message for every reason the server sends", () => {
    expect(rejectionMessage("terminal_only")).toBe("Type this in the terminal");
    expect(rejectionMessage("busy")).toBe("JARVIS is busy — cancel or wait");
    expect(rejectionMessage("invalid")).toBe("Not a command JARVIS accepts here");
    expect(rejectionMessage("too_long")).toBe("Too long (max 4000 characters)");
    expect(rejectionMessage("queue_full")).toMatch(/waiting/);
  });
});

describe("command chip", () => {
  it("names each stage of a browser command", () => {
    expect(commandChip(entry({ status: "queued" }))).toMatchObject({ label: "queued", tone: "quiet", message: null });
    expect(commandChip(entry({ status: "running" }))).toMatchObject({ label: "running", tone: "active" });
    expect(commandChip(entry({ status: "done", outcome: "complete" }))).toMatchObject({ label: "done", tone: "quiet" });
    expect(commandChip(entry({ status: "done", outcome: "output" }))).toMatchObject({ label: "done" });
    expect(commandChip(entry({ status: "done", outcome: "cancelled" }))).toMatchObject({ label: "cancelled" });
    expect(commandChip(entry({ status: "done", outcome: "error" }))).toMatchObject({ label: "error", tone: "attention" });
  });

  it("says terminal only, with where to type it, for terminal-only commands", () => {
    expect(commandChip(entry({ status: "rejected", reason: "terminal_only" }))).toEqual({
      label: "terminal only", tone: "attention", message: "Type this in the terminal",
    });
  });

  it("says rejected, with the reason in words, for the other refusals", () => {
    expect(commandChip(entry({ status: "rejected", reason: "busy" }))).toEqual({
      label: "rejected", tone: "attention", message: "JARVIS is busy — cancel or wait",
    });
  });
});

describe("paste ending in one newline", () => {
  it("accepts a single line copied with its line ending, and sends it without one", () => {
    expect(pastedProblem("what time is it?\n")).toBeNull();
    expect(pastedProblem("what time is it?\r\n")).toBeNull();
    expect(checkLine("what time is it?\n")).toEqual({ ok: true, text: "what time is it?" });
    expect(checkLine("what time is it?\r\n")).toEqual({ ok: true, text: "what time is it?" });
  });

  it("still refuses more than one line, or more than one line ending", () => {
    expect(pastedProblem("first\nsecond\n")).toMatch(/one line/i);
    expect(pastedProblem("first\n\n")).toMatch(/one line/i);
    expect(pastedProblem("first\r\n\r\n")).toMatch(/one line/i);
    expect(pastedProblem("\nfirst")).toMatch(/one line/i);
  });
});

describe("single-line input", () => {
  it("sends one trimmed line and refuses what the server would refuse", () => {
    expect(checkLine("  what time is it?  ")).toEqual({ ok: true, text: "what time is it?" });
    expect(checkLine("   ")).toEqual({ ok: false, message: null });
    expect(checkLine("a\nb")).toMatchObject({ ok: false });
    expect(checkLine("a\u2028b")).toMatchObject({ ok: false });
    expect(checkLine("x".repeat(MAX_COMMAND_LENGTH))).toMatchObject({ ok: true });
    expect(checkLine("x".repeat(MAX_COMMAND_LENGTH + 1))).toEqual({ ok: false, message: "Too long (max 4000 characters)" });
    // Counted in code points, like the server: 4000 emoji are fine.
    expect(checkLine("😀".repeat(MAX_COMMAND_LENGTH))).toMatchObject({ ok: true });
    expect(checkLine("tab\tinside")).toMatchObject({ ok: true });
  });

  it("explains a multi-line paste instead of sending it", () => {
    expect(pastedProblem("one line")).toBeNull();
    expect(pastedProblem("first\nsecond")).toMatch(/one line/i);
    expect(pastedProblem("first\r\nsecond")).toMatch(/one line/i);
  });
});

describe("history", () => {
  it("walks back and forward through sent lines, then returns to the draft", () => {
    const history = rememberLine(rememberLine(rememberLine([], "one"), "two"), "two");
    expect(history).toEqual(["one", "two"]);
    let step = browseHistory(history, null, "up", "draft");
    expect(step).toEqual({ index: 1, text: "two" });
    step = browseHistory(history, step.index, "up", "draft");
    expect(step).toEqual({ index: 0, text: "one" });
    expect(browseHistory(history, step.index, "up", "draft")).toEqual({ index: 0, text: "one" });
    step = browseHistory(history, 0, "down", "draft");
    expect(step).toEqual({ index: 1, text: "two" });
    expect(browseHistory(history, 1, "down", "draft")).toEqual({ index: null, text: "draft" });
    expect(browseHistory([], null, "up", "draft")).toEqual({ index: null, text: "draft" });
  });

  it("keeps at most 50 lines", () => {
    let history: string[] = [];
    for (let n = 0; n < 60; n += 1) history = rememberLine(history, `line ${n}`);
    expect(history).toHaveLength(50);
    expect(history[0]).toBe("line 10");
  });
});

describe("cancel", () => {
  it("is available while JARVIS is processing or running a tool, or a confirmation is open", () => {
    expect(cancelAvailable("processing", false, true)).toBe(true);
    expect(cancelAvailable("tool", false, true)).toBe(true);
    expect(cancelAvailable("idle", true, true)).toBe(true);
    expect(cancelAvailable("idle", false, true)).toBe(false);
    expect(cancelAvailable("speaking", false, true)).toBe(false);
    expect(cancelAvailable("processing", false, false)).toBe(false);
  });
});

describe("answer results", () => {
  it("maps every result to what the person should know", () => {
    expect(answerResultMessage("stale", true)).toBe("Already answered or expired");
    expect(answerResultMessage("already_answered", false)).toBe("Already answered or expired");
    expect(answerResultMessage("expired", true)).toBe("Already answered or expired");
    expect(answerResultMessage("digest_mismatch", true)).toBe("The request changed — check the terminal");
    expect(answerResultMessage("ok", true)).toBe("Approved");
    expect(answerResultMessage("ok", false)).toBe("Declined");
    expect(answerResultMessage("terminal_only", true)).toMatch(/terminal/);
    expect(answerResultMessage("something new", true)).toMatch(/terminal/);
  });

  it("formats the countdown as minutes and seconds", () => {
    expect(countdownText(61_000 + 500, 500)).toBe("1:01");
    expect(countdownText(10_000, 9_001)).toBe("0:01");
    expect(countdownText(10_000, 10_000)).toBeNull();
    expect(countdownText(3_600_000 * 2, 0)).toBe("120:00");
  });
});

const confirmation = (extra: Partial<ConfirmPayload> = {}): ConfirmPayload => ({
  id: "c1", digest: "d1", tool: "edit_workspace_file", label: "ACTION", operation: "edit notes.txt",
  affected_paths: ["notes.txt"], reason: "", hidden: 0, lines: [], omitted: 0, browser_approvable: true,
  expires_at: null, ...extra,
});

describe("buildAnswer", () => {
  it("sends exactly the type, the payload's id and digest, and a strict boolean", () => {
    const payload = confirmation({ id: "c7", digest: "abc" });
    const approve = buildAnswer(payload, true);
    expect(approve).toEqual({ type: "confirm.answer", id: "c7", digest: "abc", approved: true });
    expect(Object.keys(approve).sort()).toEqual(["approved", "digest", "id", "type"]);
    expect(buildAnswer(payload, false)).toEqual({ type: "confirm.answer", id: "c7", digest: "abc", approved: false });
    // Only a real true approves, whatever reaches it at runtime.
    for (const junk of ["true", 1, "yes", {}, null, undefined]) {
      expect(buildAnswer(payload, junk as unknown as boolean).approved).toBe(false);
    }
  });
});

describe("answerGate", () => {
  const base = { answer: null, now: 1_000, armed: true, connected: true };
  it("allows both answers on an armed, open, unanswered approvable request", () => {
    expect(answerGate({ ...base, payload: confirmation() })).toEqual({ approve: true, decline: true });
  });
  it("never allows Approve before arming, after expiry, or when the browser may not approve", () => {
    expect(answerGate({ ...base, armed: false, payload: confirmation() })).toEqual({ approve: false, decline: true });
    expect(answerGate({ ...base, payload: confirmation({ expires_at: 1_000 }) })).toEqual({ approve: false, decline: true });
    expect(answerGate({ ...base, payload: confirmation({ browser_approvable: false }) })).toEqual({ approve: false, decline: true });
  });
  it("allows nothing while an answer is on its way, after it was taken, or with no link", () => {
    const sending = { id: "c1", approved: true, phase: "sending" as const, result: null };
    const taken = { id: "c1", approved: false, phase: "answered" as const, result: "ok" as const };
    expect(answerGate({ ...base, answer: sending, payload: confirmation() })).toEqual({ approve: false, decline: false });
    expect(answerGate({ ...base, answer: taken, payload: confirmation() })).toEqual({ approve: false, decline: false });
    expect(answerGate({ ...base, connected: false, payload: confirmation() })).toEqual({ approve: false, decline: false });
  });
  it("gives the buttons back after a refused answer, so the person can still decline", () => {
    const changed = { id: "c1", approved: true, phase: "answered" as const, result: "digest_mismatch" as const };
    expect(answerGate({ ...base, answer: changed, payload: confirmation() })).toEqual({ approve: true, decline: true });
  });
});

const viewProps = (extra: Partial<CommandBarViewProps> = {}): CommandBarViewProps => ({
  value: "",
  connected: true,
  chip: null,
  notice: null,
  reply: null,
  replyOpen: false,
  cancelEnabled: false,
  onChange: () => undefined,
  onKeyDown: () => undefined,
  onPaste: () => undefined,
  onSubmit: () => undefined,
  onCancel: () => undefined,
  onToggleReply: () => undefined,
  ...extra,
});

describe("CommandBarView", () => {
  it("is a single-line text input (never a textarea)", () => {
    const html = renderToStaticMarkup(<CommandBarView {...viewProps()} />);
    expect(html).toMatch(/<input[^>]*type="text"/);
    expect(html).not.toContain("<textarea");
  });

  it("shows the chip and its message", () => {
    const html = renderToStaticMarkup(<CommandBarView {...viewProps({
      chip: { label: "terminal only", tone: "attention", message: "Type this in the terminal" },
    })} />);
    expect(html).toContain("terminal only");
    expect(html).toContain("Type this in the terminal");
  });

  it("previews the latest reply on the one reserved line above the bar, as text, with Show all", () => {
    const html = renderToStaticMarkup(<CommandBarView {...viewProps({
      reply: { kind: "output", command: "/resume", text: "<script>x</script>\nline two", seq: 9 },
    })} />);
    expect(html).toMatch(/<div class="command-strip"/);
    expect(html).toContain("&lt;script&gt;x&lt;/script&gt;\nline two");
    expect(html).not.toContain("<script>");
    expect(html).toContain("/resume");
    expect(html).toMatch(/<button[^>]*aria-expanded="false"[^>]*>Show all<\/button>/);
    // Nothing above the bar can grow over the node ring: the full text is not rendered here.
    expect(html).not.toContain("command-reply-body");
  });

  it("puts the notice on the same reserved line", () => {
    const html = renderToStaticMarkup(<CommandBarView {...viewProps({
      notice: "One line at a time: JARVIS takes single-line commands here",
      reply: { kind: "message", text: "It is 10:42.", seq: 9 },
    })} />);
    const strip = html.slice(html.indexOf('<div class="command-strip"'), html.indexOf('<div class="command-row'));
    expect(strip).toContain("One line at a time");
    expect(strip).toContain("It is 10:42.");
  });

  it("shows nothing above the bar while an approval is open", () => {
    const html = renderToStaticMarkup(<CommandBarView {...viewProps({
      aboveHidden: true,
      reply: { kind: "message", text: "It is 10:42.", seq: 9 },
      chip: { label: "terminal only", tone: "attention", message: "Type this in the terminal" },
    })} />);
    expect(html).toMatch(/<div class="command-strip" hidden="">/);
  });

  it("enables Cancel only when asked to", () => {
    const off = renderToStaticMarkup(<CommandBarView {...viewProps()} />);
    expect(off).toMatch(/<button[^>]*disabled=""[^>]*>Cancel<\/button>/);
    const on = renderToStaticMarkup(<CommandBarView {...viewProps({ cancelEnabled: true })} />);
    expect(on).not.toMatch(/<button[^>]*disabled=""[^>]*>Cancel<\/button>/);
  });
});

describe("ReplyPanelView", () => {
  it("shows the whole reply as text in a capped, scrollable area beside the ring", () => {
    const html = renderToStaticMarkup(
      <ReplyPanelView reply={{ kind: "output", command: "/resume", text: "<script>x</script>\nline two", seq: 9 }}
        onClose={() => undefined} />,
    );
    expect(html).toContain("&lt;script&gt;x&lt;/script&gt;\nline two");
    expect(html).not.toContain("<script>");
    expect(html).toMatch(/<pre class="command-reply-body" tabindex="0"/);
    expect(html).toContain("/resume");
    expect(html).toMatch(/<button[^>]*>Close<\/button>/);
  });
});
