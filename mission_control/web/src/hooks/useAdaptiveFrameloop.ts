/**
 * Renders only as often as the picture changes, because an Intel HD 620 cannot afford 60 fps of an idle
 * core next to everything else the laptop is doing.
 *
 * - "never" while the tab is hidden;
 * - "always" while JARVIS is working (listening, processing, speaking, a tool, an error hold), while the
 *   person moves the camera, and for 1.5 s after any streamed event (so packets and pulses play out);
 * - "demand" otherwise, with the idle core breathing at 15 fps (not at all in Low quality).
 */

import { useEffect, useState } from "react";

import { useDocumentVisible } from "./useDocumentVisible";
import { displayState } from "../store/reducer";
import { useMission } from "../store/store";
import { cameraActivity } from "../components/scene/motion";
import type { QualityLevel } from "../components/scene/quality";
import type { JarvisState } from "../types/events";

export type Frameloop = "always" | "demand" | "never";

export const EVENT_ACTIVE_MS = 1500;
export const DEMAND_FPS = 15;
const ACTIVE_STATES: ReadonlySet<JarvisState> = new Set(["listening", "processing", "speaking", "tool", "error"]);
/** How often the hook re-checks the time-based rules (the end of the event window, camera idle). */
const RECHECK_MS = 250;

export interface FrameloopInput {
  hidden: boolean;
  state: JarvisState;
  interacting: boolean;
  /** performance.now() of the latest streamed event, or null. */
  lastEventAt: number | null;
  now: number;
  quality: QualityLevel;
}

export function frameloopFor({ hidden, state, interacting, lastEventAt, now }: FrameloopInput): Frameloop {
  if (hidden) return "never";
  if (ACTIVE_STATES.has(state) || interacting) return "always";
  if (lastEventAt !== null && now - lastEventAt < EVENT_ACTIVE_MS) return "always";
  return "demand";
}

/** Redraws per second while on demand: the idle breathing rate. */
export function demandFps(quality: QualityLevel): number {
  return quality === "low" ? 0 : DEMAND_FPS;
}

export function useAdaptiveFrameloop(quality: QualityLevel): { frameloop: Frameloop; fps: number } {
  const visible = useDocumentVisible();
  const state = useMission(displayState);
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => performance.now());

  // A new streamed event (the log changes; meters do not touch it) opens the 1.5 s window.
  useEffect(
    () =>
      useMission.subscribe((next, previous) => {
        if (next.log !== previous.log) {
          const at = performance.now();
          setLastEventAt(at);
          setNow(at);
        }
      }),
    [],
  );

  const interacting = cameraActivity.isActive(now);
  const decided = frameloopFor({ hidden: !visible, state, interacting, lastEventAt, now, quality });

  // While "always" is held only by time (an event window, the camera settling), re-check until it lapses.
  // Camera activity starting is pushed by the rig through cameraActivity.subscribe.
  useEffect(() => cameraActivity.subscribe(() => setNow(performance.now())), []);
  useEffect(() => {
    if (decided !== "always" || ACTIVE_STATES.has(state)) return undefined;
    const timer = setInterval(() => setNow(performance.now()), RECHECK_MS);
    return () => clearInterval(timer);
  }, [decided, state]);

  return { frameloop: decided, fps: demandFps(quality) };
}
