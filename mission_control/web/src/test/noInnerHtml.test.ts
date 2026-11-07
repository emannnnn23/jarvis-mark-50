import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Backend text reaches the page only as React text nodes. This scan keeps the escape hatches out of
// src/ for good. src/test/ is skipped: it would match its own pattern strings.
const SRC = fileURLToPath(new URL("..", import.meta.url));
const TESTS = fileURLToPath(new URL(".", import.meta.url));
const FORBIDDEN = /dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function\s*\(/;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return path.startsWith(TESTS.replace(/\/$/, "")) ? [] : sourceFiles(path);
    return /\.(ts|tsx|js|jsx|html)$/.test(name) ? [path] : [];
  });
}

describe("no raw HTML or eval in src/", () => {
  it("finds the source files to scan", () => {
    expect(sourceFiles(SRC).length).toBeGreaterThan(10);
  });

  it("uses none of the HTML/eval escape hatches", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      readFileSync(file, "utf-8").split("\n").forEach((line, index) => {
        if (FORBIDDEN.test(line)) offenders.push(`${relative(SRC, file)}:${index + 1}: ${line.trim()}`);
      });
    }
    expect(offenders).toEqual([]);
  });

  it("would catch an offender", () => {
    expect(FORBIDDEN.test("el.innerHTML = text")).toBe(true);
    expect(FORBIDDEN.test("<div dangerouslySetInnerHTML={x} />")).toBe(true);
    expect(FORBIDDEN.test("eval (code)")).toBe(true);
    expect(FORBIDDEN.test("const evaluate = (x) => x")).toBe(false);
  });
});
