/**
 * The camera: orbit and zoom within limits that keep the core in view, a gentle fly-to when a node is
 * selected, a fly-home on "Reset view", and a subtle pointer parallax that makes the hologram feel
 * physical without moving anything the person must read.
 *
 * The home view comes from the viewport (cameraFit.ts), so the node ring always fits between the
 * docked panels. While the person has not moved the camera, a resize re-homes it; once they have, the
 * view stays theirs until they reset it.
 *
 * Every movement is reported to cameraActivity, which keeps the frameloop on "always" until the camera
 * has settled; in "demand" mode nothing would otherwise draw the in-between frames.
 */

import { OrbitControls } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import type { ReactNode } from "react";
import { Fog, PerspectiveCamera, Vector3 } from "three";
import type { Group } from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";

import { useUi } from "../../store/ui";
import { NODE_LAYOUT } from "../nodes/layout";
import { BASE_DISTANCE, CAMERA_HEIGHT, LOOK_AT_Y, cameraDistance, cameraFov } from "./cameraFit";
import { cameraActivity } from "./motion";

/** Polar angle limits (from straight up): never under the floor, never looking straight down. */
const MIN_POLAR = 1.0;
const MAX_POLAR = 1.75;
/** Azimuth limits either side of the front view, so the hexagon stays readable. */
const AZIMUTH_LIMIT = 1.2;
const FLY_EASE = 3.2; // 1/s
const FOCUS_DOLLY = 0.9;
const FOCUS_SHIFT = 0.22;
/** Fog band relative to the camera's distance from the core (the grid fades out behind the ring). */
const FOG_NEAR = 0.6;
const FOG_FAR = 16.6;

interface Pose {
  position: Vector3;
  target: Vector3;
}

function homePose(distance: number): Pose {
  return {
    position: new Vector3(0, CAMERA_HEIGHT * (distance / BASE_DISTANCE), distance),
    target: new Vector3(0, LOOK_AT_Y, 0),
  };
}

/** Move toward the node, keeping the core in frame: the target shifts part of the way, the camera dollies in. */
function focusPose(distance: number, node: readonly [number, number, number]): Pose {
  const target = new Vector3(node[0] * FOCUS_SHIFT, LOOK_AT_Y + node[1] * FOCUS_SHIFT, node[2] * FOCUS_SHIFT);
  const position = new Vector3(
    target.x * 1.1,
    target.y + CAMERA_HEIGHT * (distance / BASE_DISTANCE),
    distance * FOCUS_DOLLY,
  );
  return { position, target };
}

export function CameraRig({ parallax }: { parallax: boolean }) {
  const camera = useThree((three) => three.camera);
  const width = useThree((three) => three.size.width);
  const height = useThree((three) => three.size.height);
  const invalidate = useThree((three) => three.invalidate);
  const controls = useRef<OrbitControlsImpl>(null);
  const goal = useRef<Pose | null>(null);
  const selected = useUi((ui) => ui.selected);
  const resetRequest = useUi((ui) => ui.resetRequest);
  const setCameraMoved = useUi((ui) => ui.setCameraMoved);
  const distance = useMemo(() => cameraDistance(width, height), [width, height]);

  // Field of view and home position follow the viewport; the person's own view is kept once they moved it.
  useEffect(() => {
    if (camera instanceof PerspectiveCamera) {
      camera.fov = cameraFov(width / Math.max(1, height));
      camera.updateProjectionMatrix();
    }
    const control = controls.current;
    if (control === null || useUi.getState().cameraMoved) return;
    const home = homePose(distance);
    camera.position.copy(home.position);
    control.target.copy(home.target);
    control.update();
    invalidate();
  }, [camera, width, height, distance, invalidate]);

  // Selecting a node flies toward it; clearing the selection or "Reset view" flies home.
  useEffect(() => {
    const node = selected === null ? null : NODE_LAYOUT.find((placement) => placement.id === selected) ?? null;
    if (node !== null) goal.current = focusPose(distance, node.position);
    else if (useUi.getState().cameraMoved || resetRequest > 0) goal.current = homePose(distance);
    cameraActivity.mark();
    invalidate();
    // distance is read at the moment of the request on purpose: a later resize does not restart the flight.
  }, [selected, resetRequest]); // eslint-disable-line react-hooks/exhaustive-deps

  useFrame(({ scene }, rawDelta) => {
    // The fog starts just behind the core wherever the camera is, so zooming out never greys it.
    if (scene.fog instanceof Fog) {
      const reach = camera.position.length();
      scene.fog.near = reach + FOG_NEAR;
      scene.fog.far = reach + FOG_FAR;
    }
    const control = controls.current;
    const pose = goal.current;
    if (control === null || pose === null) return;
    const k = 1 - Math.exp(-Math.min(rawDelta, 0.1) * FLY_EASE);
    camera.position.lerp(pose.position, k);
    control.target.lerp(pose.target, k);
    cameraActivity.mark();
    if (camera.position.distanceTo(pose.position) < 0.01 && control.target.distanceTo(pose.target) < 0.01) {
      camera.position.copy(pose.position);
      control.target.copy(pose.target);
      goal.current = null;
    }
  });

  return (
    <>
      <OrbitControls
        ref={controls}
        makeDefault
        enablePan={false}
        enableDamping
        dampingFactor={0.08}
        rotateSpeed={0.55}
        zoomSpeed={0.7}
        minDistance={distance * 0.55}
        maxDistance={distance * 1.6}
        minPolarAngle={MIN_POLAR}
        maxPolarAngle={MAX_POLAR}
        minAzimuthAngle={-AZIMUTH_LIMIT}
        maxAzimuthAngle={AZIMUTH_LIMIT}
        onStart={() => {
          goal.current = null; // the person's hand beats any flight in progress
          cameraActivity.begin();
          setCameraMoved(true);
        }}
        onEnd={() => cameraActivity.end()}
        onChange={() => cameraActivity.mark()}
      />
      {parallax && <PointerWake />}
    </>
  );
}

/** Pointer moves over the canvas wake the frameloop briefly, so the parallax eases at full rate. */
function PointerWake() {
  const element = useThree((three) => three.gl.domElement);
  useEffect(() => {
    const wake = () => cameraActivity.mark();
    element.addEventListener("pointermove", wake, { passive: true });
    return () => element.removeEventListener("pointermove", wake);
  }, [element]);
  return null;
}

const PARALLAX_YAW = 0.045;
const PARALLAX_PITCH = 0.025;
const PARALLAX_EASE = 2.4;

/** Tilts its children a little toward the pointer. Off in Low quality and with reduced motion. */
export function ParallaxGroup({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const group = useRef<Group>(null);
  useFrame(({ pointer }, rawDelta) => {
    const node = group.current;
    if (node === null) return;
    const k = 1 - Math.exp(-Math.min(rawDelta, 0.1) * PARALLAX_EASE);
    const yaw = enabled ? pointer.x * PARALLAX_YAW : 0;
    const pitch = enabled ? -pointer.y * PARALLAX_PITCH : 0;
    node.rotation.y += (yaw - node.rotation.y) * k;
    node.rotation.x += (pitch - node.rotation.x) * k;
  });
  return <group ref={group}>{children}</group>;
}
