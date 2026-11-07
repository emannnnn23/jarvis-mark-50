/**
 * The camera: orbit and zoom within limits that keep the core in view, a gentle turn toward a selected
 * node, a fly-home on "Reset view", and a subtle pointer parallax that makes the hologram feel physical
 * without moving anything the person must read.
 *
 * Framing comes from cameraFit.ts, which projects the real node ring: the home view keeps every node and
 * label clear of the top bar, the side columns and the command bar dock, and a focus turn is only as
 * strong as that same check allows, so no node ever slides under a panel. While the person has not moved
 * the camera, a resize re-homes it; once they have, the view stays theirs until they reset it.
 *
 * Every movement is reported to cameraActivity, which keeps the frameloop on "always" until the camera
 * has settled; in "demand" mode nothing would otherwise draw the in-between frames.
 */

import { OrbitControls } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import type { ReactNode } from "react";
import { Fog, PerspectiveCamera } from "three";
import type { Group } from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";

import { useUi } from "../../store/ui";
import { applyView, fitView, focusPose, homePose } from "./cameraFit";
import type { Pose } from "./cameraFit";
import { cameraActivity } from "./motion";

/** Polar angle limits (from straight up): never under the floor, never looking straight down. */
const MIN_POLAR = 1.0;
const MAX_POLAR = 1.75;
/** Azimuth limits either side of the front view, so the hexagon stays readable. */
const AZIMUTH_LIMIT = 1.2;
const FLY_EASE = 3.2; // 1/s
/** Fog band relative to the camera's distance from the core (the grid fades out behind the ring). */
const FOG_NEAR = 0.6;
const FOG_FAR = 16.6;

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
  const view = useMemo(() => fitView(width, height), [width, height]);
  const distance = view.distance;

  // Field of view, view offset and home position follow the viewport; the person's own view is kept
  // once they moved it.
  useEffect(() => {
    if (camera instanceof PerspectiveCamera) applyView(camera, width, height, view);
    const control = controls.current;
    if (control === null || useUi.getState().cameraMoved) return;
    const home = homePose(distance);
    camera.position.copy(home.position);
    control.target.copy(home.target);
    control.update();
    invalidate();
  }, [camera, width, height, view, distance, invalidate]);

  // Selecting a node flies toward it; clearing the selection or "Reset view" flies home.
  useEffect(() => {
    if (selected !== null) goal.current = focusPose(view, width, height, selected);
    else if (useUi.getState().cameraMoved || resetRequest > 0) goal.current = homePose(distance);
    cameraActivity.mark();
    invalidate();
    // The view is read at the moment of the request on purpose: a later resize does not restart the flight.
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
