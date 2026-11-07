import { describe, expect, it } from "vitest";

import { NARROW_MAX_WIDTH, cameraDistance, cameraFov, sidePanelInset } from "../components/scene/cameraFit";
import { particleCount } from "../components/scene/quality";
import { DEMAND_FPS, EVENT_ACTIVE_MS, demandFps, frameloopFor } from "../hooks/useAdaptiveFrameloop";
import type { FrameloopInput } from "../hooks/useAdaptiveFrameloop";
import { QUALITY_STORAGE_KEY, effectiveQuality, readStoredQuality, writeStoredQuality } from "../hooks/usePerformanceMode";

const NOW = 1_000_000;

const base: FrameloopInput = {
  hidden: false, state: "idle", interacting: false, lastEventAt: null, now: NOW, quality: "balanced",
};

describe("frameloopFor", () => {
  const table: [string, Partial<FrameloopInput>, "never" | "always" | "demand"][] = [
    ["idle with nothing happening", {}, "demand"],
    ["idle in low quality", { quality: "low" }, "demand"],
    ["offline", { state: "offline" }, "demand"],
    ["hidden tab, even while working", { hidden: true, state: "processing", interacting: true }, "never"],
    ["listening", { state: "listening" }, "always"],
    ["processing", { state: "processing" }, "always"],
    ["speaking", { state: "speaking" }, "always"],
    ["running a tool", { state: "tool" }, "always"],
    ["error hold", { state: "error" }, "always"],
    ["active state in low quality", { state: "processing", quality: "low" }, "always"],
    ["camera interaction", { interacting: true }, "always"],
    ["just after an event", { lastEventAt: NOW - 200 }, "always"],
    ["right at the end of the event window", { lastEventAt: NOW - EVENT_ACTIVE_MS + 1 }, "always"],
    ["after the event window", { lastEventAt: NOW - EVENT_ACTIVE_MS }, "demand"],
    ["long after an event", { lastEventAt: NOW - 60_000 }, "demand"],
  ];
  for (const [name, overrides, expected] of table) {
    it(`${name} -> ${expected}`, () => {
      expect(frameloopFor({ ...base, ...overrides })).toBe(expected);
    });
  }

  it("keeps an event alive for 1.5 s", () => {
    expect(EVENT_ACTIVE_MS).toBe(1500);
  });
});

describe("demandFps", () => {
  it("breathes at 15 fps when idle, and not at all in Low", () => {
    expect(DEMAND_FPS).toBe(15);
    expect(demandFps("high")).toBe(15);
    expect(demandFps("balanced")).toBe(15);
    expect(demandFps("low")).toBe(0);
  });
});

/** A Storage stand-in: an in-memory map, or one that throws like a blocked or private-mode store. */
function fakeStorage(initial: Record<string, string> = {}, broken = false) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem(key: string) {
      if (broken) throw new Error("SecurityError");
      return data.get(key) ?? null;
    },
    setItem(key: string, value: string) {
      if (broken) throw new Error("QuotaExceededError");
      data.set(key, value);
    },
  };
}

describe("quality choice", () => {
  it("is remembered in localStorage and read back", () => {
    const storage = fakeStorage();
    expect(readStoredQuality(storage)).toBeNull();
    writeStoredQuality(storage, "high");
    expect(storage.data.get(QUALITY_STORAGE_KEY)).toBe("high");
    expect(readStoredQuality(storage)).toBe("high");
  });

  it("ignores junk and survives blocked storage", () => {
    expect(readStoredQuality(fakeStorage({ [QUALITY_STORAGE_KEY]: "ultra" }))).toBeNull();
    expect(readStoredQuality(fakeStorage({}, true))).toBeNull();
    expect(() => writeStoredQuality(fakeStorage({}, true), "low")).not.toThrow();
    expect(readStoredQuality(null)).toBeNull();
  });

  it("defaults to Balanced, follows the choice, and prefers-reduced-motion forces Low", () => {
    expect(effectiveQuality(null, false)).toBe("balanced");
    expect(effectiveQuality("high", false)).toBe("high");
    expect(effectiveQuality("low", false)).toBe("low");
    expect(effectiveQuality("high", true)).toBe("low");
    expect(effectiveQuality(null, true)).toBe("low");
  });
});

describe("responsive budget", () => {
  it("halves the particle count below 900 px", () => {
    expect(NARROW_MAX_WIDTH).toBe(899);
    expect(particleCount("low", false)).toBe(400);
    expect(particleCount("balanced", false)).toBe(1200);
    expect(particleCount("high", false)).toBe(3000);
    expect(particleCount("low", true)).toBe(200);
    expect(particleCount("balanced", true)).toBe(600);
    expect(particleCount("high", true)).toBe(1500);
  });

  it("reserves room for docked side panels only on wide screens", () => {
    expect(sidePanelInset(800)).toBe(0); // overlays below 900 px
    expect(sidePanelInset(1366)).toBeGreaterThan(250);
    expect(sidePanelInset(2560)).toBeLessThanOrEqual(400 + 24);
  });

  it("backs the camera off on narrow aspects so the node ring fits between the panels", () => {
    const wide = cameraDistance(1366, 768);
    const square = cameraDistance(1200, 1100);
    const phone = cameraDistance(390, 844);
    expect(wide).toBeGreaterThanOrEqual(9);
    expect(wide).toBeLessThan(11);
    expect(square).toBeGreaterThan(wide);
    expect(phone).toBeGreaterThan(wide);
    expect(cameraFov(390 / 844)).toBeGreaterThan(cameraFov(1366 / 768));
  });
});
