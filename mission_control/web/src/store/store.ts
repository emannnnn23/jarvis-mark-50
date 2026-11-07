/** The one store: the reducer's state plus `dispatch`. Components read slices of it with selectors. */

import { create } from "zustand";

import { initialMissionState, reduce } from "./reducer";
import type { MissionInput, MissionState } from "./reducer";

export interface MissionStore extends MissionState {
  dispatch: (input: MissionInput) => void;
}

export const useMission = create<MissionStore>()((set) => ({
  ...initialMissionState(),
  dispatch: (input) => set((state) => reduce(state, input)),
}));
