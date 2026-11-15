/**
 * One subsystem node: a slowly turning octahedron with a halo, coloured by status. Hovering brightens
 * it; selecting it widens the halo and opens its detail panel. A larger invisible sphere takes the
 * pointer, so a 0.12-unit marker is still easy to hit with a mouse or a finger.
 */

import { Billboard } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import type { ThreeEvent } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import { AdditiveBlending, MeshBasicMaterial, OctahedronGeometry, RingGeometry, SphereGeometry } from "three";
import type { Mesh } from "three";

import { useUi } from "../../store/ui";
import type { SubsystemId, SubsystemStatus } from "../../types/events";
import { useDisposable } from "../scene/useDisposable";
import { STATUS_LOOK } from "./layout";

export type NodeEmphasis = "none" | "hovered" | "selected";

interface Props {
  id: SubsystemId;
  position: [number, number, number];
  status: SubsystemStatus;
  emphasis: NodeEmphasis;
}

const additive = (opacity: number, wireframe = false) =>
  new MeshBasicMaterial({ transparent: true, depthWrite: false, blending: AdditiveBlending, opacity, wireframe });

const HALO_SCALE: Record<NodeEmphasis, number> = { none: 1, hovered: 1.12, selected: 1.28 };
const HALO_BOOST: Record<NodeEmphasis, number> = { none: 1, hovered: 1.35, selected: 1.6 };
const EASE = 9;

export function SubsystemNode({ id, position, status, emphasis }: Props) {
  const marker = useRef<Mesh>(null);
  const haloMesh = useRef<Mesh>(null);
  const select = useUi((ui) => ui.select);
  const hover = useUi((ui) => ui.hover);
  const look = STATUS_LOOK[status];
  const markerGeometry = useDisposable(() => new OctahedronGeometry(0.12, 0), []);
  const haloGeometry = useDisposable(() => new RingGeometry(0.2, 0.212, 56), []);
  const hitGeometry = useDisposable(() => new SphereGeometry(0.34, 12, 8), []);
  const fill = useDisposable(() => additive(0.18), []);
  const wire = useDisposable(() => additive(0.9, true), []);
  const halo = useDisposable(() => additive(0.5), []);
  // Never drawn (visible: false skips rendering) but still hit by the raycaster.
  const hit = useDisposable(() => new MeshBasicMaterial({ visible: false }), []);

  useEffect(() => {
    for (const material of [fill, wire, halo]) material.color.set(look.color);
    fill.opacity = 0.22 * look.glow;
    wire.opacity = 0.95 * look.glow;
  }, [look, fill, wire, halo]);

  useFrame((_, rawDelta) => {
    const delta = Math.min(rawDelta, 0.1);
    if (marker.current && status !== "unavailable") marker.current.rotation.y += delta * 0.6;
    const ring = haloMesh.current;
    if (ring !== null) {
      const k = 1 - Math.exp(-delta * EASE);
      const scale = ring.scale.x + (HALO_SCALE[emphasis] - ring.scale.x) * k;
      ring.scale.setScalar(scale);
      halo.opacity += (Math.min(1, 0.55 * look.glow * HALO_BOOST[emphasis]) - halo.opacity) * k;
    }
  });

  const onOver = (event: ThreeEvent<PointerEvent>) => {
    event.stopPropagation();
    hover(id);
    document.body.style.cursor = "pointer";
  };
  const onOut = () => {
    if (useUi.getState().hovered === id) hover(null);
    document.body.style.cursor = "";
  };
  const onClick = (event: ThreeEvent<MouseEvent>) => {
    event.stopPropagation();
    select(useUi.getState().selected === id ? null : id);
  };

  useEffect(() => () => {
    document.body.style.cursor = "";
  }, []);

  return (
    <group position={position}>
      <mesh ref={marker} geometry={markerGeometry} material={fill} dispose={null}>
        <mesh geometry={markerGeometry} material={wire} scale={1.02} dispose={null} />
      </mesh>
      <Billboard>
        <mesh ref={haloMesh} geometry={haloGeometry} material={halo} dispose={null} />
      </Billboard>
      <mesh geometry={hitGeometry} material={hit} dispose={null} onPointerOver={onOver} onPointerOut={onOut} onClick={onClick} />
    </group>
  );
}
