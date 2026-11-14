/**
 * The centrepiece: reactor, rings and particles, all driven by one live look that eases toward the
 * current state's target, so a state change reads as the core shifting gear rather than a cut.
 * While JARVIS speaks with exact word timings, the speaking breath is replaced by the speech pulse:
 * the core brightens and swells a little on each word and settles between words.
 */

import { useFrame } from "@react-three/fiber";
import { useMemo } from "react";
import { Color } from "three";

import { displayState } from "../../store/reducer";
import { useSpeechPulse } from "../../hooks/useSpeechPulse";
import { useMission } from "../../store/store";
import { OrbitalRings } from "./OrbitalRings";
import { ParticleField } from "./ParticleField";
import { ReactorSphere } from "./ReactorSphere";
import { STATE_INDEX, stateVisuals } from "./stateVisuals";

/** Mutable per-frame values shared by the core's parts (never React state: it changes every frame). */
export interface CoreLive {
  color: Color;
  intensity: number;
  speed: number;
  stateIndex: number;
  /** Seconds of animation time; frozen while the frameloop is stopped. */
  time: number;
  /** 0..1 speech pulse (eased), 0 unless JARVIS is speaking with word timings. */
  pulse: number;
}

const EASE = 2.6; // 1/s: about 0.4 s to cover most of a change
/** How fast the shown pulse follows the word envelope: quick enough for syllables, no flicker. */
const PULSE_EASE = 28;
/** Extra brightness at a word's peak. */
const PULSE_GAIN = 0.45;
/** Under reduced motion the pulse is a third as strong: a glow on the words, hardly any swell. */
const CALM_SCALE = 0.3;

export function JarvisCore({ particles, calm = false }: { particles: number; calm?: boolean }) {
  const state = useMission(displayState);
  const speech = useSpeechPulse();
  const target = useMemo(() => {
    const look = stateVisuals(state);
    return { ...look, colorValue: new Color(look.color), index: STATE_INDEX[state] };
  }, [state]);
  const live = useMemo<CoreLive>(
    () => ({
      color: new Color(stateVisuals("idle").color), intensity: 0.4, speed: 0.1, stateIndex: 0, time: 0, pulse: 0,
    }),
    [],
  );

  useFrame((_, rawDelta) => {
    const delta = Math.min(rawDelta, 0.1); // a resumed tab must not jump
    const k = 1 - Math.exp(-delta * EASE);
    const kPulse = 1 - Math.exp(-delta * PULSE_EASE);
    live.time += delta;
    live.color.lerp(target.colorValue, k);
    const following = state === "speaking" && speech.active;
    const wanted = following ? speech.current * (calm ? CALM_SCALE : 1) : 0;
    live.pulse += (wanted - live.pulse) * kPulse;
    const breathing = following
      ? 1 + PULSE_GAIN * live.pulse
      : target.pulse > 0 ? 1 + 0.1 * Math.sin(live.time * Math.PI * 2 * target.pulse) : 1;
    live.intensity += (target.intensity * breathing - live.intensity) * (following ? kPulse : k);
    live.speed += (target.rotationSpeed - live.speed) * k;
    live.stateIndex = target.index;
  });

  return (
    <group>
      <ReactorSphere live={live} />
      <OrbitalRings live={live} />
      <ParticleField live={live} count={particles} />
    </group>
  );
}
