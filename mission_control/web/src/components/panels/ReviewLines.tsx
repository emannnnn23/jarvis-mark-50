/**
 * A confirmation's review (usually a unified diff), exactly as the terminal shows it.
 *
 * The backend has already revealed invisible and text-direction characters as \u{...} text and cut the
 * review at 400 lines; this component adds nothing and takes nothing away. Each line is one <span> with
 * its kind as the class, inside a <pre>, rendered as React text: no trimming, no normalising, no markup.
 * Long lines wrap (white-space: pre-wrap, see theme.css) rather than scroll sideways, so no part of a
 * line can sit out of sight. What a person approves is what they read here, character for character.
 *
 * A revealed \u{200b} and the same eight characters typed in the file read alike, so the backend says
 * where each revealed one starts (`revealed`: [line, UTF-16 offset]); those are wrapped in their own
 * <span class="invisible-char"> to stand out. The text itself is unchanged: only the styling differs.
 */

import type { ReviewLine } from "../../types/events";

const KINDS: ReadonlySet<string> = new Set(["add", "del", "hunk", "ctx"]);

/** A revealed character as the backend writes it: \u{ then 4 to 6 lowercase hex digits then }. */
const ESCAPE = /^\\u\{[0-9a-f]{4,6}\}/;

export interface LineSegment {
  text: string;
  revealed: boolean;
}

/**
 * Splits one line at the revealed escapes that start at `starts` (UTF-16 offsets, as the backend sends
 * them). A mark that does not point at a \u{...} escape, overlaps the one before or is out of order is
 * ignored: the line is then shown with that part as plain text, never changed.
 */
export function segmentLine(text: string, starts: readonly number[]): LineSegment[] {
  const parts: LineSegment[] = [];
  let cursor = 0;
  for (const start of starts) {
    if (!Number.isInteger(start) || start < cursor) continue;
    const match = ESCAPE.exec(text.slice(start, start + 10));
    if (match === null) continue;
    if (start > cursor) parts.push({ text: text.slice(cursor, start), revealed: false });
    parts.push({ text: match[0], revealed: true });
    cursor = start + match[0].length;
  }
  if (cursor < text.length || parts.length === 0) parts.push({ text: text.slice(cursor), revealed: false });
  return parts;
}

/** The marks for each line, in order. */
function marksByLine(revealed: readonly (readonly [number, number])[] | undefined): Map<number, number[]> {
  const byLine = new Map<number, number[]>();
  for (const [line, start] of revealed ?? []) {
    const list = byLine.get(line);
    if (list === undefined) byLine.set(line, [start]);
    else list.push(start);
  }
  for (const list of byLine.values()) list.sort((a, b) => a - b);
  return byLine;
}

export function ReviewLines({ lines, revealed }: {
  lines: readonly ReviewLine[];
  revealed?: readonly (readonly [number, number])[];
}) {
  const marks = marksByLine(revealed);
  return (
    // Focusable and labelled, so a long review can be scrolled from the keyboard and is announced.
    <pre className="review-lines" tabIndex={0} role="region" aria-label="Change to review">
      {lines.map((line, index) => {
        const starts = marks.get(index);
        // A kind the page does not know is shown as context, never used as an arbitrary class name.
        const kind = KINDS.has(line.k) ? line.k : "ctx";
        if (starts === undefined) return <span key={index} className={kind}>{line.t}</span>;
        return (
          <span key={index} className={kind}>
            {segmentLine(line.t, starts).map((part, at) =>
              part.revealed ? <span key={at} className="invisible-char">{part.text}</span> : part.text,
            )}
          </span>
        );
      })}
    </pre>
  );
}
