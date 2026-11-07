/**
 * Where the camera sits, decided by projecting the real node ring, so every node and its label stays in
 * the free part of the screen: clear of the top bar, of the docked side columns (wide screens) and of the
 * command bar dock at the bottom centre, at every viewport size.
 *
 * - fitView() backs the camera off from the base distance until the ring fits, then shifts the image
 *   vertically (a projection view offset, so nothing tilts) to centre the ring in the free band. The core
 *   stays horizontally centred.
 * - focusPose() turns the camera toward a selected node by orbiting around the core, as far as the same
 *   check allows, so a focused view never pushes another node under a panel or out of reach.
 * The same functions run in the vitest node environment, so the framing rules are tested as numbers.
 *
 * The pixel sizes below mirror theme.css (gutters, top bar, dock) and NodeLabels (label box).
 */

import { PerspectiveCamera, Spherical, Vector3 } from "three";

import { NODE_LAYOUT } from "../nodes/layout";
import type { SubsystemId } from "../../types/events";

/** Widest viewport that gets the narrow layout (overlays, half the particles). Matches theme.css. */
export const NARROW_MAX_WIDTH = 899;
export const NARROW_QUERY = `(max-width: ${NARROW_MAX_WIDTH}px)`;

export const BASE_DISTANCE = 9.4;
export const CAMERA_HEIGHT = 0.7;
export const LOOK_AT_Y = -0.05;
/** The command bar dock's height (theme.css .hud-dock min-height). */
export const DOCK_HEIGHT = 64;
/** Label offset below its marker, in CSS pixels (used by the label projector too). */
export const LABEL_DROP = 30;

const BASE_FOV = 40;
const PORTRAIT_FOV = 48;
const PANEL_MIN = 252;
const PANEL_MAX = 380;
const PANEL_SHARE = 0.21;
const DOCK_MAX_WIDTH = 640;
/** Top bar heights: one row on wide screens; it wraps to two or three rows on narrow ones. */
const TOPBAR_WIDE = 56;
const TOPBAR_NARROW = 84;
const TOPBAR_SMALL = 116;
const SMALL_MAX_WIDTH = 560;
const GRID_GAP = 14;
/** Breathing room kept between the ring and anything it must stay clear of. */
const CLEARANCE = 6;
/** World radius of a node's halo at its largest (selected). */
const MARKER_RADIUS = 0.28;
/** Label box around its anchor: half its widest line, and its height with up to two fact lines. */
const LABEL_HALF_WIDTH = 64;
const LABEL_HEIGHT = 54;
const STEP = 1.01;
const MAX_DISTANCE_FACTOR = 4;
/** The strongest focus turn: azimuth toward a side node, polar toward a top or bottom one. */
const FOCUS_AZIMUTH = 0.24;
const FOCUS_POLAR = 0.1;
const FOCUS_STRENGTHS = [1, 0.8, 0.6, 0.45, 0.3, 0.15] as const;
/** Extra distance over the tightest fit, so a focus turn has room without crowding a panel. */
const FOCUS_HEADROOM = 1.035;

export interface ScreenRect {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export interface Pose {
  position: Vector3;
  target: Vector3;
}

export interface FitView {
  distance: number;
  fov: number;
  /** Pixels the image is shifted up (PerspectiveCamera.setViewOffset), to centre the ring in the free band. */
  offsetY: number;
  /** Where the ring must stay. */
  free: ScreenRect;
}

export interface NodeScreen {
  id: string;
  marker: ScreenRect;
  label: ScreenRect;
}

const isNarrow = (width: number) => width <= NARROW_MAX_WIDTH;
const gutter = (width: number) => (isNarrow(width) ? 12 : 20);

/** The docked side column width in px for this viewport width (0 when the panels are overlays). */
export function sidePanelWidth(viewportWidth: number): number {
  if (isNarrow(viewportWidth)) return 0;
  return Math.round(Math.min(PANEL_MAX, Math.max(PANEL_MIN, viewportWidth * PANEL_SHARE)));
}

/** Horizontal space each docked column takes from the scene, gutter included. */
export function sidePanelInset(viewportWidth: number): number {
  const width = sidePanelWidth(viewportWidth);
  return width === 0 ? 0 : width + gutter(viewportWidth);
}

export function cameraFov(aspect: number): number {
  return aspect < 1 ? PORTRAIT_FOV : BASE_FOV;
}

function topBarBottom(width: number): number {
  const bar = width <= SMALL_MAX_WIDTH ? TOPBAR_SMALL : isNarrow(width) ? TOPBAR_NARROW : TOPBAR_WIDE;
  return gutter(width) + bar;
}

/** The docked side columns, from below the top bar to the bottom gutter; none on narrow screens. */
export function panelRects(width: number, height: number): ScreenRect[] {
  const side = sidePanelWidth(width);
  if (side === 0) return [];
  const g = gutter(width);
  const top = topBarBottom(width) + GRID_GAP;
  return [
    { left: g, right: g + side, top, bottom: height - g },
    { left: width - g - side, right: width - g, top, bottom: height - g },
  ];
}

/** The command bar dock, at the bottom centre of the scene's middle column. */
export function dockRect(width: number, height: number): ScreenRect {
  const g = gutter(width);
  const inset = sidePanelInset(width);
  const middle = width - 2 * g - 2 * (inset === 0 ? 0 : inset - g + GRID_GAP);
  const dockWidth = Math.min(DOCK_MAX_WIDTH, Math.max(0, middle));
  const left = (width - dockWidth) / 2;
  return { left, right: left + dockWidth, top: height - g - DOCK_HEIGHT, bottom: height - g };
}

function freeRect(width: number, height: number): ScreenRect {
  const inset = sidePanelInset(width);
  const g = gutter(width);
  return {
    left: inset + CLEARANCE,
    right: width - inset - CLEARANCE,
    top: topBarBottom(width) + CLEARANCE,
    bottom: height - g - DOCK_HEIGHT - CLEARANCE,
  };
}

export function homePose(distance: number): Pose {
  return {
    position: new Vector3(0, CAMERA_HEIGHT * (distance / BASE_DISTANCE), distance),
    target: new Vector3(0, LOOK_AT_Y, 0),
  };
}

/** Configure a camera exactly as the scene does: field of view, pose and the vertical view offset. */
export function applyView(camera: PerspectiveCamera, width: number, height: number, view: Pick<FitView, "fov" | "offsetY">): void {
  camera.fov = view.fov;
  camera.aspect = width / Math.max(1, height);
  if (view.offsetY !== 0) camera.setViewOffset(width, height, 0, view.offsetY, width, height);
  else camera.clearViewOffset();
  camera.updateProjectionMatrix();
}

type Point = { id: string; position: readonly [number, number, number] };

/** Screen boxes of each node's marker and label for this pose. */
export function projectNodes(
  pose: Pose, width: number, height: number, view: Pick<FitView, "fov" | "offsetY">,
  points: readonly Point[] = NODE_LAYOUT,
): NodeScreen[] {
  const camera = new PerspectiveCamera(view.fov, width / Math.max(1, height), 0.1, 80);
  camera.position.copy(pose.position);
  camera.lookAt(pose.target);
  camera.updateMatrixWorld();
  applyView(camera, width, height, view);
  const pixelsPerUnit = height / 2 / Math.tan((view.fov * Math.PI) / 360);
  const world = new Vector3();
  return points.map(({ id, position }) => {
    world.set(...position);
    const depth = camera.position.distanceTo(world);
    const ndc = world.clone().project(camera);
    const x = (ndc.x * 0.5 + 0.5) * width;
    const y = (-ndc.y * 0.5 + 0.5) * height;
    const r = (MARKER_RADIUS / depth) * pixelsPerUnit;
    return {
      id,
      marker: { left: x - r, right: x + r, top: y - r, bottom: y + r },
      label: { left: x - LABEL_HALF_WIDTH, right: x + LABEL_HALF_WIDTH, top: y + LABEL_DROP - 3, bottom: y + LABEL_DROP + LABEL_HEIGHT },
    };
  });
}

function within(nodes: NodeScreen[], free: ScreenRect): boolean {
  return nodes.every(({ marker, label }) =>
    Math.min(marker.left, label.left) >= free.left
    && Math.max(marker.right, label.right) <= free.right
    && marker.top >= free.top
    && Math.max(marker.bottom, label.bottom) <= free.bottom);
}

function centred(width: number, height: number, distance: number, fov: number, free: ScreenRect): FitView {
  const raw = projectNodes(homePose(distance), width, height, { fov, offsetY: 0 });
  const top = Math.min(...raw.map((node) => node.marker.top));
  const bottom = Math.max(...raw.map((node) => Math.max(node.marker.bottom, node.label.bottom)));
  const offsetY = Math.round((top + bottom) / 2 - (free.top + free.bottom) / 2);
  return { distance, fov, offsetY, free };
}

/**
 * The home framing for this viewport: the nearest distance at which the whole ring fits (never nearer
 * than the base distance), plus a little headroom for focus turns, with the ring centred in the band.
 */
export function fitView(width: number, height: number): FitView {
  const fov = cameraFov(width / Math.max(1, height));
  const free = freeRect(width, height);
  let distance = BASE_DISTANCE;
  while (distance < BASE_DISTANCE * MAX_DISTANCE_FACTOR) {
    const view = centred(width, height, distance, fov, free);
    if (within(projectNodes(homePose(distance), width, height, view), free)) break;
    distance *= STEP;
  }
  return centred(width, height, distance * FOCUS_HEADROOM, fov, free);
}

/**
 * The focused pose for one node: orbit around the core toward it (and dolly in slightly), at the
 * strongest setting that still keeps all six nodes and labels in the free area. The target stays on
 * the core, so the view turns toward the node rather than sliding the ring sideways under a panel.
 */
export function focusPose(view: FitView, width: number, height: number, id: SubsystemId): Pose {
  const home = homePose(view.distance);
  const node = NODE_LAYOUT.find((placement) => placement.id === id);
  if (node === undefined) return home;
  const [x, y] = node.position;
  const base = new Spherical().setFromVector3(home.position.clone().sub(home.target));
  for (const strength of FOCUS_STRENGTHS) {
    const turned = base.clone();
    turned.theta += strength * FOCUS_AZIMUTH * Math.sign(x);
    turned.phi -= strength * FOCUS_POLAR * Math.sign(Math.abs(x) < 0.01 ? y : 0);
    turned.radius *= 1 - 0.04 * strength;
    const pose = { position: new Vector3().setFromSpherical(turned).add(home.target), target: home.target.clone() };
    if (within(projectNodes(pose, width, height, view), view.free)) return pose;
  }
  return home;
}
