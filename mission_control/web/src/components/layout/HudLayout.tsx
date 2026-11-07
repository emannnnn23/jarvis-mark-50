/**
 * The page frame: the 3D scene fills the window and the HUD floats over its edges, leaving the centre
 * (the core) clear. Wide screens dock a column on each side: node details and the system monitor on the
 * left; connection banner, task and event stream on the right. Below 900 px both columns become
 * overlays opened from the top bar, and the bottom centre stays reserved for the command bar dock.
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
  /** Narrow screens: which column is open as an overlay. */
  leftOpen: boolean;
  rightOpen: boolean;
  /** Docked side column width in px (0 on narrow screens). */
  side: number;
  className?: string;
}

export function HudLayout({ scene, labels, top, banner, left, right, center, leftOpen, rightOpen, side, className = "" }: Props) {
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
        {/* Reserved for the command bar (Task 10): docked at the bottom centre on every screen size. */}
        <div className="hud-dock" data-slot="command-bar" />
      </div>
    </div>
  );
}
