/**
 * Opens the Mission Control socket for the page's lifetime and feeds every frame into the store.
 * Status changes pass through the reconnect grace gate, so a drop that heals within a second is invisible.
 */

import { useCallback, useEffect, useRef } from "react";

import { MissionSocket } from "../services/socket";
import { createStatusGate } from "../services/statusGate";
import { useMission } from "../store/store";

function socketUrl(): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}/ws`;
}

export function useMissionSocket(token: string | null): { retry: () => void } {
  const dispatch = useMission((state) => state.dispatch);
  const socket = useRef<MissionSocket | null>(null);

  useEffect(() => {
    if (!token) {
      dispatch({ type: "connection", status: "no_token" });
      return undefined;
    }
    const gate = createStatusGate((status) => dispatch({ type: "connection", status }));
    const current = new MissionSocket(socketUrl(), token, (frame) => dispatch(frame), gate);
    socket.current = current;
    current.connect();
    return () => {
      gate.cancel();
      current.close();
      if (socket.current === current) socket.current = null;
    };
  }, [token, dispatch]);

  const retry = useCallback(() => socket.current?.connect(), []);
  return { retry };
}
