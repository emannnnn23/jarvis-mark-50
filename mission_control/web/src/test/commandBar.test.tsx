import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CommandBarView } from "../components/layout/CommandBar";
import type { CommandBarViewProps } from "../components/layout/CommandBar";
import {
  MAX_COMMAND_LENGTH,
  answerResultMessage,
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

const viewProps = (extra: Partial<CommandBarViewProps> = {}): CommandBarViewProps => ({
  value: "",
  connected: true,
  chip: null,
  notice: null,
  reply: null,
  replyHidden: false,
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

  it("shows the latest reply as text in a capped, scrollable area", () => {
    const html = renderToStaticMarkup(<CommandBarView {...viewProps({
      reply: { kind: "output", command: "/resume", text: "<script>x</script>\nline two", seq: 9 },
    })} />);
    expect(html).toContain("&lt;script&gt;x&lt;/script&gt;\nline two");
    expect(html).not.toContain("<script>");
    expect(html).toContain('class="command-reply-body"');
    expect(html).toContain("/resume");
  });

  it("shows nothing above the bar while an approval is open", () => {
    const html = renderToStaticMarkup(<CommandBarView {...viewProps({
      aboveHidden: true,
      reply: { kind: "message", text: "It is 10:42.", seq: 9 },
      chip: { label: "terminal only", tone: "attention", message: "Type this in the terminal" },
    })} />);
    expect(html).toMatch(/<div class="command-above" hidden="">/);
  });

  it("enables Cancel only when asked to", () => {
    const off = renderToStaticMarkup(<CommandBarView {...viewProps()} />);
    expect(off).toMatch(/<button[^>]*disabled=""[^>]*>Cancel<\/button>/);
    const on = renderToStaticMarkup(<CommandBarView {...viewProps({ cancelEnabled: true })} />);
    expect(on).not.toMatch(/<button[^>]*disabled=""[^>]*>Cancel<\/button>/);
  });
});
