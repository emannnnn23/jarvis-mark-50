/**
 * The page frame: the 3D scene fills the window and the HUD floats over its edges, leaving the
 * centre (the core) clear. If WebGL is unavailable the HUD still works and says why the scene is missing.
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
  className?: string;
}

export function HudLayout({ scene, labels, top, banner, left, className = "" }: Props) {
  return (
    <div className={`hud ${className}`.trim()}>
      <div className="hud-scene">{scene}</div>
      <div className="hud-vignette" aria-hidden="true" />
      {labels}
      <div className="hud-overlay">
        {top}
        <div className="hud-banner">{banner}</div>
        <aside className="hud-left" aria-label="System monitor">
          {left}
        </aside>
      </div>
    </div>
  );
}
