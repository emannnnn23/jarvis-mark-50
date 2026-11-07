/**
 * The performance budget for an Intel HD 620 class GPU, as one pure lookup: how many particles to draw
 * and the device-pixel-ratio range the canvas may use. Task 7 adds the user's choice and auto-degrade.
 */

export type QualityLevel = "low" | "balanced" | "high";

export interface QualityProfile {
  particles: 400 | 1200 | 3000;
  dpr: [number, number];
  antialias: boolean;
}

const PROFILES: Record<QualityLevel, QualityProfile> = {
  low: { particles: 400, dpr: [1, 1], antialias: false },
  balanced: { particles: 1200, dpr: [1, 1.25], antialias: true },
  high: { particles: 3000, dpr: [1, 1.75], antialias: true },
};

export function qualityProfile(level: QualityLevel): QualityProfile {
  const profile = PROFILES[level];
  return { ...profile, dpr: [...profile.dpr] as [number, number] };
}

/** Balanced unless the person asked the system for less motion. */
export function defaultQuality(): QualityLevel {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "low" : "balanced";
  } catch {
    return "balanced";
  }
}
