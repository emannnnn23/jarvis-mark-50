/**
 * Says what is wrong with the link to JARVIS and what to do about it. Silent while connected; a short
 * drop never reaches it (the status gate holds "reconnecting" back for a second).
 */

import { useMission } from "../../store/store";
import type { ConnectionStatus } from "../../types/events";

interface Message {
  tone: "info" | "attention";
  title: string;
  body: string;
  retry?: boolean;
}

const MESSAGES: Partial<Record<ConnectionStatus, Message>> = {
  reconnecting: {
    tone: "info",
    title: "Connection lost. Reconnecting.",
    body: "The view resumes where it stopped as soon as JARVIS answers.",
  },
  offline: {
    tone: "info",
    title: "JARVIS has shut down.",
    body: "This page keeps trying. If you start JARVIS again, open the new link it prints in the terminal.",
  },
  auth_failed: {
    tone: "attention",
    title: "Open the link printed in the terminal.",
    body: "This page's access key is no longer valid, usually because JARVIS was restarted.",
  },
  no_token: {
    tone: "attention",
    title: "Open the link printed in the terminal.",
    body: "Mission Control needs the link JARVIS prints at startup; it carries this session's access key.",
  },
  too_many_clients: {
    tone: "attention",
    title: "Too many Mission Control tabs are open.",
    body: "Close one of them, then retry here.",
    retry: true,
  },
};

export function ConnectionBanner({ onRetry }: { onRetry: () => void }) {
  const status = useMission((state) => state.connection);
  const message = MESSAGES[status];
  if (message === undefined) return null;
  return (
    <div className={`banner banner-${message.tone}`} role={message.tone === "attention" ? "alert" : "status"}>
      <div className="banner-text">
        <strong>{message.title}</strong>
        <span>{message.body}</span>
      </div>
      {message.retry === true && (
        <button type="button" className="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
