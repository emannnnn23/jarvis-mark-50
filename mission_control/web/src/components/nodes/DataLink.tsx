/**
 * The links from the core to the six nodes, and the data packets that travel along them.
 *
 * Each link is a line from the reactor's surface to its node, bright at the node end in the node's
 * status colour, so health reads at a glance. When real work crosses a link (packets.ts decides which
 * events count) a packet runs along it and the link brightens for a moment; the VOICE link also follows
 * the live microphone level. All packets are one InstancedMesh of 32, so a burst costs one draw call.
 */

import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import {
  AdditiveBlending,
  BufferAttribute,
  BufferGeometry,
  Color,
  IcosahedronGeometry,
  LineBasicMaterial,
  Matrix4,
  MeshBasicMaterial,
  Quaternion,
  Vector3,
} from "three";
import type { InstancedMesh } from "three";

import { useMission } from "../../store/store";
import type { SubsystemId, SubsystemStatus } from "../../types/events";
import { useDisposable } from "../scene/useDisposable";
import { NODE_LAYOUT, STATUS_LOOK } from "./layout";
import { PACKET_LIMIT, PACKET_TRAVEL_S, PULSE_S, lastPacketAge, launchPackets, packetSlots, packetsFor } from "./packets";

const CORE_GAP = 1.2;
const NODE_GAP = 0.26;
/** Events older than this (a replay after reconnecting) do not launch packets: they are history. */
const LIVE_EVENT_MS = 3000;
/** voice.level readings older than this no longer light the VOICE link. */
const LEVEL_FRESH_MS = 1000;
const PACKET_COLOR = new Color("#c4f7ff");
const PACKET_ATTENTION = new Color("#ffb547");

interface LinkEnds {
  id: SubsystemId;
  start: Vector3;
  end: Vector3;
}

/** From the reactor's surface to just short of the node, for every node. */
export function linkEnds(): LinkEnds[] {
  return NODE_LAYOUT.map(({ id, position }) => {
    const node = new Vector3(...position);
    const direction = node.clone().normalize();
    return { id, start: direction.clone().multiplyScalar(CORE_GAP), end: node.clone().sub(direction.multiplyScalar(NODE_GAP)) };
  });
}

function buildLinks(ends: LinkEnds[]): BufferGeometry {
  const positions = new Float32Array(ends.length * 6);
  ends.forEach(({ start, end }, index) => positions.set([start.x, start.y, start.z, end.x, end.y, end.z], index * 6));
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new BufferAttribute(positions, 3));
  geometry.setAttribute("color", new BufferAttribute(new Float32Array(ends.length * 6), 3));
  return geometry;
}

/** Launches packets for each new live event the store receives. */
function usePacketFeed() {
  const invalidate = useThree((three) => three.invalidate);
  useEffect(() => {
    let handled = useMission.getState().lastSeq;
    return useMission.subscribe((next, previous) => {
      if (next.log === previous.log) return;
      const now = Date.now();
      let launched = false;
      for (let index = next.log.length - 1; index >= 0; index -= 1) {
        const event = next.log[index]!;
        if (event.seq <= handled) break;
        if (now - event.timestamp > LIVE_EVENT_MS) continue;
        const specs = packetsFor(event);
        if (specs.length) {
          launchPackets(specs);
          launched = true;
        }
      }
      handled = Math.max(handled, next.lastSeq);
      if (launched) invalidate();
    });
  }, [invalidate]);
}

const ease = (t: number) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);

export function DataLinks({ statusOf }: { statusOf: (id: SubsystemId) => SubsystemStatus }) {
  const ends = useMemo(linkEnds, []);
  const links = useDisposable(() => buildLinks(ends), [ends]);
  const linkMaterial = useDisposable(
    () => new LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false, blending: AdditiveBlending }),
    [],
  );
  const packetGeometry = useDisposable(() => new IcosahedronGeometry(0.045, 1), []);
  const packetMaterial = useDisposable(
    () => new MeshBasicMaterial({ transparent: true, depthWrite: false, blending: AdditiveBlending, toneMapped: false }),
    [],
  );
  const packets = useRef<InstancedMesh>(null);
  const scratch = useMemo(
    () => ({ color: new Color(), matrix: new Matrix4(), position: new Vector3(), scale: new Vector3(), rotation: new Quaternion() }),
    [],
  );
  const pulses = useRef(new Map<SubsystemId, number>());
  const looks = ends.map(({ id }) => STATUS_LOOK[statusOf(id)]);
  const looksKey = looks.map((look) => look.color).join();
  const dirty = useRef(true);
  // Instances start as identity matrices (32 packets at the origin): force the first upload to hide them.
  const packetsDrawn = useRef(PACKET_LIMIT);
  useEffect(() => {
    dirty.current = true;
  }, [looksKey]);
  usePacketFeed();

  useFrame(() => {
    const now = performance.now() / 1000;
    // Link brightness: the status glow plus any recent packet's pulse (and the live mic level on VOICE).
    const level = useMission.getState().voiceLevel;
    const voiceLevel = level !== null && Date.now() - level.at < LEVEL_FRESH_MS ? level.rms : 0;
    let changed = dirty.current;
    ends.forEach(({ id }) => {
      const age = lastPacketAge(id, now);
      let pulse = age >= 0 && age < PACKET_TRAVEL_S + PULSE_S ? 1 - Math.max(0, age - PACKET_TRAVEL_S) / PULSE_S : 0;
      if (id === "voice") pulse = Math.max(pulse, Math.min(1, voiceLevel * 2.5));
      if (pulses.current.get(id) !== pulse) {
        pulses.current.set(id, pulse);
        changed = true;
      }
    });
    if (changed) {
      dirty.current = false;
      const colors = links.getAttribute("color") as BufferAttribute;
      ends.forEach(({ id }, index) => {
        const look = looks[index]!;
        const pulse = pulses.current.get(id) ?? 0;
        scratch.color.set(look.color).multiplyScalar(0.06 + 0.35 * pulse);
        colors.setXYZ(index * 2, scratch.color.r, scratch.color.g, scratch.color.b);
        scratch.color.set(look.color).multiplyScalar(0.5 * look.glow + 0.6 * pulse);
        colors.setXYZ(index * 2 + 1, scratch.color.r, scratch.color.g, scratch.color.b);
      });
      colors.needsUpdate = true;
    }

    // Packets: travel along the link with an ease, swelling in the middle of the trip.
    const mesh = packets.current;
    if (mesh === null) return;
    let active = 0;
    packetSlots.forEach((slot, index) => {
      const t = (now - slot.startedAt) / PACKET_TRAVEL_S;
      const link = ends.find((candidate) => candidate.id === slot.node)!;
      if (t >= 0 && t < 1) {
        const along = ease(slot.direction === "out" ? t : 1 - t);
        scratch.position.lerpVectors(link.start, link.end, along);
        scratch.scale.setScalar(0.55 + 0.75 * Math.sin(Math.PI * t));
        active += 1;
      } else {
        scratch.scale.setScalar(0);
      }
      scratch.matrix.compose(scratch.position, scratch.rotation, scratch.scale);
      mesh.setMatrixAt(index, scratch.matrix);
      mesh.setColorAt(index, slot.tone === "attention" ? PACKET_ATTENTION : PACKET_COLOR);
    });
    if (active > 0 || packetsDrawn.current > 0) {
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
    packetsDrawn.current = active;
  });

  return (
    <group>
      <lineSegments geometry={links} material={linkMaterial} dispose={null} />
      <instancedMesh ref={packets} args={[packetGeometry, packetMaterial, PACKET_LIMIT]} frustumCulled={false} dispose={null} />
    </group>
  );
}
