/**
 * The strip across the top: who is running (JARVIS, model, permissions), what it is doing right now,
 * the view settings (quality), whether this page is live, and on narrow screens the buttons that open
 * the side panels as overlays.
 */

import { useEffect, useRef, useState } from "react";

import type { PerformanceMode } from "../../hooks/usePerformanceMode";
import { displayState } from "../../store/reducer";
import { useMission } from "../../store/store";
import { useUi } from "../../store/ui";
import { QUALITY_LEVELS } from "../scene/quality";
import type { QualityLevel } from "../scene/quality";
import type { ConnectionStatus, JarvisState } from "../../types/events";
import { formatClock } from "../panels/format";

const STATE_WORD: Record<JarvisState, string> = {
  idle: "Idle",
  listening: "Listening",
  processing: "Processing",
  speaking: "Speaking",
  tool: "Running a tool",
  error: "Error",
  offline: "Offline",
};

const LINK_WORD: Record<ConnectionStatus, string> = {
  connecting: "Connecting",
  open: "Live",
  reconnecting: "Reconnecting",
  offline: "JARVIS offline",
  auth_failed: "Access expired",
  too_many_clients: "Tab limit reached",
  no_token: "No access key",
};

function StateReadout() {
  const state = useMission(displayState);
  const jarvis = useMission((s) => s.jarvis);
  const live = state !== "offline";
  const detail = live ? jarvis?.detail ?? null : null;
  const awaiting = state === "tool" && detail === "awaiting approval";
  return (
    <div className={`state-readout state-${awaiting ? "attention" : state}`} aria-live="polite">
      <span className="state-word">{awaiting ? "Waiting for approval" : STATE_WORD[state]}</span>
      {detail !== null && !awaiting && <span className="state-detail">{detail}</span>}
      {live && jarvis !== null && <span className="state-since">since {formatClock(jarvis.since)}</span>}
    </div>
  );
}

const QUALITY_TEXT: Record<QualityLevel, { name: string; note: string }> = {
  low: { name: "Low", note: "Fewest particles, no glass blur, still when idle" },
  balanced: { name: "Balanced", note: "Smooth on integrated graphics" },
  high: { name: "High", note: "Most particles, the sharpest image and a soft glow" },
};

/** The "View" menu: the quality level, remembered on this computer. */
function ViewMenu({ performance }: { performance: PerformanceMode }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onPointer = (event: PointerEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) setOpen(false);
    };
    // Capture phase, and mark the key handled: Escape closes this menu only, not the node details too.
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);
  return (
    <div className="view-menu" ref={root}>
      <button type="button" className="button button-small" aria-expanded={open} aria-controls="view-menu-panel"
        onClick={() => setOpen((value) => !value)}>
        Quality: {QUALITY_TEXT[performance.quality].name}
      </button>
      {open && (
        <div id="view-menu-panel" className="panel view-menu-panel">
          <fieldset className="quality-choice" disabled={performance.forcedLow}>
            <legend>Rendering quality</legend>
            {QUALITY_LEVELS.map((level) => (
              <label key={level} className="quality-option">
                <input type="radio" name="quality" value={level} checked={performance.quality === level}
                  onChange={() => performance.setQuality(level)} />
                <span className="quality-name">{QUALITY_TEXT[level].name}</span>
                <span className="quality-note">{QUALITY_TEXT[level].note}</span>
              </label>
            ))}
          </fieldset>
          {performance.forcedLow && (
            <p className="panel-note">Your system asks for reduced motion, so Mission Control stays on Low.</p>
          )}
          {performance.degradedTo !== null && (
            <p className="panel-note" role="status">
              The scene was running slowly, so it dropped to {QUALITY_TEXT[performance.degradedTo].name} for this
              page. Pick a level to try it again.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** Narrow screens only: open the side columns as overlays. */
function OverlayToggles() {
  const overlay = useUi((ui) => ui.overlay);
  const toggle = useUi((ui) => ui.toggleOverlay);
  return (
    <div className="overlay-toggles">
      <button type="button" className="button button-small" aria-expanded={overlay === "system"} aria-controls="hud-left"
        onClick={() => toggle("system")}>System</button>
      <button type="button" className="button button-small" aria-expanded={overlay === "events"} aria-controls="hud-right"
        onClick={() => toggle("events")}>Events</button>
    </div>
  );
}

export function TopBar({ performance, narrow }: { performance: PerformanceMode; narrow: boolean }) {
  const session = useMission((s) => s.session);
  const connection = useMission((s) => s.connection);
  return (
    <header className="topbar" aria-label="JARVIS status and view settings">
      <div className="brand">
        <span className="brand-name">JARVIS</span>
        <span className="brand-mark">Mark 50</span>
        {session !== null && <span className="brand-version">v{session.version}</span>}
      </div>
      <StateReadout />
      <div className="session">
        {session !== null && (
          <dl className="session-facts">
            <dt>Model</dt>
            <dd>{session.model}</dd>
            <dt>Permissions</dt>
            <dd>{session.permission_mode}</dd>
            {session.developer_mode && (
              <>
                <dt>Mode</dt>
                <dd>Developer</dd>
              </>
            )}
          </dl>
        )}
        {narrow && <OverlayToggles />}
        <ViewMenu performance={performance} />
        <span className={`link link-${connection}`}>
          <span className="link-dot" aria-hidden="true" />
          {LINK_WORD[connection]}
        </span>
      </div>
    </header>
  );
}
