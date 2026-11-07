/**
 * The performance budget for an Intel HD 620 class GPU, as one pure lookup: how many particles to draw
 * and the device-pixel-ratio range the canvas may use. The person picks the level (usePerformanceMode);
 * below 900 px the particle count halves, because a small screen shows less of the field anyway.
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

export const QUALITY_LEVELS: readonly QualityLevel[] = ["low", "balanced", "high"];

export function isQualityLevel(value: unknown): value is QualityLevel {
  return typeof value === "string" && (QUALITY_LEVELS as readonly string[]).includes(value);
}

/** Particles to draw: the level's budget, halved on narrow screens. */
export function particleCount(level: QualityLevel, narrow: boolean): number {
  const count = PROFILES[level].particles;
  return narrow ? count / 2 : count;
}
