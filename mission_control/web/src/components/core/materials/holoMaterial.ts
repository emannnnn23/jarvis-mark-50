/**
 * The core's shaders. Everything glows by additive blending of its own colour: no postprocessing, no
 * textures, a handful of ALU operations per fragment, which an HD 620 draws at full rate.
 */

import { AdditiveBlending, Color, DoubleSide, ShaderMaterial } from "three";

export interface HoloUniforms {
  uTime: { value: number };
  uColor: { value: Color };
  uIntensity: { value: number };
}

const holoVertex = /* glsl */ `
  varying vec3 vNormalView;
  varying vec3 vViewDir;
  varying vec3 vLocal;
  void main() {
    vLocal = position;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vNormalView = normalize(normalMatrix * normal);
    vViewDir = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }
`;

// Fresnel rim (bright edge, clear centre, so the shell reads as glass), fine latitude lines that drift
// upward like a scan, and one slow bright band sweeping the sphere.
const holoFragment = /* glsl */ `
  uniform float uTime;
  uniform vec3 uColor;
  uniform float uIntensity;
  varying vec3 vNormalView;
  varying vec3 vViewDir;
  varying vec3 vLocal;
  void main() {
    float facing = abs(dot(normalize(vNormalView), normalize(vViewDir)));
    float rim = pow(1.0 - facing, 2.4);
    float lines = smoothstep(0.86, 1.0, 0.5 + 0.5 * sin(vLocal.y * 42.0 - uTime * 1.2));
    float sweep = smoothstep(0.18, 0.0, abs(fract(vLocal.y * 0.5 + 0.5 - uTime * 0.07) - 0.5));
    float glow = rim * 1.35 + lines * 0.2 * (0.3 + rim) + sweep * 0.12 + 0.02;
    float a = glow * uIntensity;
    gl_FragColor = vec4(uColor * a, a);
  }
`;

export function createHoloMaterial(color: string): ShaderMaterial & { uniforms: HoloUniforms } {
  const uniforms: HoloUniforms = {
    uTime: { value: 0 },
    uColor: { value: new Color(color) },
    uIntensity: { value: 0.5 },
  };
  return new ShaderMaterial({
    uniforms: uniforms as unknown as ShaderMaterial["uniforms"],
    vertexShader: holoVertex,
    fragmentShader: holoFragment,
    transparent: true,
    depthWrite: false,
    blending: AdditiveBlending,
    side: DoubleSide,
  }) as ShaderMaterial & { uniforms: HoloUniforms };
}

// The heart: the inverse of the shell, brightest where it faces the camera, so a plain sphere reads as a
// soft point of light with no visible edge.
const glowFragment = /* glsl */ `
  uniform float uTime;
  uniform vec3 uColor;
  uniform float uIntensity;
  varying vec3 vNormalView;
  varying vec3 vViewDir;
  varying vec3 vLocal;
  void main() {
    float facing = abs(dot(normalize(vNormalView), normalize(vViewDir)));
    float core = pow(facing, 3.0);
    float a = (core * 0.85 + pow(facing, 12.0) * 0.6) * uIntensity;
    vec3 hot = mix(uColor, vec3(1.0), pow(facing, 8.0) * 0.55);
    gl_FragColor = vec4(hot * a, a);
  }
`;

export function createGlowMaterial(color: string): ShaderMaterial & { uniforms: HoloUniforms } {
  const uniforms: HoloUniforms = {
    uTime: { value: 0 },
    uColor: { value: new Color(color) },
    uIntensity: { value: 0.5 },
  };
  return new ShaderMaterial({
    uniforms: uniforms as unknown as ShaderMaterial["uniforms"],
    vertexShader: holoVertex,
    fragmentShader: glowFragment,
    transparent: true,
    depthWrite: false,
    blending: AdditiveBlending,
  }) as ShaderMaterial & { uniforms: HoloUniforms };
}

export interface RingUniforms extends HoloUniforms {
  uSegments: { value: number };
  uDuty: { value: number };
  uSweep: { value: number };
}

const ringVertex = /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

// uv.x runs once around a TorusGeometry. Segments turn the ring into a scale of ticks; a bright arc
// travels round it at the ring's own speed, which is what makes the rotation legible.
const ringFragment = /* glsl */ `
  uniform float uTime;
  uniform vec3 uColor;
  uniform float uIntensity;
  uniform float uSegments;
  uniform float uDuty;
  uniform float uSweep;
  varying vec2 vUv;
  void main() {
    float tick = uSegments > 0.0 ? step(fract(vUv.x * uSegments), uDuty) : 1.0;
    float head = fract(vUv.x - uTime * uSweep);
    float arc = pow(head, 10.0);
    float a = (0.55 * tick + arc * 1.2) * uIntensity;
    if (a < 0.004) discard;
    gl_FragColor = vec4(uColor * a, a);
  }
`;

export function createRingMaterial(color: string, segments: number, duty: number, sweep: number) {
  const uniforms: RingUniforms = {
    uTime: { value: 0 },
    uColor: { value: new Color(color) },
    uIntensity: { value: 0.5 },
    uSegments: { value: segments },
    uDuty: { value: duty },
    uSweep: { value: sweep },
  };
  return new ShaderMaterial({
    uniforms: uniforms as unknown as ShaderMaterial["uniforms"],
    vertexShader: ringVertex,
    fragmentShader: ringFragment,
    transparent: true,
    depthWrite: false,
    blending: AdditiveBlending,
  }) as ShaderMaterial & { uniforms: RingUniforms };
}
