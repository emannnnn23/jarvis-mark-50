import { useMemo } from "react";

import { Scene } from "./components/scene/Scene";
import { defaultQuality } from "./components/scene/quality";
import { HudLayout, SceneBoundary } from "./components/layout/HudLayout";
import { TopBar } from "./components/layout/TopBar";
import { NodeLabels } from "./components/nodes/NodeLabels";
import { ConnectionBanner } from "./components/panels/ConnectionBanner";
import { SystemMonitorPanel } from "./components/panels/SystemMonitorPanel";
import { useMissionSocket } from "./hooks/useMissionSocket";

export function App({ token }: { token: string | null }) {
  const { retry } = useMissionSocket(token);
  const quality = useMemo(defaultQuality, []);
  return (
    <HudLayout
      className={`quality-${quality}`}
      scene={
        <SceneBoundary>
          <Scene quality={quality} />
        </SceneBoundary>
      }
      labels={<NodeLabels />}
      top={<TopBar />}
      banner={<ConnectionBanner onRetry={retry} />}
      left={<SystemMonitorPanel />}
    />
  );
}
