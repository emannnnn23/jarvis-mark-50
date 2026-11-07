/**
 * The 3D canvas. How often it draws is decided by useAdaptiveFrameloop: never while the tab is hidden,
 * continuously while JARVIS works or the camera moves, otherwise on demand with a slow idle breath.
 * The device pixel ratio and particle count follow the quality level (halved particles on narrow
 * screens), and the camera rig keeps the node ring framed between the side panels.
 */

import { Canvas, useThree } from "@react-three/fiber";
import { useEffect } from "react";

import { useAdaptiveFrameloop } from "../../hooks/useAdaptiveFrameloop";
import { useUi } from "../../store/ui";
import { JarvisCore } from "../core/JarvisCore";
import { SubsystemNodes } from "../nodes/SubsystemNodes";
import { BASE_DISTANCE, CAMERA_HEIGHT } from "./cameraFit";
import { CameraRig, ParallaxGroup } from "./CameraRig";
import { HoloGrid } from "./HoloGrid";
import { particleCount, qualityProfile } from "./quality";
import type { QualityLevel } from "./quality";

const BACKGROUND = "#04070b";

/** In "demand" mode, asks for a frame `fps` times a second so the idle core still breathes. */
function DemandTicker({ fps }: { fps: number }) {
  const invalidate = useThree((three) => three.invalidate);
  useEffect(() => {
    if (fps <= 0) return undefined;
    const timer = setInterval(() => invalidate(), 1000 / fps);
    return () => clearInterval(timer);
  }, [fps, invalidate]);
  return null;
}

export function Scene({ quality, narrow }: { quality: QualityLevel; narrow: boolean }) {
  const { frameloop, fps } = useAdaptiveFrameloop(quality);
  const profile = qualityProfile(quality);
  const select = useUi((ui) => ui.select);
  const parallax = quality !== "low";
  return (
    <Canvas
      className="scene-canvas"
      dpr={profile.dpr}
      frameloop={frameloop}
      gl={{ antialias: profile.antialias, alpha: false, stencil: false, powerPreference: "high-performance" }}
      camera={{ fov: 40, near: 0.1, far: 80, position: [0, CAMERA_HEIGHT, BASE_DISTANCE] }}
      onPointerMissed={() => select(null)}
    >
      <color attach="background" args={[BACKGROUND]} />
      <fog attach="fog" args={[BACKGROUND, 10, 26]} />
      <CameraRig parallax={parallax} />
      {frameloop === "demand" && <DemandTicker fps={fps} />}
      <HoloGrid />
      <ParallaxGroup enabled={parallax}>
        <JarvisCore particles={particleCount(quality, narrow)} />
        <SubsystemNodes />
      </ParallaxGroup>
    </Canvas>
  );
}
