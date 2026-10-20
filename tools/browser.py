"""Opening web pages and YouTube searches in the default browser."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus
from urllib.parse import urlparse

from tools.registry import ToolError


class BrowserTools:
    """Browser tool handlers. Mixed into JarvisTools, which provides self.config and self.memory."""

    # -- browser -------------------------------------------------------------
    def _open_in_browser(self, url: str) -> bool:
        try:
            return bool(self.browser_open(url))
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Could not open the browser: {exc}") from None

    def open_youtube(self, search: str) -> dict[str, Any]:
        url = "https://www.youtube.com/results?search_query=" + quote_plus(search.strip())
        issued = self._open_in_browser(url)
        if not issued:
            raise ToolError("No web browser could be found to open YouTube. Is a default browser set?")
        return {
            "browser_request_issued": True,
            "url": url,
            "note": "A YouTube search page was requested in the browser. No video was played.",
        }

    def open_url(self, url: str | None = None) -> dict[str, Any]:
        url = (url or "https://duckduckgo.com").strip()
        # Reject explicit non-web schemes (javascript:, data:, file:) before adding https://.
        # The (?!\d) keeps "localhost:8080" working.
        scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):(?!\d)", url)
        if scheme and scheme.group(1).lower() not in ("http", "https"):
            raise ToolError("Only http and https web addresses can be opened.")
        parsed = urlparse(url if "://" in url else "https://" + url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ToolError("Only http and https web addresses can be opened.")
        target = parsed.geturl()
        if not self._open_in_browser(target):
            raise ToolError("No web browser could be found. Is a default browser set?")
        return {"browser_request_issued": True, "url": target}
