/**
 * The 3D canvas. It renders only while the tab is visible (frameloop "never" otherwise), caps the
 * device pixel ratio by quality level, and keeps the whole node ring in frame on narrow screens.
 */

import { Canvas, useThree } from "@react-three/fiber";
import { useLayoutEffect } from "react";

import { useDocumentVisible } from "../../hooks/useDocumentVisible";
import { JarvisCore } from "../core/JarvisCore";
import { SubsystemNodes } from "../nodes/SubsystemNodes";
import { HoloGrid } from "./HoloGrid";
import { qualityProfile } from "./quality";
import type { QualityLevel } from "./quality";

const FOV = 40;
const BASE_DISTANCE = 9.4;
const CAMERA_HEIGHT = 0.7;
/** World half-width that must stay visible: the side nodes plus their labels. */
const NEEDED_HALF_WIDTH = 3.9;
const BACKGROUND = "#04070b";

function CameraFit() {
  const camera = useThree((three) => three.camera);
  const aspect = useThree((three) => three.size.width / Math.max(1, three.size.height));
  useLayoutEffect(() => {
    const halfTan = Math.tan((FOV * Math.PI) / 360);
    const distance = Math.max(BASE_DISTANCE, NEEDED_HALF_WIDTH / (halfTan * aspect));
    camera.position.set(0, CAMERA_HEIGHT * (distance / BASE_DISTANCE), distance);
    camera.lookAt(0, -0.05, 0);
    camera.updateProjectionMatrix();
  }, [camera, aspect]);
  return null;
}

export function Scene({ quality }: { quality: QualityLevel }) {
  const visible = useDocumentVisible();
  const profile = qualityProfile(quality);
  return (
    <Canvas
      className="scene-canvas"
      dpr={profile.dpr}
      frameloop={visible ? "always" : "never"}
      gl={{ antialias: profile.antialias, alpha: false, stencil: false, powerPreference: "high-performance" }}
      camera={{ fov: FOV, near: 0.1, far: 60, position: [0, CAMERA_HEIGHT, BASE_DISTANCE] }}
    >
      <color attach="background" args={[BACKGROUND]} />
      <fog attach="fog" args={[BACKGROUND, 10, 26]} />
      <CameraFit />
      <HoloGrid />
      <JarvisCore particles={profile.particles} />
      <SubsystemNodes />
    </Canvas>
  );
}
