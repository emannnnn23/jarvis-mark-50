import { useEffect } from "react";

import { Scene } from "./components/scene/Scene";
import { sidePanelWidth } from "./components/scene/cameraFit";
import { CommandBar } from "./components/layout/CommandBar";
import { HudLayout, SceneBoundary } from "./components/layout/HudLayout";
import { TopBar } from "./components/layout/TopBar";
import { NodeDetailPanel } from "./components/nodes/NodeDetailPanel";
import { NodeLabels } from "./components/nodes/NodeLabels";
import { ConfirmationPanel } from "./components/panels/ConfirmationPanel";
import { ConnectionBanner } from "./components/panels/ConnectionBanner";
import { EventStreamPanel } from "./components/panels/EventStreamPanel";
import { ReplyPanel } from "./components/panels/ReplyPanel";
import { SystemMonitorPanel } from "./components/panels/SystemMonitorPanel";
import { TaskPanel } from "./components/panels/TaskPanel";
import { useMissionSocket } from "./hooks/useMissionSocket";
import { usePerformanceMode } from "./hooks/usePerformanceMode";
import { useNarrow, useViewportWidth } from "./hooks/useViewport";
import { useUi } from "./store/ui";

/** Shown over the scene once the camera has left its home view. */
function ResetViewButton() {
  const moved = useUi((ui) => ui.cameraMoved);
  const reset = useUi((ui) => ui.resetView);
  if (!moved) return null;
  return (
    <button type="button" className="button button-small reset-view" onClick={reset}>
      Reset view
    </button>
  );
}

/** Escape closes the innermost thing that is open: an overlay first, then the full reply, then the node details. */
function useEscapeToClose() {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const ui = useUi.getState();
      if (ui.overlay !== null) ui.closeOverlay();
      else if (ui.replyOpen) ui.closeReply();
      else if (ui.selected !== null) ui.select(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
}

export function App({ token }: { token: string | null }) {
  const { retry, send } = useMissionSocket(token);
  const performance = usePerformanceMode();
  const narrow = useNarrow();
  const width = useViewportWidth();
  const overlay = useUi((ui) => ui.overlay);
  const selected = useUi((ui) => ui.selected);
  useEscapeToClose();
  return (
    <HudLayout
      className={`quality-${performance.quality}${narrow ? " hud-narrow" : ""}`}
      side={sidePanelWidth(width)}
      scene={
        <SceneBoundary>
          <Scene quality={performance.quality} narrow={narrow} />
        </SceneBoundary>
      }
      labels={<NodeLabels />}
      top={<TopBar performance={performance} narrow={narrow} />}
      banner={<ConnectionBanner onRetry={retry} />}
      center={<ResetViewButton />}
      dock={<CommandBar send={send} />}
      approval={<ConfirmationPanel send={send} />}
      left={
        <>
          <NodeDetailPanel />
          <SystemMonitorPanel />
        </>
      }
      right={
        <>
          <ReplyPanel />
          <TaskPanel />
          <EventStreamPanel />
        </>
      }
      leftOpen={!narrow || overlay === "system" || selected !== null}
      rightOpen={!narrow || overlay === "events"}
    />
  );
}
