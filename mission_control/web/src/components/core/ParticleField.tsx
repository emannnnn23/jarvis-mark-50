/**
 * The particle field: one THREE.Points whose every motion happens in the vertex shader (uTime,
 * uIntensity, uState), so the CPU uploads nothing per frame and the count only costs fill rate.
 */

import { useFrame, useThree } from "@react-three/fiber";
import { AdditiveBlending, BufferAttribute, BufferGeometry, Color, ShaderMaterial } from "three";

import { useDisposable } from "../scene/useDisposable";
import type { CoreLive } from "./JarvisCore";

const vertexShader = /* glsl */ `
  attribute float aSeed;
  attribute float aRadius;
  uniform float uTime;
  uniform float uIntensity;
  uniform float uState;
  uniform float uSize;
  uniform float uPixelRatio;
  varying float vAlpha;
  void main() {
    vec3 p = position;
    // Orbit about the vertical axis; inner particles move faster, like a disc.
    float angle = uTime * (0.06 + 0.22 * uIntensity) * (0.55 + aSeed) / (0.5 + aRadius * 0.35);
    float c = cos(angle);
    float s = sin(angle);
    p.xz = mat2(c, -s, s, c) * p.xz;
    p.y += sin(uTime * 0.5 + aSeed * 6.2831) * 0.05;
    // Working states (processing, speaking, tool) draw the field in and out a little.
    float working = step(1.5, uState) * step(uState, 4.5);
    p *= 1.0 - working * 0.05 * (0.5 + 0.5 * sin(uTime * 2.2 + aSeed * 11.0));
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_PointSize = uSize * uPixelRatio * (0.45 + aSeed) / -mv.z;
    float twinkle = 0.55 + 0.45 * sin(uTime * (0.8 + aSeed * 2.0) + aSeed * 40.0);
    vAlpha = twinkle * (0.35 + 0.9 * uIntensity) * smoothstep(3.8, 1.4, aRadius) * (0.4 + 0.6 * fract(aSeed * 7.31));
    gl_Position = projectionMatrix * mv;
  }
`;

const fragmentShader = /* glsl */ `
  uniform vec3 uColor;
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    if (d > 0.5) discard;
    float a = smoothstep(0.5, 0.0, d) * vAlpha;
    gl_FragColor = vec4(uColor * a, a);
  }
`;

/** Deterministic pseudo-random numbers, so the field looks the same on every load. */
function mulberry32(seed: number) {
  let t = seed >>> 0;
  return () => {
    t = (t + 0x6d2b79f5) >>> 0;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

function buildGeometry(count: number): BufferGeometry {
  const random = mulberry32(50);
  const positions = new Float32Array(count * 3);
  const seeds = new Float32Array(count);
  const radii = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    const radius = 1.25 + 2.3 * Math.pow(random(), 1.6); // denser near the reactor
    const theta = random() * Math.PI * 2;
    const y = (random() * 2 - 1) * 0.62; // a flattened shell: more disc than ball
    const ring = Math.sqrt(1 - y * y);
    positions[i * 3] = Math.cos(theta) * ring * radius;
    positions[i * 3 + 1] = y * radius * 0.7;
    positions[i * 3 + 2] = Math.sin(theta) * ring * radius;
    seeds[i] = random();
    radii[i] = radius;
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new BufferAttribute(positions, 3));
  geometry.setAttribute("aSeed", new BufferAttribute(seeds, 1));
  geometry.setAttribute("aRadius", new BufferAttribute(radii, 1));
  return geometry;
}

export function ParticleField({ live, count }: { live: CoreLive; count: number }) {
  const pixelRatio = useThree((three) => three.viewport.dpr);
  const geometry = useDisposable(() => buildGeometry(count), [count]);
  const material = useDisposable(
    () =>
      new ShaderMaterial({
        uniforms: {
          uTime: { value: 0 },
          uIntensity: { value: 0.5 },
          uState: { value: 0 },
          uSize: { value: 58 },
          uPixelRatio: { value: 1 },
          uColor: { value: new Color("#6fe7f5") },
        },
        vertexShader,
        fragmentShader,
        transparent: true,
        depthWrite: false,
        blending: AdditiveBlending,
      }),
    [],
  );

  useFrame(() => {
    const u = material.uniforms;
    u.uTime!.value = live.time;
    u.uIntensity!.value = live.intensity;
    u.uState!.value = live.stateIndex;
    u.uPixelRatio!.value = pixelRatio;
    (u.uColor!.value as Color).copy(live.color);
  });

  return <points geometry={geometry} material={material} dispose={null} frustumCulled={false} />;
}
