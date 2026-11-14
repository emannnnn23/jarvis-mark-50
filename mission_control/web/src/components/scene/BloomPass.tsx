/**
 * The High-quality glow: one bloom pass over the scene. This module is only ever loaded through a
 * dynamic import from Scene.tsx when the quality is High, so Low and Balanced never download
 * postprocessing (vite puts it in its own "bloom" chunk).
 */

import { Bloom, EffectComposer } from "@react-three/postprocessing";

export default function BloomPass() {
  return (
    <EffectComposer multisampling={4} enableNormalPass={false}>
      <Bloom mipmapBlur intensity={0.85} luminanceThreshold={0.22} luminanceSmoothing={0.3} radius={0.7} />
    </EffectComposer>
  );
}
