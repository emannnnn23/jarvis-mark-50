/**
 * The six nodes, their links to the core (DataLink) and the projector that keeps the DOM labels under
 * them. Nodes are selectable in 3D (click or tap) and through their labels, which are buttons.
 */

import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import { Vector3 } from "three";
import type { Object3D } from "three";

import { displayState } from "../../store/reducer";
import { useMission } from "../../store/store";
import { useUi } from "../../store/ui";
import type { SubsystemId } from "../../types/events";
import { DataLinks } from "./DataLink";
import { SubsystemNode } from "./SubsystemNode";
import { NODE_LAYOUT } from "./layout";
import { labelElements } from "./labelRegistry";

/** Label offset below the marker, in CSS pixels. */
const LABEL_DROP = 30;

/**
 * Moves each DOM label under its node's projected position. It sits inside the same parallax group as
 * the nodes and projects through that group's transform, so labels never drift from their markers.
 */
function LabelProjector() {
  const anchor = useRef<Object3D>(null);
  const points = useMemo(() => NODE_LAYOUT.map(({ id, position }) => ({ id, point: new Vector3(...position) })), []);
  const projected = useMemo(() => new Vector3(), []);
  // Per element, so a remounted label is positioned at once; compared to our own string because the
  // browser normalises style.transform when read back.
  const written = useMemo(() => new WeakMap<HTMLElement, string>(), []);
  useFrame(({ camera, size }) => {
    const frame = anchor.current;
    if (frame === null) return;
    frame.updateWorldMatrix(true, false);
    for (const { id, point } of points) {
      const element = labelElements.get(id);
      if (element === undefined) continue;
      projected.copy(point).applyMatrix4(frame.matrixWorld).project(camera);
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
  return <object3D ref={anchor} />;
}

export function SubsystemNodes() {
  const subsystems = useMission((state) => state.subsystems);
  // With the link down nothing about the subsystems is known: draw them all as "unknown".
  const stale = useMission((state) => displayState(state) === "offline");
  const selected = useUi((ui) => ui.selected);
  const hovered = useUi((ui) => ui.hovered);
  const statusOf = (id: SubsystemId) => (stale ? "unknown" : subsystems[id].status);

  return (
    <group>
      <DataLinks statusOf={statusOf} />
      {NODE_LAYOUT.map(({ id, position }) => (
        <SubsystemNode key={id} id={id} position={position} status={statusOf(id)}
          emphasis={selected === id ? "selected" : hovered === id ? "hovered" : "none"} />
      ))}
      <LabelProjector />
    </group>
  );
}
