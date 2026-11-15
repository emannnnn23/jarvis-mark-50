/** The viewport width and whether it gets the narrow layout (below 900 px). */

import { useSyncExternalStore } from "react";

import { NARROW_QUERY } from "../components/scene/cameraFit";
import { useMediaQuery } from "./usePerformanceMode";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("resize", onChange);
  return () => window.removeEventListener("resize", onChange);
}

export function useViewportWidth(): number {
  return useSyncExternalStore(subscribe, () => window.innerWidth, () => 1280);
}

export function useNarrow(): boolean {
  return useMediaQuery(NARROW_QUERY);
}
