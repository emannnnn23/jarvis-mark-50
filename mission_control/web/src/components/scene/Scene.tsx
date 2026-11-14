/**
 * The 3D canvas. How often it draws is decided by useAdaptiveFrameloop: never while the tab is hidden,
 * continuously while JARVIS works or the camera moves, otherwise on demand with a slow idle breath.
 * The device pixel ratio and particle count follow the quality level (halved particles on narrow
 * screens), and the camera rig keeps the node ring framed between the side panels.
 *
 * Also here:
 * - High quality adds a bloom pass, loaded on demand (BloomPass.tsx), so Low and Balanced never fetch it;
 * - while the scene draws continuously, drei's PerformanceMonitor watches the frame rate and asks the
 *   page to drop one quality level after a sustained slow period (quality.ts AUTO_DEGRADE);
 * - if the GPU drops the WebGL context (driver reset, sleep, too many tabs), a visible notice replaces
 *   the frozen picture, and the view restarts on a new context, by itself when the browser restores
 *   one or when the person asks.
 */

import { PerformanceMonitor } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import type { RootState } from "@react-three/fiber";
import { Component, Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { useAdaptiveFrameloop } from "../../hooks/useAdaptiveFrameloop";
import { useUi } from "../../store/ui";
import { JarvisCore } from "../core/JarvisCore";
import { SubsystemNodes } from "../nodes/SubsystemNodes";
import { BASE_DISTANCE, CAMERA_HEIGHT } from "./cameraFit";
import { CameraRig, ParallaxGroup } from "./CameraRig";
import { HoloGrid } from "./HoloGrid";
import { AUTO_DEGRADE, autoDegradeBounds, particleCount, qualityProfile } from "./quality";
import type { QualityLevel } from "./quality";

const BACKGROUND = "#04070b";

// Only evaluated when first rendered, which happens only at High quality.
const BloomPass = lazy(() => import("./BloomPass"));

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

/** The glow is decoration: if its chunk fails to load, the scene carries on without it. */
class OptionalEffect extends Component<{ children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: Error): void {
    console.warn("Mission Control: the High-quality glow could not load:", error.message);
  }

  override render(): ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}

interface Props {
  quality: QualityLevel;
  narrow: boolean;
  /** prefers-reduced-motion: the speech pulse is gentler. */
  calm?: boolean;
  /** Called after a sustained low frame rate: the page drops one quality level. */
  onSlow?: () => void;
}

export function Scene({ quality, narrow, calm = false, onSlow }: Props) {
  const { frameloop, fps } = useAdaptiveFrameloop(quality);
  const profile = qualityProfile(quality);
  const select = useUi((ui) => ui.select);
  const parallax = quality !== "low";
  const [contextLost, setContextLost] = useState(false);
  // A new key mounts a new canvas, so a lost context is replaced by a fresh one.
  const [generation, setGeneration] = useState(0);
  const detachContextListeners = useRef<(() => void) | null>(null);

  const restart = useCallback(() => {
    setContextLost(false);
    setGeneration((value) => value + 1);
  }, []);

  const onCreated = useCallback(({ gl }: RootState) => {
    detachContextListeners.current?.();
    const canvas = gl.domElement;
    const onLost = (event: Event) => {
      event.preventDefault(); // lets the browser offer a restore
      setContextLost(true);
    };
    const onRestored = () => restart();
    canvas.addEventListener("webglcontextlost", onLost);
    canvas.addEventListener("webglcontextrestored", onRestored);
    detachContextListeners.current = () => {
      canvas.removeEventListener("webglcontextlost", onLost);
      canvas.removeEventListener("webglcontextrestored", onRestored);
    };
  }, [restart]);
  useEffect(() => () => detachContextListeners.current?.(), []);

  // Frame rate is only meaningful while drawing continuously ("demand" draws at 15 fps on purpose).
  const monitor = frameloop === "always" && !contextLost && quality !== "low" && onSlow !== undefined;

  return (
    <>
      <Canvas
        key={generation}
        className="scene-canvas"
        dpr={profile.dpr}
        frameloop={contextLost ? "never" : frameloop}
        gl={{ antialias: profile.antialias, alpha: false, stencil: false, powerPreference: "high-performance" }}
        camera={{ fov: 40, near: 0.1, far: 80, position: [0, CAMERA_HEIGHT, BASE_DISTANCE] }}
        onCreated={onCreated}
        onPointerMissed={() => select(null)}
        aria-hidden="true"
      >
        <color attach="background" args={[BACKGROUND]} />
        <fog attach="fog" args={[BACKGROUND, 10, 26]} />
        <CameraRig parallax={parallax} />
        {frameloop === "demand" && <DemandTicker fps={fps} />}
        {monitor && (
          // Remounted per level, so each level is measured afresh before another drop.
          <PerformanceMonitor key={quality} ms={AUTO_DEGRADE.sampleMs} iterations={AUTO_DEGRADE.samples}
            bounds={autoDegradeBounds} onDecline={onSlow} />
        )}
        <HoloGrid />
        <ParallaxGroup enabled={parallax}>
          <JarvisCore particles={particleCount(quality, narrow)} calm={calm} />
          <SubsystemNodes />
        </ParallaxGroup>
        {quality === "high" && (
          <OptionalEffect>
            <Suspense fallback={null}>
              <BloomPass />
            </Suspense>
          </OptionalEffect>
        )}
      </Canvas>
      {contextLost && (
        <div className="scene-fallback scene-lost" role="alert">
          <p>The graphics card reset the 3D view.</p>
          <p>Live status still updates in the panels.</p>
          <p>
            <button type="button" className="button button-small" onClick={restart}>Restart 3D view</button>
          </p>
        </div>
      )}
    </>
  );
}
