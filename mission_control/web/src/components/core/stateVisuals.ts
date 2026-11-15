/**
 * How each JarvisState looks on the core. One table, so the reactor, rings and particles always agree.
 *
 * Cyan is the resting holographic colour; electric blue means JARVIS is working (thinking or running a
 * tool); amber is kept for the state that needs a person's attention (error); offline is a dim grey-blue
 * that barely moves, so a dead link can never be mistaken for a quiet one.
 */

import type { JarvisState } from "../../types/events";

export interface StateVisual {
  /** Radians per second for the rings; particles and the reactor scale from it. */
  rotationSpeed: number;
  color: string;
  /** 0..1 overall brightness. */
  intensity: number;
  /** Breathing rate in Hz (0 = steady). */
  pulse: number;
}

export const STATE_VISUALS: Record<JarvisState, StateVisual> = {
  idle: { rotationSpeed: 0.12, color: "#6fe7f5", intensity: 0.55, pulse: 0.18 },
  listening: { rotationSpeed: 0.2, color: "#9af4ff", intensity: 0.8, pulse: 0.6 },
  processing: { rotationSpeed: 0.55, color: "#2e7cf6", intensity: 1.0, pulse: 1.1 },
  speaking: { rotationSpeed: 0.28, color: "#c4f7ff", intensity: 0.9, pulse: 1.6 },
  tool: { rotationSpeed: 0.7, color: "#4aa8ff", intensity: 0.95, pulse: 0.9 },
  error: { rotationSpeed: 0.16, color: "#ffb547", intensity: 0.85, pulse: 0.5 },
  offline: { rotationSpeed: 0.03, color: "#3a4a57", intensity: 0.18, pulse: 0 },
};

/** Stable index per state, for shaders (uState). */
export const STATE_INDEX: Record<JarvisState, number> = {
  idle: 0,
  listening: 1,
  processing: 2,
  speaking: 3,
  tool: 4,
  error: 5,
  offline: 6,
};

export function stateVisuals(state: JarvisState): StateVisual {
  return STATE_VISUALS[state];
}
