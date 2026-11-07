/**
 * The bridge between the 3D scene and the node labels, which live in the DOM (crisp text, React text
 * nodes only). The labels register their elements here; the scene projects each node's position every
 * frame and moves its label, writing a transform only when it changed.
 */

import type { SubsystemId } from "../../types/events";

export const labelElements = new Map<SubsystemId, HTMLElement>();

export function registerLabel(id: SubsystemId) {
  return (element: HTMLElement | null) => {
    if (element === null) labelElements.delete(id);
    else labelElements.set(id, element);
  };
}
