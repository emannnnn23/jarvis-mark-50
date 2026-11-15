/** True while the tab is visible. The scene stops rendering entirely when it is not. */

import { useSyncExternalStore } from "react";

function subscribe(onChange: () => void): () => void {
  document.addEventListener("visibilitychange", onChange);
  return () => document.removeEventListener("visibilitychange", onChange);
}

export function useDocumentVisible(): boolean {
  return useSyncExternalStore(subscribe, () => document.visibilityState !== "hidden", () => true);
}
