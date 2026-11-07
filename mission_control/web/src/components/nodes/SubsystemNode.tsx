/**
 * One subsystem node: a slowly turning octahedron with a halo, coloured by status. Its label is DOM
 * (NodeLabels), moved here by the projector in SubsystemNodes. Selection arrives in Task 7.
 */

import { Billboard } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import { AdditiveBlending, MeshBasicMaterial, OctahedronGeometry, RingGeometry } from "three";
import type { Mesh } from "three";

import type { SubsystemStatus } from "../../types/events";
import { useDisposable } from "../scene/useDisposable";
import { STATUS_LOOK } from "./layout";

interface Props {
  position: [number, number, number];
  status: SubsystemStatus;
}

const additive = (opacity: number, wireframe = false) =>
  new MeshBasicMaterial({ transparent: true, depthWrite: false, blending: AdditiveBlending, opacity, wireframe });

export function SubsystemNode({ position, status }: Props) {
  const marker = useRef<Mesh>(null);
  const look = STATUS_LOOK[status];
  const markerGeometry = useDisposable(() => new OctahedronGeometry(0.12, 0), []);
  const haloGeometry = useDisposable(() => new RingGeometry(0.2, 0.212, 56), []);
  const fill = useDisposable(() => additive(0.18), []);
  const wire = useDisposable(() => additive(0.9, true), []);
  const halo = useDisposable(() => additive(0.5), []);

  useEffect(() => {
    for (const material of [fill, wire, halo]) material.color.set(look.color);
    fill.opacity = 0.22 * look.glow;
    wire.opacity = 0.95 * look.glow;
    halo.opacity = 0.55 * look.glow;
  }, [look, fill, wire, halo]);

  useFrame((_, delta) => {
    if (marker.current && status !== "unavailable") marker.current.rotation.y += Math.min(delta, 0.1) * 0.6;
  });

  return (
    <group position={position}>
      <mesh ref={marker} geometry={markerGeometry} material={fill} dispose={null}>
        <mesh geometry={markerGeometry} material={wire} scale={1.02} dispose={null} />
      </mesh>
      <Billboard>
        <mesh geometry={haloGeometry} material={halo} dispose={null} />
      </Billboard>
    </group>
  );
}
