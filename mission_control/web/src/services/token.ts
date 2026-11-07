/**
 * The per-run token JARVIS prints in the terminal link (`http://127.0.0.1:<port>/#token=...`).
 *
 * It travels in the URL fragment, which browsers never send to a server. The page reads it once, keeps it
 * in sessionStorage (so a reload of this tab still works, and it is gone when the tab closes), and removes
 * it from the address bar at once, so it is not left in history, bookmarks or a screen share.
 */

export const TOKEN_KEY = "jarvis.mission.token";

export function takeToken(): string | null {
  const hash = location.hash || "";
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  if (params.has("token")) history.replaceState(null, "", location.pathname);
  const fromLink = params.get("token");
  if (fromLink) {
    try {
      sessionStorage.setItem(TOKEN_KEY, fromLink);
    } catch {
      // Blocked storage: the token still works for this page load, just not after a reload.
    }
    return fromLink;
  }
  try {
    return sessionStorage.getItem(TOKEN_KEY) || null;
  } catch {
    return null;
  }
}
