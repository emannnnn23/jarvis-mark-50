import { describe, expect, it } from "vitest";

import {
  DOCK_HEIGHT,
  NARROW_MAX_WIDTH,
  cameraFov,
  dockRect,
  fitView,
  focusPose,
  homePose,
  panelRects,
  projectNodes,
  sidePanelInset,
} from "../components/scene/cameraFit";
import type { ScreenRect } from "../components/scene/cameraFit";
import { NODE_LAYOUT } from "../components/nodes/layout";
import { particleCount } from "../components/scene/quality";
import { DEMAND_FPS, EVENT_ACTIVE_MS, demandFps, frameloopFor, needsWake } from "../hooks/useAdaptiveFrameloop";
import type { FrameloopInput } from "../hooks/useAdaptiveFrameloop";
import { QUALITY_STORAGE_KEY, effectiveQuality, readStoredQuality, writeStoredQuality } from "../hooks/usePerformanceMode";

const NOW = 1_000_000;

const base: FrameloopInput = {
  hidden: false, state: "idle", interacting: false, lastEventAt: null, now: NOW, quality: "balanced",
};

describe("frameloopFor", () => {
  const table: [string, Partial<FrameloopInput>, "never" | "always" | "demand"][] = [
    ["idle with nothing happening", {}, "demand"],
    ["idle in low quality", { quality: "low" }, "demand"],
    ["offline", { state: "offline" }, "demand"],
    ["hidden tab, even while working", { hidden: true, state: "processing", interacting: true }, "never"],
    ["listening", { state: "listening" }, "always"],
    ["processing", { state: "processing" }, "always"],
    ["speaking", { state: "speaking" }, "always"],
    ["running a tool", { state: "tool" }, "always"],
    ["error hold", { state: "error" }, "always"],
    ["active state in low quality", { state: "processing", quality: "low" }, "always"],
    ["camera interaction", { interacting: true }, "always"],
    ["just after an event", { lastEventAt: NOW - 200 }, "always"],
    ["right at the end of the event window", { lastEventAt: NOW - EVENT_ACTIVE_MS + 1 }, "always"],
    ["after the event window", { lastEventAt: NOW - EVENT_ACTIVE_MS }, "demand"],
    ["long after an event", { lastEventAt: NOW - 60_000 }, "demand"],
  ];
  for (const [name, overrides, expected] of table) {
    it(`${name} -> ${expected}`, () => {
      expect(frameloopFor({ ...base, ...overrides })).toBe(expected);
    });
  }

  it("keeps an event alive for 1.5 s", () => {
    expect(EVENT_ACTIVE_MS).toBe(1500);
  });
});

describe("demandFps", () => {
  it("breathes at 15 fps when idle, and not at all in Low", () => {
    expect(DEMAND_FPS).toBe(15);
    expect(demandFps("high")).toBe(15);
    expect(demandFps("balanced")).toBe(15);
    expect(demandFps("low")).toBe(0);
  });
});

/** A Storage stand-in: an in-memory map, or one that throws like a blocked or private-mode store. */
function fakeStorage(initial: Record<string, string> = {}, broken = false) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem(key: string) {
      if (broken) throw new Error("SecurityError");
      return data.get(key) ?? null;
    },
    setItem(key: string, value: string) {
      if (broken) throw new Error("QuotaExceededError");
      data.set(key, value);
    },
  };
}

describe("quality choice", () => {
  it("is remembered in localStorage and read back", () => {
    const storage = fakeStorage();
    expect(readStoredQuality(storage)).toBeNull();
    writeStoredQuality(storage, "high");
    expect(storage.data.get(QUALITY_STORAGE_KEY)).toBe("high");
    expect(readStoredQuality(storage)).toBe("high");
  });

  it("ignores junk and survives blocked storage", () => {
    expect(readStoredQuality(fakeStorage({ [QUALITY_STORAGE_KEY]: "ultra" }))).toBeNull();
    expect(readStoredQuality(fakeStorage({}, true))).toBeNull();
    expect(() => writeStoredQuality(fakeStorage({}, true), "low")).not.toThrow();
    expect(readStoredQuality(null)).toBeNull();
  });

  it("defaults to Balanced, follows the choice, and prefers-reduced-motion forces Low", () => {
    expect(effectiveQuality(null, false)).toBe("balanced");
    expect(effectiveQuality("high", false)).toBe("high");
    expect(effectiveQuality("low", false)).toBe("low");
    expect(effectiveQuality("high", true)).toBe("low");
    expect(effectiveQuality(null, true)).toBe("low");
  });
});

describe("responsive budget", () => {
  it("halves the particle count below 900 px", () => {
    expect(NARROW_MAX_WIDTH).toBe(899);
    expect(particleCount("low", false)).toBe(400);
    expect(particleCount("balanced", false)).toBe(1200);
    expect(particleCount("high", false)).toBe(3000);
    expect(particleCount("low", true)).toBe(200);
    expect(particleCount("balanced", true)).toBe(600);
    expect(particleCount("high", true)).toBe(1500);
  });

  it("reserves room for docked side panels only on wide screens", () => {
    expect(sidePanelInset(800)).toBe(0); // overlays below 900 px
    expect(sidePanelInset(1366)).toBeGreaterThan(250);
    expect(sidePanelInset(2560)).toBeLessThanOrEqual(400 + 24);
  });

  it("widens the field of view on portrait screens", () => {
    expect(cameraFov(390 / 844)).toBeGreaterThan(cameraFov(1366 / 768));
  });
});

const overlaps = (a: ScreenRect, b: ScreenRect) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

describe("camera framing", () => {
  const VIEWPORTS: [number, number][] = [[1366, 768], [1920, 1080], [1280, 800], [1440, 900], [1200, 1100], [2560, 1080], [820, 760], [390, 844]];

  it("reserves a real dock for the command bar", () => {
    expect(DOCK_HEIGHT).toBeGreaterThanOrEqual(64);
    const dock = dockRect(1366, 768);
    expect(dock.bottom - dock.top).toBe(DOCK_HEIGHT);
    expect(dock.bottom).toBeLessThanOrEqual(768 - 12);
  });

  it("keeps the lowest node label above the dock at 1366x768", () => {
    const view = fitView(1366, 768);
    const nodes = projectNodes(homePose(view.distance), 1366, 768, view);
    const lowest = Math.max(...nodes.map((node) => node.label.bottom));
    expect(lowest).toBeLessThanOrEqual(dockRect(1366, 768).top);
  });

  for (const [width, height] of VIEWPORTS) {
    it(`frames every node and label clear of the panels, top bar and dock at ${width}x${height}`, () => {
      const view = fitView(width, height);
      const nodes = projectNodes(homePose(view.distance), width, height, view);
      expect(nodes).toHaveLength(6);
      const blocked = [...panelRects(width, height), dockRect(width, height)];
      for (const node of nodes) {
        for (const rect of blocked) {
          expect(overlaps(node.marker, rect), `${node.id} marker vs ${JSON.stringify(rect)}`).toBe(false);
          expect(overlaps(node.label, rect), `${node.id} label vs ${JSON.stringify(rect)}`).toBe(false);
        }
        expect(node.marker.top, `${node.id} under the top bar`).toBeGreaterThanOrEqual(view.free.top);
        expect(node.marker.left).toBeGreaterThanOrEqual(0);
        expect(node.label.right).toBeLessThanOrEqual(width);
      }
      // The core sits in the middle of the free space, horizontally centred.
      const core = projectNodes(homePose(view.distance), width, height, view, [{ id: "core", position: [0, 0, 0] }])[0]!;
      expect(Math.abs((core.marker.left + core.marker.right) / 2 - width / 2)).toBeLessThan(2);
    });
  }

  it("keeps the 16:9 core about as large as before (no needless zoom-out)", () => {
    const view = fitView(1366, 768);
    expect(view.distance).toBeGreaterThan(9);
    expect(view.distance).toBeLessThan(11.5);
  });

  for (const [width, height] of [[1366, 768], [1920, 1080], [1280, 800]] as const) {
    it(`keeps all six nodes and labels visible and clickable with any node focused at ${width}x${height}`, () => {
      const view = fitView(width, height);
      const blocked = [...panelRects(width, height), dockRect(width, height)];
      for (const { id } of NODE_LAYOUT) {
        const pose = focusPose(view, width, height, id);
        const nodes = projectNodes(pose, width, height, view);
        for (const node of nodes) {
          for (const rect of blocked) {
            expect(overlaps(node.marker, rect), `focus ${id}: ${node.id} marker`).toBe(false);
            expect(overlaps(node.label, rect), `focus ${id}: ${node.id} label`).toBe(false);
          }
          expect(node.marker.top, `focus ${id}: ${node.id} under the top bar`).toBeGreaterThanOrEqual(view.free.top);
        }
      }
    });
  }

  it("focus really moves the camera for a side node at 1366x768", () => {
    const view = fitView(1366, 768);
    const home = homePose(view.distance);
    const focused = focusPose(view, 1366, 768, "tools");
    expect(focused.position.distanceTo(home.position)).toBeGreaterThan(0.2);
    expect(focused.target.toArray()).toEqual(home.target.toArray()); // the core stays the centre of attention
  });
});

describe("needsWake", () => {
  it("wakes the frameloop when the shown state changes or the tab becomes visible again", () => {
    const before = { state: "idle" as const, visible: true };
    expect(needsWake(before, { state: "offline", visible: true })).toBe(true);
    expect(needsWake({ state: "idle", visible: false }, { state: "idle", visible: true })).toBe(true);
    expect(needsWake(before, { state: "idle", visible: true })).toBe(false);
    expect(needsWake(before, { state: "idle", visible: false })).toBe(false);
  });
});
