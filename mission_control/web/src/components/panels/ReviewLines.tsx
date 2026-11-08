/**
 * A confirmation's review (usually a unified diff), exactly as the terminal shows it.
 *
 * The backend has already revealed invisible and text-direction characters as \u{...} text and cut the
 * review at 400 lines; this component adds nothing and takes nothing away. Each line is one <span> with
 * its kind as the class, inside a <pre> (white-space: pre), rendered as React text: no trimming, no
 * normalising, no markup. What a person approves is what they read here, character for character.
 */

import type { ReviewLine } from "../../types/events";

const KINDS: ReadonlySet<string> = new Set(["add", "del", "hunk", "ctx"]);

export function ReviewLines({ lines }: { lines: readonly ReviewLine[] }) {
  return (
    <pre className="review-lines">
      {lines.map((line, index) => (
        // A kind the page does not know is shown as context, never used as an arbitrary class name.
        <span key={index} className={KINDS.has(line.k) ? line.k : "ctx"}>{line.t}</span>
      ))}
    </pre>
  );
}
