/**
 * The page's own state, apart from what JARVIS reports: which node is selected or hovered, which overlay
 * is open on a narrow screen, whether the camera has left its home view, and the event stream's view.
 * Kept out of the mission store so a click never touches the reducer's picture of JARVIS.
 */

import { create } from "zustand";

import { clearStream, filterStream, initialStreamView, pauseStream, resumeStream } from "./eventStream";
import type { StreamFilter, StreamView } from "./eventStream";
import { useMission } from "./store";
import type { SubsystemId } from "../types/events";

export type Overlay = "system" | "events" | null;

export interface UiStore {
  selected: SubsystemId | null;
  hovered: SubsystemId | null;
  overlay: Overlay;
  /** True once the person orbits, zooms or focuses a node; shows "Reset view". */
  cameraMoved: boolean;
  /** Bumped to ask the camera rig to fly home. */
  resetRequest: number;
  stream: StreamView;
  select: (id: SubsystemId | null) => void;
  hover: (id: SubsystemId | null) => void;
  toggleOverlay: (overlay: Exclude<Overlay, null>) => void;
  closeOverlay: () => void;
  setCameraMoved: (moved: boolean) => void;
  resetView: () => void;
  pauseStream: () => void;
  resumeStream: () => void;
  clearStream: () => void;
  filterStream: (filter: StreamFilter) => void;
}

export const useUi = create<UiStore>()((set) => ({
  selected: null,
  hovered: null,
  overlay: null,
  cameraMoved: false,
  resetRequest: 0,
  stream: initialStreamView(),
  select: (id) => set((state) => (state.selected === id ? state : { selected: id, ...(id !== null ? { cameraMoved: true } : {}) })),
  hover: (id) => set((state) => (state.hovered === id ? state : { hovered: id })),
  toggleOverlay: (overlay) => set((state) => ({ overlay: state.overlay === overlay ? null : overlay })),
  closeOverlay: () => set({ overlay: null }),
  setCameraMoved: (moved) => set((state) => (state.cameraMoved === moved ? state : { cameraMoved: moved })),
  resetView: () => set((state) => ({ selected: null, cameraMoved: false, resetRequest: state.resetRequest + 1 })),
  pauseStream: () => set((state) => ({ stream: pauseStream(state.stream, useMission.getState().log) })),
  resumeStream: () => set((state) => ({ stream: resumeStream(state.stream) })),
  clearStream: () => set((state) => ({ stream: clearStream(state.stream, useMission.getState().log) })),
  filterStream: (filter) => set((state) => ({ stream: filterStream(state.stream, filter) })),
}));
