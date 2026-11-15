/**
 * Camera activity, shared between the camera rig (inside the canvas) and the frameloop decision (outside
 * it) without React state: the rig marks activity on every drag, zoom, focus move or parallax pointer
 * move, and the frameloop hook keeps rendering continuously until it has been quiet for a moment.
 */

/** How long rendering stays continuous after the last camera movement (damping settles within it). */
export const CAMERA_SETTLE_MS = 900;

type Listener = () => void;

function createCameraActivity() {
  let holding = 0; // drags in progress
  let lastAt = Number.NEGATIVE_INFINITY;
  let wasActive = false;
  const listeners = new Set<Listener>();
  const notify = (now: number) => {
    const active = holding > 0 || now - lastAt < CAMERA_SETTLE_MS;
    if (active && !wasActive) listeners.forEach((listener) => listener());
    wasActive = active;
  };
  return {
    /** Something moved the camera just now. */
    mark(now = performance.now()): void {
      lastAt = now;
      notify(now);
    },
    begin(now = performance.now()): void {
      holding += 1;
      lastAt = now;
      notify(now);
    },
    end(now = performance.now()): void {
      holding = Math.max(0, holding - 1);
      lastAt = now;
      wasActive = true;
    },
    isActive(now = performance.now()): boolean {
      const active = holding > 0 || now - lastAt < CAMERA_SETTLE_MS;
      wasActive = active;
      return active;
    },
    /** Called when activity starts after a quiet spell, so the frameloop can switch to "always" at once. */
    subscribe(listener: Listener): () => void {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export const cameraActivity = createCameraActivity();
