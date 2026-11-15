import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  PLAYER_LATENCY_MS, STALE_CHUNK_MS, pulseAt, speechEnvelope, speechTimeline,
} from "../components/core/speechEnvelope";
import {
  AUTO_DEGRADE, autoDegradeBounds, qualityAfterDecline,
} from "../components/scene/quality";
import { effectiveQuality } from "../hooks/usePerformanceMode";
import { initialMissionState, reduce } from "../store/reducer";
import type { MissionState } from "../store/reducer";
import type { ServerEvent } from "../types/events";

const NOW_EPOCH = 1762500000000;
// "Hello there": 100-350 ms and 400-700 ms.
const WORDS: [number, number][] = [[100, 250], [400, 300]];

function voiceOutput(seq: number, payload: Record<string, unknown>, timestamp = NOW_EPOCH): ServerEvent {
  return { v: 1, seq, type: "voice.output", timestamp, ...payload } as unknown as ServerEvent;
}

const chunk = (seq: number, extra: Record<string, unknown> = {}, timestamp = NOW_EPOCH) =>
  voiceOutput(seq, { phase: "chunk", index: 0, words: WORDS, timing: "exact", duration_ms: 900, ...extra }, timestamp);

const run = (events: ServerEvent[], from: MissionState = initialMissionState()) =>
  events.reduce<MissionState>((current, event) => reduce(current, event), from);

describe("speechEnvelope", () => {
  it("peaks inside each word window", () => {
    expect(speechEnvelope(WORDS, 100 + 125)).toBeCloseTo(1, 5);
    expect(speechEnvelope(WORDS, 400 + 150)).toBeCloseTo(1, 5);
    expect(speechEnvelope(WORDS, 150)).toBeGreaterThan(0.3);
    expect(speechEnvelope(WORDS, 650)).toBeGreaterThan(0.3);
  });

  it("is 0 before, between and after the words", () => {
    for (const t of [-500, 0, 99, 100, 350, 351, 375, 399, 400, 700, 701, 5000]) {
      expect(speechEnvelope(WORDS, t)).toBe(0);
    }
  });

  it("stays within 0..1 everywhere, overlapping or odd windows included", () => {
    const odd: [number, number][] = [[0, 300], [100, 300], [500, 0], [600, -20]];
    for (let t = -50; t <= 800; t += 7) {
      const value = speechEnvelope(odd, t);
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThanOrEqual(1);
    }
    expect(speechEnvelope(odd, 550)).toBe(0); // a zero or negative duration is no window
    expect(speechEnvelope([], 100)).toBe(0);
  });
});

describe("speechTimeline and pulseAt", () => {
  it("starts the words at the time the chunk arrived plus the player latency", () => {
    expect(PLAYER_LATENCY_MS).toBe(150);
    const state = run([chunk(5)]);
    const received = 10_000;
    const timeline = speechTimeline(state.speech, received, NOW_EPOCH + 20);
    expect(timeline).not.toBeNull();
    expect(timeline?.start).toBe(received + 150);
    expect(timeline?.end).toBe(700); // the last word ends 700 ms in
    // The first word peaks 150 ms (latency) + 225 ms (its middle) after receipt; nothing before latency.
    expect(pulseAt(timeline, received + 150 + 225)).toBeCloseTo(1, 5);
    expect(pulseAt(timeline, received + 100)).toBe(0);
    expect(pulseAt(timeline, received + 150 + 375)).toBe(0); // between the two words
    expect(pulseAt(timeline, received + 150 + 550)).toBeCloseTo(1, 5);
    expect(pulseAt(timeline, received + 150 + 900)).toBe(0);
  });

  it("gives no timeline without exact timings, for a stale replayed chunk, or with nothing playing", () => {
    expect(speechTimeline(null, 0, NOW_EPOCH)).toBeNull();
    expect(speechTimeline(run([chunk(5, { timing: "none", words: [] })]).speech, 0, NOW_EPOCH)).toBeNull();
    expect(speechTimeline(run([chunk(5, { words: [] })]).speech, 0, NOW_EPOCH)).toBeNull();
    const old = run([chunk(5, {}, NOW_EPOCH - STALE_CHUNK_MS - 1)]).speech;
    expect(speechTimeline(old, 0, NOW_EPOCH)).toBeNull();
    expect(pulseAt(null, 12345)).toBe(0);
  });
});

describe("reduce: speech chunks", () => {
  it("keeps the current chunk while speaking and clears it on end, error and a new start", () => {
    let state = run([voiceOutput(1, { phase: "start", timing: "none" }), chunk(2)]);
    expect(state.speech).toMatchObject({ seq: 2, index: 0, timing: "exact", words: WORDS, duration_ms: 900 });
    state = run([chunk(3, { index: 1, words: [[0, 200]] })], state);
    expect(state.speech?.index).toBe(1);
    expect(run([voiceOutput(4, { phase: "end", timing: "none", duration_ms: 2000 })], state).speech).toBeNull();
    expect(run([voiceOutput(4, { phase: "error", timing: "none" })], state).speech).toBeNull();
    expect(run([voiceOutput(4, { phase: "start", timing: "none" })], state).speech).toBeNull();
  });

  it("ignores a chunk older than the one it holds", () => {
    const state = run([chunk(7, { index: 2 }), chunk(6, { index: 1 })]);
    expect(state.speech?.index).toBe(2);
  });
});

describe("auto-degrade", () => {
  it("drops exactly one level per sustained slow period, and never below Low", () => {
    expect(qualityAfterDecline("high")).toBe("balanced");
    expect(qualityAfterDecline("balanced")).toBe("low");
    expect(qualityAfterDecline("low")).toBeNull();
  });

  it("measures for a sustained period and never raises the level by itself", () => {
    expect(AUTO_DEGRADE.sampleMs * AUTO_DEGRADE.samples).toBeGreaterThanOrEqual(5000);
    const [lower, upper] = autoDegradeBounds();
    expect(lower).toBe(AUTO_DEGRADE.minFps);
    expect(upper).toBe(Infinity);
  });

  it("caps the chosen quality at the degraded level, and reduced motion still wins", () => {
    expect(effectiveQuality("high", false, null)).toBe("high");
    expect(effectiveQuality("high", false, "balanced")).toBe("balanced");
    expect(effectiveQuality("balanced", false, "low")).toBe("low");
    expect(effectiveQuality("low", false, "balanced")).toBe("low"); // a cap never raises the level
    expect(effectiveQuality(null, false, null)).toBe("balanced");
    expect(effectiveQuality(null, false, "low")).toBe("low");
    expect(effectiveQuality("high", true, null)).toBe("low");
  });
});

describe("bloom stays lazy", () => {
  const read = (path: string) => readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf-8");

  it("is reached only through a dynamic import, rendered only at High", () => {
    const scene = read("../components/scene/Scene.tsx");
    expect(scene).toMatch(/lazy\(\(\) => import\("\.\/BloomPass"\)\)/);
    expect(scene).not.toMatch(/^import[^;]*(BloomPass|postprocessing)/m);
    expect(scene).toMatch(/quality === "high" && \(/);
    expect(read("../components/scene/BloomPass.tsx")).toMatch(/from "@react-three\/postprocessing"/);
  });
});
