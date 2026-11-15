/**
 * The floor: one LineSegments grid below the core that fades out with distance, giving the scene a
 * ground plane and a sense of scale without drawing the eye away from the centre.
 */

import { AdditiveBlending, BufferAttribute, BufferGeometry, LineBasicMaterial } from "three";

import { useDisposable } from "./useDisposable";

const HALF = 13.5;
const STEP = 0.75;
const FLOOR_Y = -3.1;
const BASE = [0.18, 0.48, 0.96]; // electric blue, scaled per vertex

function buildGrid(): BufferGeometry {
  const positions: number[] = [];
  const colors: number[] = [];
  const shade = (x: number, z: number) => {
    const d = Math.hypot(x, z) / HALF;
    const k = Math.max(0, 1 - d) ** 2.2 * 0.42;
    colors.push(BASE[0]! * k, BASE[1]! * k, BASE[2]! * k);
  };
  // Lines are cut into STEP-long pieces so the per-vertex fade follows the distance from the centre.
  for (let a = -HALF; a <= HALF + 1e-6; a += STEP) {
    for (let b = -HALF; b < HALF - 1e-6; b += STEP) {
      positions.push(a, FLOOR_Y, b, a, FLOOR_Y, b + STEP);
      shade(a, b);
      shade(a, b + STEP);
      positions.push(b, FLOOR_Y, a, b + STEP, FLOOR_Y, a);
      shade(b, a);
      shade(b + STEP, a);
    }
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new BufferAttribute(new Float32Array(positions), 3));
  geometry.setAttribute("color", new BufferAttribute(new Float32Array(colors), 3));
  return geometry;
}

export function HoloGrid() {
  const geometry = useDisposable(buildGrid, []);
  const material = useDisposable(
    () => new LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false, blending: AdditiveBlending }),
    [],
  );
  return <lineSegments geometry={geometry} material={material} dispose={null} />;
}
