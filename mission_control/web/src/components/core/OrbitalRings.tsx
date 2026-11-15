/** Three orbit rings at different tilts and speeds. Their ticks and travelling arcs make speed legible. */

import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import { TorusGeometry } from "three";
import type { Mesh } from "three";

import { useDisposable } from "../scene/useDisposable";
import type { CoreLive } from "./JarvisCore";
import { createRingMaterial } from "./materials/holoMaterial";

interface RingSpec {
  radius: number;
  tilt: [number, number, number];
  /** Multiplier on the state's rotation speed; the sign sets the direction. */
  speed: number;
  segments: number;
  duty: number;
  sweep: number;
}

const RINGS: RingSpec[] = [
  { radius: 1.45, tilt: [1.15, 0, 0.2], speed: 1.0, segments: 0, duty: 1, sweep: 0.11 },
  { radius: 1.8, tilt: [1.32, 0.35, -0.3], speed: -0.7, segments: 96, duty: 0.55, sweep: 0.07 },
  { radius: 2.2, tilt: [0.95, -0.25, 0.5], speed: 0.45, segments: 24, duty: 0.12, sweep: 0.05 },
];

function Ring({ spec, live }: { spec: RingSpec; live: CoreLive }) {
  const mesh = useRef<Mesh>(null);
  const geometry = useDisposable(() => new TorusGeometry(spec.radius, 0.012, 4, 240), [spec.radius]);
  const material = useDisposable(
    () => createRingMaterial("#6fe7f5", spec.segments, spec.duty, spec.sweep),
    [spec.segments, spec.duty, spec.sweep],
  );

  useFrame((_, delta) => {
    material.uniforms.uTime.value = live.time * (0.4 + live.speed * 3);
    material.uniforms.uColor.value.copy(live.color);
    material.uniforms.uIntensity.value = live.intensity;
    if (mesh.current) mesh.current.rotation.z += Math.min(delta, 0.1) * live.speed * spec.speed;
  });

  return (
    <group rotation={spec.tilt}>
      <mesh ref={mesh} geometry={geometry} material={material} dispose={null} />
    </group>
  );
}

export function OrbitalRings({ live }: { live: CoreLive }) {
  return (
    <group>
      {RINGS.map((spec) => (
        <Ring key={spec.radius} spec={spec} live={live} />
      ))}
    </group>
  );
}
