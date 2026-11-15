/** The reactor: a fresnel glass shell, a faint lattice just outside it, and a soft bright heart. */

import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import { AdditiveBlending, IcosahedronGeometry, MeshBasicMaterial } from "three";
import type { Group } from "three";

import { useDisposable } from "../scene/useDisposable";
import type { CoreLive } from "./JarvisCore";
import { createGlowMaterial, createHoloMaterial } from "./materials/holoMaterial";

export function ReactorSphere({ live }: { live: CoreLive }) {
  const group = useRef<Group>(null);
  const shellGeometry = useDisposable(() => new IcosahedronGeometry(1, 3), []);
  const latticeGeometry = useDisposable(() => new IcosahedronGeometry(1.08, 2), []);
  const heartGeometry = useDisposable(() => new IcosahedronGeometry(0.42, 3), []);
  const shell = useDisposable(() => createHoloMaterial("#6fe7f5"), []);
  const lattice = useDisposable(
    () => new MeshBasicMaterial({ wireframe: true, transparent: true, depthWrite: false, blending: AdditiveBlending }),
    [],
  );
  const heart = useDisposable(() => createGlowMaterial("#6fe7f5"), []);

  useFrame((_, delta) => {
    shell.uniforms.uTime.value = live.time;
    shell.uniforms.uColor.value.copy(live.color);
    shell.uniforms.uIntensity.value = live.intensity;
    lattice.color.copy(live.color);
    lattice.opacity = 0.025 + 0.05 * live.intensity;
    heart.uniforms.uColor.value.copy(live.color);
    heart.uniforms.uIntensity.value = 0.3 + 0.7 * live.intensity + 0.35 * live.pulse;
    if (group.current) {
      group.current.scale.setScalar(1 + 0.05 * live.pulse); // a word swells the reactor slightly
      group.current.rotation.y += Math.min(delta, 0.1) * live.speed * 0.5;
      group.current.rotation.x = Math.sin(live.time * 0.15) * 0.08;
    }
  });

  return (
    <group ref={group}>
      <mesh geometry={shellGeometry} material={shell} dispose={null} />
      <mesh geometry={latticeGeometry} material={lattice} dispose={null} />
      <mesh geometry={heartGeometry} material={heart} dispose={null} />
    </group>
  );
}
