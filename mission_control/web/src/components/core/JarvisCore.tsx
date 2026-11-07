/**
 * The centrepiece: reactor, rings and particles, all driven by one live look that eases toward the
 * current state's target, so a state change reads as the core shifting gear rather than a cut.
 */

import { useFrame } from "@react-three/fiber";
import { useMemo } from "react";
import { Color } from "three";

import { displayState } from "../../store/reducer";
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
}

const EASE = 2.6; // 1/s: about 0.4 s to cover most of a change

export function JarvisCore({ particles }: { particles: number }) {
  const state = useMission(displayState);
  const target = useMemo(() => {
    const look = stateVisuals(state);
    return { ...look, colorValue: new Color(look.color), index: STATE_INDEX[state] };
  }, [state]);
  const live = useMemo<CoreLive>(
    () => ({ color: new Color(stateVisuals("idle").color), intensity: 0.4, speed: 0.1, stateIndex: 0, time: 0 }),
    [],
  );

  useFrame((_, rawDelta) => {
    const delta = Math.min(rawDelta, 0.1); // a resumed tab must not jump
    const k = 1 - Math.exp(-delta * EASE);
    live.time += delta;
    live.color.lerp(target.colorValue, k);
    const breathing = target.pulse > 0 ? 1 + 0.1 * Math.sin(live.time * Math.PI * 2 * target.pulse) : 1;
    live.intensity += (target.intensity * breathing - live.intensity) * k;
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
