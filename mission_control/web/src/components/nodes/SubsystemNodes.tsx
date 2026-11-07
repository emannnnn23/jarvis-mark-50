/**
 * The six nodes and their links to the core. Each link is a line from the reactor's surface to its node,
 * bright at the node end in the node's status colour, so health reads at a glance across the room.
 */

import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo } from "react";
import { AdditiveBlending, BufferAttribute, BufferGeometry, Color, LineBasicMaterial, Vector3 } from "three";

import { displayState } from "../../store/reducer";
import { useMission } from "../../store/store";
import type { SubsystemId } from "../../types/events";
import { useDisposable } from "../scene/useDisposable";
import { SubsystemNode } from "./SubsystemNode";
import { NODE_LAYOUT, STATUS_LOOK } from "./layout";
import { labelElements } from "./labelRegistry";

const CORE_GAP = 1.2;
const NODE_GAP = 0.26;
const scratch = new Color();

function buildLinks(): BufferGeometry {
  const positions = new Float32Array(NODE_LAYOUT.length * 6);
  NODE_LAYOUT.forEach(({ position }, index) => {
    const node = new Vector3(...position);
    const direction = node.clone().normalize();
    const start = direction.clone().multiplyScalar(CORE_GAP);
    const end = node.clone().sub(direction.multiplyScalar(NODE_GAP));
    positions.set([start.x, start.y, start.z, end.x, end.y, end.z], index * 6);
  });
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new BufferAttribute(positions, 3));
  geometry.setAttribute("color", new BufferAttribute(new Float32Array(NODE_LAYOUT.length * 6), 3));
  return geometry;
}

/** Label offset below the marker, in CSS pixels. */
const LABEL_DROP = 24;

/** Moves each DOM label under its node's projected position. */
function LabelProjector() {
  const points = useMemo(() => NODE_LAYOUT.map(({ id, position }) => ({ id, point: new Vector3(...position) })), []);
  const projected = useMemo(() => new Vector3(), []);
  // Per element, so a remounted label is positioned at once; compared to our own string because the
  // browser normalises style.transform when read back.
  const written = useMemo(() => new WeakMap<HTMLElement, string>(), []);
  useFrame(({ camera, size }) => {
    for (const { id, point } of points) {
      const element = labelElements.get(id);
      if (element === undefined) continue;
      projected.copy(point).project(camera);
      const x = Math.round((projected.x * 0.5 + 0.5) * size.width);
      const y = Math.round((-projected.y * 0.5 + 0.5) * size.height + LABEL_DROP);
      const transform = `translate3d(${x}px, ${y}px, 0) translateX(-50%)`;
      if (written.get(element) !== transform) {
        element.style.transform = transform;
        element.style.visibility = projected.z < 1 ? "visible" : "hidden";
        written.set(element, transform);
      }
    }
  });
  return null;
}

export function SubsystemNodes() {
  const subsystems = useMission((state) => state.subsystems);
  // With the link down nothing about the subsystems is known: draw them all as "unknown".
  const stale = useMission((state) => displayState(state) === "offline");
  const statusOf = (id: SubsystemId) => (stale ? "unknown" : subsystems[id].status);
  const links = useDisposable(buildLinks, []);
  const linkMaterial = useDisposable(
    () => new LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false, blending: AdditiveBlending }),
    [],
  );

  useEffect(() => {
    const colors = links.getAttribute("color") as BufferAttribute;
    NODE_LAYOUT.forEach(({ id }, index) => {
      const look = STATUS_LOOK[statusOf(id)];
      scratch.set(look.color).multiplyScalar(0.06);
      colors.setXYZ(index * 2, scratch.r, scratch.g, scratch.b);
      scratch.set(look.color).multiplyScalar(0.5 * look.glow);
      colors.setXYZ(index * 2 + 1, scratch.r, scratch.g, scratch.b);
    });
    colors.needsUpdate = true;
  }, [links, subsystems, stale]);

  return (
    <group>
      <lineSegments geometry={links} material={linkMaterial} dispose={null} />
      {NODE_LAYOUT.map(({ id, position }) => (
        <SubsystemNode key={id} position={position} status={statusOf(id)} />
      ))}
      <LabelProjector />
    </group>
  );
}
