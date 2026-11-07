/**
 * The person's quality choice (Low / Balanced / High), remembered in localStorage.
 *
 * prefers-reduced-motion forces Low whatever was chosen: the system setting is the person's stronger
 * wish. Storage can be blocked (private windows, strict settings), so every access is wrapped and the
 * choice then simply lasts for this page.
 */

import { useCallback, useState, useSyncExternalStore } from "react";

import { isQualityLevel } from "../components/scene/quality";
import type { QualityLevel } from "../components/scene/quality";

export const QUALITY_STORAGE_KEY = "jarvis.missionControl.quality";
const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

type QualityStorage = Pick<Storage, "getItem" | "setItem">;

export function readStoredQuality(storage: QualityStorage | null): QualityLevel | null {
  try {
    const value = storage?.getItem(QUALITY_STORAGE_KEY) ?? null;
    return isQualityLevel(value) ? value : null;
  } catch {
    return null;
  }
}

export function writeStoredQuality(storage: QualityStorage | null, level: QualityLevel): void {
  try {
    storage?.setItem(QUALITY_STORAGE_KEY, level);
  } catch {
    // Blocked storage: the choice holds for this page only.
  }
}

export function effectiveQuality(chosen: QualityLevel | null, reducedMotion: boolean): QualityLevel {
  if (reducedMotion) return "low";
  return chosen ?? "balanced";
}

function localStore(): QualityStorage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function mediaQuery(query: string): MediaQueryList | null {
  try {
    return window.matchMedia(query);
  } catch {
    return null;
  }
}

/** Subscribes to a media query; false when matchMedia is unavailable. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = mediaQuery(query);
      list?.addEventListener("change", onChange);
      return () => list?.removeEventListener("change", onChange);
    },
    () => mediaQuery(query)?.matches ?? false,
    () => false,
  );
}

export interface PerformanceMode {
  /** What the scene uses. */
  quality: QualityLevel;
  /** What the person picked (null: never picked). */
  chosen: QualityLevel | null;
  /** True while prefers-reduced-motion holds the quality at Low. */
  forcedLow: boolean;
  setQuality: (level: QualityLevel) => void;
}

export function usePerformanceMode(): PerformanceMode {
  const reducedMotion = useMediaQuery(REDUCED_MOTION_QUERY);
  const [chosen, setChosen] = useState<QualityLevel | null>(() => readStoredQuality(localStore()));
  const setQuality = useCallback((level: QualityLevel) => {
    setChosen(level);
    writeStoredQuality(localStore(), level);
  }, []);
  return { quality: effectiveQuality(chosen, reducedMotion), chosen, forcedLow: reducedMotion, setQuality };
}
