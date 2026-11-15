/**
 * The page frame: the 3D scene fills the window and the HUD floats over its edges, leaving the centre
 * (the core) clear. Wide screens dock a column on each side: node details and the system monitor on the
 * left; connection banner, task and event stream on the right. Below 900 px both columns become
 * overlays opened from the top bar, and the bottom centre holds the command bar dock. An open approval
 * sits in the middle column just above the dock, over the scene.
 * If WebGL is unavailable the HUD still works and says why the scene is missing.
 */

import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface BoundaryState {
  failed: boolean;
}

export class SceneBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  override state: BoundaryState = { failed: false };

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true };
  }

  override componentDidCatch(error: Error, _info: ErrorInfo): void {
    console.warn("Mission Control: the 3D view could not start:", error.message);
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <div className="scene-fallback">
          <p>The 3D view could not start: WebGL is off or unsupported in this browser.</p>
          <p>Live status still updates in the panels.</p>
        </div>
      );
    }
    return this.props.children;
  }
}

interface Props {
  scene: ReactNode;
  /** DOM labels anchored to 3D positions; above the scene, below the panels. */
  labels: ReactNode;
  top: ReactNode;
  banner: ReactNode;
  left: ReactNode;
  right: ReactNode;
  /** Controls over the scene's top centre (Reset view). */
  center: ReactNode;
  /** The command bar, in the bottom-centre dock. */
  dock: ReactNode;
  /** The open approval, over the scene's middle just above the dock. */
  approval: ReactNode;
  /** Narrow screens: which column is open as an overlay. */
  leftOpen: boolean;
  rightOpen: boolean;
  /** Docked side column width in px (0 on narrow screens). */
  side: number;
  className?: string;
}

export function HudLayout({
  scene, labels, top, banner, left, right, center, dock, approval, leftOpen, rightOpen, side, className = "",
}: Props) {
  return (
    <div className={`hud ${className}`.trim()} style={{ ["--side" as string]: `${side}px` }}>
      <div className="hud-scene">{scene}</div>
      <div className="hud-vignette" aria-hidden="true" />
      {labels}
      <div className="hud-overlay">
        {top}
        <div className="hud-banner">{banner}</div>
        <div className="hud-center">{center}</div>
        <aside id="hud-left" className="hud-left" data-open={leftOpen} aria-label="Node details and system monitor">
          {left}
        </aside>
        <aside id="hud-right" className="hud-right" data-open={rightOpen} aria-label="Task and event stream">
          {right}
        </aside>
        <div className="hud-approval">{approval}</div>
        {/* The command bar: docked at the bottom centre on every screen size (cameraFit.ts DOCK_HEIGHT). */}
        <div className="hud-dock" data-slot="command-bar" role="region" aria-label="Command bar">{dock}</div>
      </div>
    </div>
  );
}
