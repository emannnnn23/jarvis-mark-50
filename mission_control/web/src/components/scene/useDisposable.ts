import { useEffect, useMemo } from "react";

/**
 * A three.js resource created once per `deps` and disposed when the component unmounts (or the deps
 * change), so remounting the scene never leaks GPU buffers or shader programs.
 */
export function useDisposable<T extends { dispose(): void }>(create: () => T, deps: readonly unknown[]): T {
  const resource = useMemo(create, deps);
  useEffect(() => () => resource.dispose(), [resource]);
  return resource;
}
