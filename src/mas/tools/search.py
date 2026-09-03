"""Web search, the one real-world tool the Research Agent is given.

Search is a hard dependency to test against, so it is a small Protocol with two
implementations: a live DuckDuckGo backend and a no-op used when search is
disabled or unavailable.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..state import Source

log = logging.getLogger(__name__)


class SearchTool(Protocol):
    def __call__(self, query: str, max_results: int = 5) -> list[Source]: ...


def _null_search(query: str, max_results: int = 5) -> list[Source]:
    """Return nothing; the Research Agent then falls back to model knowledge."""
    return []


def _duckduckgo_search(query: str, max_results: int = 5) -> list[Source]:
    try:
        from ddgs import DDGS
    except ImportError:  # pragma: no cover - depends on the install
        log.warning("ddgs is not installed; continuing without web search")
        return []

    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:  # network flakiness must not kill a report run
        log.warning("search failed for %r: %s", query, exc)
        return []

    return [
        Source(
            title=hit.get("title", "") or query,
            url=hit.get("href", "") or hit.get("url", ""),
            snippet=(hit.get("body", "") or "")[:600],
        )
        for hit in hits
    ]


def build_search_tool(backend: str) -> SearchTool:
    """Resolve a backend name to a callable search tool."""
    if backend == "duckduckgo":
        return _duckduckgo_search
    if backend in ("none", "off", ""):
        return _null_search
    raise ValueError(f"unknown search backend: {backend!r}")
