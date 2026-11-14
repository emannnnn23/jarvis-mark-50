/**
 * The live speech pulse for the core: a 0..1 envelope that rises with each word JARVIS speaks.
 *
 * Must be used inside the R3F Canvas. The value changes every frame, so it is a ref read in useFrame,
 * never React state. The chunk's arrival time is taken when the store changes (the moment the socket
 * delivered it), and the words are laid out from there plus the player's latency (speechEnvelope.ts).
 */

import { useFrame } from "@react-three/fiber";
import { useEffect, useRef } from "react";

import { pulseAt, speechTimeline } from "../components/core/speechEnvelope";
import type { SpeechTimeline } from "../components/core/speechEnvelope";
import { useMission } from "../store/store";

export interface SpeechPulse {
  /** 0..1: the envelope now. */
  current: number;
  /** True while a part with exact word timings is laid out (the core then follows the words). */
  active: boolean;
}

export function useSpeechPulse(): SpeechPulse {
  const pulse = useRef<SpeechPulse>({ current: 0, active: false });
  const timeline = useRef<SpeechTimeline | null>(null);

  useEffect(
    () =>
      useMission.subscribe((next, previous) => {
        if (next.speech !== previous.speech) {
          timeline.current = speechTimeline(next.speech, performance.now(), Date.now());
        }
      }),
    [],
  );

  useFrame(() => {
    const now = performance.now();
    const current = timeline.current;
    // A part is laid out from its first word to its last: past that, the core goes back to breathing.
    const active = current !== null && now - current.start <= current.end;
    pulse.current.current = active ? pulseAt(current, now) : 0;
    pulse.current.active = active;
  });

  return pulse.current;
}
