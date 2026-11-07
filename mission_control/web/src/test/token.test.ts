import { afterEach, describe, expect, it, vi } from "vitest";

import { TOKEN_KEY, takeToken } from "../services/token";

function fakeBrowser(hash: string, stored: string | null = null) {
  const storage = new Map<string, string>(stored === null ? [] : [[TOKEN_KEY, stored]]);
  const replaceState = vi.fn();
  vi.stubGlobal("location", { hash, pathname: "/", search: "" });
  vi.stubGlobal("history", { replaceState });
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => void storage.set(key, value),
    removeItem: (key: string) => void storage.delete(key),
  });
  return { storage, replaceState };
}

afterEach(() => vi.unstubAllGlobals());

describe("takeToken", () => {
  it("reads #token= once, keeps it in sessionStorage and strips it from the address bar", () => {
    const { storage, replaceState } = fakeBrowser("#token=abc_DEF-123");
    expect(takeToken()).toBe("abc_DEF-123");
    expect(storage.get(TOKEN_KEY)).toBe("abc_DEF-123");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/");
  });

  it("falls back to the stored token after a reload", () => {
    const { replaceState } = fakeBrowser("", "stored-token");
    expect(takeToken()).toBe("stored-token");
    expect(replaceState).not.toHaveBeenCalled();
  });

  it("prefers a new link's token over a stale stored one", () => {
    const { storage } = fakeBrowser("#token=fresh", "stale");
    expect(takeToken()).toBe("fresh");
    expect(storage.get(TOKEN_KEY)).toBe("fresh");
  });

  it("returns null without a token, and ignores an empty one", () => {
    fakeBrowser("");
    expect(takeToken()).toBeNull();
    fakeBrowser("#token=");
    expect(takeToken()).toBeNull();
  });

  it("still works when sessionStorage throws (blocked storage)", () => {
    fakeBrowser("#token=abc");
    vi.stubGlobal("sessionStorage", {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
    });
    expect(takeToken()).toBe("abc");
  });
});
