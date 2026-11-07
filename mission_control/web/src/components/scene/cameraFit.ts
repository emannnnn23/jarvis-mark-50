/**
 * Where the camera sits, as a pure function of the viewport, so the node ring and its labels always fit
 * between the docked side panels and the core stays in the middle of the free space.
 *
 * Wide screens dock the panels at both edges (sidePanelInset px each); below 900 px they become
 * overlays, so the whole width is free. The FOV widens a little on portrait screens, where backing the
 * camera off alone would make the core tiny.
 */

/** Widest viewport that gets the narrow layout (overlays, half the particles). Matches theme.css. */
export const NARROW_MAX_WIDTH = 899;
export const NARROW_QUERY = `(max-width: ${NARROW_MAX_WIDTH}px)`;

export const BASE_DISTANCE = 9.4;
export const CAMERA_HEIGHT = 0.7;
export const LOOK_AT_Y = -0.05;
const BASE_FOV = 40;
const PORTRAIT_FOV = 48;
/** World half-width the node ring needs with its labels, measured on the 16:9 layout. */
const RING_HALF_WIDTH = 3.9;
/** World half-width the ring's outer labels reach; it must fit inside the space between the panels. */
const LABEL_HALF_WIDTH = 3.45;
const GUTTER = 20;
const PANEL_MIN = 252;
const PANEL_MAX = 380;
const PANEL_SHARE = 0.21;

/** The docked side panel width in px for this viewport width (0 when the panels are overlays). */
export function sidePanelWidth(viewportWidth: number): number {
  if (viewportWidth <= NARROW_MAX_WIDTH) return 0;
  return Math.round(Math.min(PANEL_MAX, Math.max(PANEL_MIN, viewportWidth * PANEL_SHARE)));
}

/** Horizontal space each docked panel takes from the scene, gutter included. */
export function sidePanelInset(viewportWidth: number): number {
  const width = sidePanelWidth(viewportWidth);
  return width === 0 ? 0 : width + GUTTER;
}

export function cameraFov(aspect: number): number {
  return aspect < 1 ? PORTRAIT_FOV : BASE_FOV;
}

/** Distance from the core that keeps the whole ring in the free part of the screen. */
export function cameraDistance(width: number, height: number): number {
  const aspect = width / Math.max(1, height);
  const halfTan = Math.tan((cameraFov(aspect) * Math.PI) / 360);
  const free = Math.max(0.3, 1 - (2 * sidePanelInset(width)) / Math.max(1, width));
  return Math.max(
    BASE_DISTANCE,
    RING_HALF_WIDTH / (halfTan * aspect),
    LABEL_HALF_WIDTH / (halfTan * aspect * free),
  );
}
