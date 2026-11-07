import { describe, expect, it } from "vitest";

import { qualityProfile } from "../components/scene/quality";
import { STATE_VISUALS, stateVisuals } from "../components/core/stateVisuals";

describe("qualityProfile", () => {
  it("follows the HD 620 performance budget", () => {
    expect(qualityProfile("low")).toEqual({ particles: 400, dpr: [1, 1], antialias: false });
    expect(qualityProfile("balanced")).toEqual({ particles: 1200, dpr: [1, 1.25], antialias: true });
    expect(qualityProfile("high")).toEqual({ particles: 3000, dpr: [1, 1.75], antialias: true });
  });
});

describe("stateVisuals", () => {
  it("has a look for every JarvisState", () => {
    const states = ["idle", "listening", "processing", "speaking", "tool", "error", "offline"] as const;
    expect(Object.keys(STATE_VISUALS).sort()).toEqual([...states].sort());
    for (const state of states) {
      const look = stateVisuals(state);
      expect(look.color).toMatch(/^#[0-9a-f]{6}$/);
      expect(look.intensity).toBeGreaterThan(0);
      expect(look.rotationSpeed).toBeGreaterThan(0);
    }
  });

  it("uses amber for error and dims offline below every live state", () => {
    expect(stateVisuals("error").color).toBe("#ffb547");
    const offline = stateVisuals("offline");
    for (const state of ["idle", "listening", "processing", "speaking", "tool", "error"] as const) {
      expect(offline.intensity).toBeLessThan(stateVisuals(state).intensity);
      expect(offline.rotationSpeed).toBeLessThanOrEqual(stateVisuals(state).rotationSpeed);
    }
    expect(stateVisuals("processing").rotationSpeed).toBeGreaterThan(stateVisuals("idle").rotationSpeed);
  });
});
