/**
 * The strip across the top: who is running (JARVIS, model, permissions), what it is doing right now,
 * and whether this page is live.
 */

import { displayState } from "../../store/reducer";
import { useMission } from "../../store/store";
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

export function TopBar() {
  const session = useMission((s) => s.session);
  const connection = useMission((s) => s.connection);
  return (
    <header className="topbar">
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
        <span className={`link link-${connection}`}>
          <span className="link-dot" aria-hidden="true" />
          {LINK_WORD[connection]}
        </span>
      </div>
    </header>
  );
}
