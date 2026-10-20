"""Web search through DuckDuckGo (ddgs)."""

from __future__ import annotations

import time
from typing import Any

from core.audit import redact
from tools.registry import STATUS_SUCCESS, ToolError, UNTRUSTED_NOTICE


def _import_ddgs():
    try:
        from ddgs import DDGS  # current package name
        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS  # pyrefly: ignore[missing-import]  # older package name, optional
        return DDGS
    except ImportError:
        raise ToolError("Web search is unavailable: install it with 'pip install ddgs'.") from None


class WebSearchTools:
    """Web search tool handlers. Mixed into JarvisTools, which provides self.config and self.memory."""

    # -- information ---------------------------------------------------------
    def search_web(self, query: str, max_results: int | None = None) -> dict[str, Any]:
        query = query.strip()
        limit = min(max_results or self.config.search_limit, self.config.search_limit, 10)
        factory = self.ddgs_factory or _import_ddgs()
        raw_results = None
        last_error: Exception | None = None
        for attempt in range(2):  # one retry for transient failures
            try:
                raw_results = list(factory(timeout=10).text(query, max_results=limit) or [])
                break
            except Exception as exc:  # noqa: BLE001 - library raises several exception types
                last_error = exc
                kind = type(exc).__name__.lower()
                if "ratelimit" in kind or "429" in str(exc):
                    raise ToolError("DuckDuckGo is rate-limiting searches right now; try again in a minute.") from None
                if attempt == 0 and ("timeout" in kind or "connect" in kind or "timed out" in str(exc).lower()):
                    time.sleep(1.0)
                    continue
                break
        if raw_results is None:
            text = str(last_error)
            if "no results" in text.lower():
                raw_results = []
            else:
                raise ToolError(f"Web search failed: {type(last_error).__name__}: {redact(text)[:200]}")
        results = []
        for item in raw_results[:limit]:
            if not isinstance(item, dict):
                continue
            results.append({
                "title": str(item.get("title") or "")[:150],
                "url": str(item.get("href") or item.get("url") or ""),
                "snippet": str(item.get("body") or item.get("snippet") or "")[:300],
            })
        if not results:
            return {"status": STATUS_SUCCESS, "query": query, "results": [],
                    "message": "No results found. Try a different phrasing."}
        return {
            "query": query,
            "results": results,
            "note": "These are search snippets only; the pages themselves were not opened. Cite the URLs.",
            "notice": UNTRUSTED_NOTICE,
        }
