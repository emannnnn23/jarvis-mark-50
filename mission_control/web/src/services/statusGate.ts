/**
 * Holds back "reconnecting" for a moment, so a quick drop and resume (the server dropping a tab that
 * stalled for 3 s, a laptop waking up) does not flash a banner or turn the core offline.
 */

import type { ConnectionStatus } from "../types/events";

export const RECONNECT_GRACE_MS = 1000;

export interface StatusGate {
  (status: ConnectionStatus): void;
  cancel(): void;
}

export function createStatusGate(show: (status: ConnectionStatus) => void, graceMs = RECONNECT_GRACE_MS): StatusGate {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const clear = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  const gate = ((status: ConnectionStatus) => {
    if (status === "reconnecting") {
      if (timer === null) timer = setTimeout(() => {
        timer = null;
        show("reconnecting");
      }, graceMs);
      return;
    }
    clear();
    show(status);
  }) as StatusGate;
  gate.cancel = clear;
  return gate;
}
