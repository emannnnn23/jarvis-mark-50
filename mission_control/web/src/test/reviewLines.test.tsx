import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ConfirmationPanelView, SECRET_BANNER } from "../components/panels/ConfirmationPanel";
import { ReviewLines, segmentLine } from "../components/panels/ReviewLines";
import type { ConfirmPayload } from "../types/events";

const payload = (extra: Partial<ConfirmPayload> = {}): ConfirmPayload => ({
  id: "c1",
  digest: "abc123",
  tool: "edit_workspace_file",
  label: "ACTION",
  operation: "edit notes.txt",
  affected_paths: ["notes.txt", "<img src=x onerror=alert(1)>"],
  reason: "It changes a file.",
  hidden: 0,
  lines: [{ k: "hunk", t: "@@ -1 +1 @@" }, { k: "del", t: "-helo" }, { k: "add", t: "+hello" }],
  omitted: 0,
  browser_approvable: true,
  expires_at: null,
  ...extra,
});

const render = (props: Partial<Parameters<typeof ConfirmationPanelView>[0]> = {}) =>
  renderToStaticMarkup(
    <ConfirmationPanelView
      payload={payload()}
      answer={null}
      now={1_000}
      armed
      connected
      onAnswer={() => undefined}
      {...props}
    />,
  );

/** The text of each <button> in the markup. */
const buttons = (html: string) => [...html.matchAll(/<button[^>]*>(.*?)<\/button>/g)].map((match) => match[1]);

describe("ReviewLines", () => {
  it("renders markup and revealed invisible characters as text, never as HTML", () => {
    const html = renderToStaticMarkup(
      <ReviewLines lines={[{ k: "add", t: "+<script>alert(1)</script>" }, { k: "ctx", t: "a\\u{202e}b" }]} />,
    );
    expect(html).toContain("+&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).not.toContain("<script>");
    // The backend already revealed U+202E as the text \u{202e}: shown exactly, not turned back into the character.
    expect(html).toContain("a\\u{202e}b");
    expect(html).not.toContain("\u202e");
  });

  it("keeps a raw bidi override inert: escaped as text, never as markup", () => {
    const html = renderToStaticMarkup(<ReviewLines lines={[{ k: "ctx", t: "x\u202e<b>y</b>" }]} />);
    expect(html).toContain("x\u202e&lt;b&gt;y&lt;/b&gt;");
  });

  it("renders one span per line, with the line's kind as its class, in a <pre>", () => {
    const html = renderToStaticMarkup(
      <ReviewLines lines={[{ k: "hunk", t: "@@ -1 +1 @@" }, { k: "del", t: "-a" }, { k: "add", t: "+b" }, { k: "ctx", t: "" }]} />,
    );
    expect(html.startsWith("<pre")).toBe(true);
    expect(html.match(/<span/g)).toHaveLength(4);
    expect(html).toContain('<span class="hunk">@@ -1 +1 @@</span>');
    expect(html).toContain('<span class="del">-a</span>');
    expect(html).toContain('<span class="add">+b</span>');
    expect(html).toContain('<span class="ctx"></span>');
  });

  it("shows each line exactly: no trimming, no collapsing, no normalising", () => {
    const text = "   \tindented  twice  \u00a0";
    const html = renderToStaticMarkup(<ReviewLines lines={[{ k: "ctx", t: text }]} />);
    expect(html).toContain(`<span class="ctx">${text}</span>`);
    const decomposed = "e\u0301";
    expect(renderToStaticMarkup(<ReviewLines lines={[{ k: "ctx", t: decomposed }]} />)).toContain(decomposed);
  });

  it("is a keyboard-focusable, labelled region, so a long review can be scrolled without a mouse", () => {
    const html = renderToStaticMarkup(<ReviewLines lines={[{ k: "ctx", t: "x" }]} />);
    expect(html).toMatch(/^<pre[^>]*tabindex="0"/);
    expect(html).toMatch(/^<pre[^>]*role="region"/);
    expect(html).toMatch(/^<pre[^>]*aria-label="Change to review"/);
  });

  it("never uses an unknown kind as a class name", () => {
    const html = renderToStaticMarkup(<ReviewLines lines={[{ k: "panel hud", t: "x" }]} />);
    expect(html).toContain('<span class="ctx">x</span>');
  });
});

const THEME = readFileSync(fileURLToPath(new URL("../styles/theme.css", import.meta.url)), "utf-8");

/** The declarations of the first rule whose selector is exactly `selector`. */
function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = THEME.match(new RegExp(`(^|\\n)${escaped} \\{([^}]*)\\}`));
  expect(match, selector).not.toBeNull();
  return match![2]!;
}

describe("ReviewLines wrapping (a padded line cannot hide its tail)", () => {
  it("wraps every line instead of scrolling sideways", () => {
    const line = rule(".review-lines > span");
    expect(line).toMatch(/white-space:\s*pre-wrap;/);
    expect(line).toMatch(/overflow-wrap:\s*anywhere;/);
    expect(line).toMatch(/unicode-bidi:\s*isolate;/);
    const block = rule(".review-lines");
    expect(block).toMatch(/overflow-x:\s*hidden;/);
    expect(block).toMatch(/overflow-y:\s*auto;/);
    expect(block).not.toMatch(/white-space:\s*pre;/);
  });

  it("isolates text direction in event rows and replies too", () => {
    expect(THEME).toMatch(/\.event-status,\s*\.event-detail,\s*\.command-reply-line,\s*\.command-reply-body,\s*\.confirm-facts dd \{\s*unicode-bidi:\s*isolate;/);
  });
});

describe("ReviewLines revealed characters", () => {
  const literal = "+a = 'x\\u{200b}y'";
  const revealed = "+b = 'x\\u{200b}y'";

  it("marks only the revealed escapes the backend points at, never the typed ones", () => {
    const html = renderToStaticMarkup(
      <ReviewLines lines={[{ k: "add", t: literal }, { k: "add", t: revealed }]} revealed={[[1, 7]]} />,
    );
    expect(html).toContain(`<span class="add">${literal.replace(/'/g, "&#x27;")}</span>`);
    expect(html).toContain('<span class="add">+b = &#x27;x<span class="invisible-char">\\u{200b}</span>y&#x27;</span>');
    expect(html.match(/invisible-char/g)).toHaveLength(1);
  });

  it("keeps the text exactly: the marked line reads the same, character for character", () => {
    const parts = segmentLine("a\\u{202e}b\\u{1f600}", [1, 10]);
    expect(parts.map((part) => part.text).join("")).toBe("a\\u{202e}b\\u{1f600}");
    expect(parts).toEqual([
      { text: "a", revealed: false }, { text: "\\u{202e}", revealed: true }, { text: "b", revealed: false },
      { text: "\\u{1f600}", revealed: true },
    ]);
  });

  it("counts offsets in UTF-16 units, so an emoji before the mark is two", () => {
    expect(segmentLine("+\u{1F600}\\u{200b}", [3])).toEqual([
      { text: "+\u{1F600}", revealed: false }, { text: "\\u{200b}", revealed: true },
    ]);
  });

  it("ignores marks that do not point at a \\u{...} escape, overlap or run backwards", () => {
    for (const marks of [[0], [2], [40], [-1], [1, 1], [10, 1], [1.5]]) {
      const parts = segmentLine("a\\u{202e}b\\u{1f600}", marks);
      expect(parts.map((part) => part.text).join(""), JSON.stringify(marks)).toBe("a\\u{202e}b\\u{1f600}");
      const markedAt = parts.filter((part) => part.revealed).map((part) => part.text);
      expect(markedAt.every((text) => /^\\u\{[0-9a-f]{4,6}\}$/.test(text)), JSON.stringify(marks)).toBe(true);
    }
    expect(segmentLine("x\\u{zz}", [1])).toEqual([{ text: "x\\u{zz}", revealed: false }]);
  });

  it("styles a revealed escape so it stands out from typed text", () => {
    expect(rule(".invisible-char")).toMatch(/background|outline|border/);
  });
});

describe("ConfirmationPanel", () => {
  it("says how many \\u{...} sequences were typed in the text itself, like the terminal", () => {
    const note = "!! 2 literal \\u{...} sequence(s) in the text itself, not hidden characters";
    expect(render({ payload: payload({ literal_escapes: 2 }) })).toContain(note);
    expect(render()).not.toContain("literal");
  });

  it("passes the revealed marks to the review", () => {
    const html = render({ payload: payload({ lines: [{ k: "add", t: "+x\\u{200b}" }], revealed: [[0, 2]] }) });
    expect(html).toContain('<span class="invisible-char">\\u{200b}</span>');
  });

  it("shows what the terminal shows: label, tool, operation, paths and reason, as text", () => {
    const html = render();
    expect(html).toContain("Confirmation required");
    expect(html).toContain("ACTION: edit_workspace_file");
    expect(html).toContain("edit notes.txt");
    expect(html).toContain("notes.txt");
    expect(html).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(html).toContain("It changes a file.");
    expect(html).toContain('<span class="add">+hello</span>');
  });

  it("says how many invisible characters were revealed and how many lines were left out, like the terminal", () => {
    const html = render({ payload: payload({ hidden: 3, omitted: 57 }) });
    expect(html).toContain("!! 3 invisible or text-direction character(s), shown as \\u{...}");
    expect(html).toContain("... 57 more lines not shown");
    const clean = render();
    expect(clean).not.toContain("invisible");
    expect(clean).not.toContain("more lines not shown");
  });

  it("offers Decline and Approve when the browser may approve", () => {
    const html = render();
    expect(buttons(html)).toEqual(["Decline", "Approve"]);
    expect(html).not.toContain(SECRET_BANNER);
  });

  it("offers only Decline, with the amber banner, when the browser may not approve", () => {
    const html = render({ payload: payload({ browser_approvable: false }) });
    expect(buttons(html)).toEqual(["Decline"]);
    expect(html).not.toContain("Approve");
    expect(html).toContain(SECRET_BANNER);
    expect(SECRET_BANNER).toBe(
      "This looks like it contains a secret or is too long to show in full: answer in the terminal",
    );
  });

  it("never autofocuses Approve, keeps it off until the panel has armed, and has no form to submit", () => {
    const unarmed = render({ armed: false });
    expect(unarmed).not.toMatch(/autofocus/i);
    expect(unarmed).not.toContain("<form");
    expect(unarmed).toMatch(/<button[^>]*disabled=""[^>]*>Approve<\/button>/);
    expect(unarmed).not.toMatch(/<button[^>]*disabled=""[^>]*>Decline<\/button>/);
    for (const tag of unarmed.match(/<button[^>]*>/g) ?? []) expect(tag).toContain('type="button"');
  });

  it("takes focus itself (tabIndex -1) as an alert dialog", () => {
    const html = render();
    expect(html).toMatch(/<section[^>]*role="alertdialog"[^>]*tabindex="-1"/);
  });

  it("disables both buttons while an answer is on its way", () => {
    const html = render({ answer: { id: "c1", approved: true, phase: "sending", result: null } });
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Decline<\/button>/);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Approve<\/button>/);
  });

  it("shows the result the server sent", () => {
    const changed = render({ answer: { id: "c1", approved: true, phase: "answered", result: "digest_mismatch" } });
    expect(changed).toContain("The request changed — check the terminal");
    const stale = render({ answer: { id: "c1", approved: true, phase: "answered", result: "stale" } });
    expect(stale).toContain("Already answered or expired");
  });

  it("counts down to expires_at, and stops offering Approve once it has passed", () => {
    const counting = render({ payload: payload({ expires_at: 1_000 + 125_000 }) });
    expect(counting).toContain("Expires in 2:05");
    const expired = render({ payload: payload({ expires_at: 900 }) });
    expect(expired).toContain("Already answered or expired");
    expect(expired).toMatch(/<button[^>]*disabled=""[^>]*>Approve<\/button>/);
    expect(render()).not.toContain("Expires in");
  });

  it("disables both buttons while disconnected", () => {
    const html = render({ connected: false });
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Decline<\/button>/);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Approve<\/button>/);
  });
});
