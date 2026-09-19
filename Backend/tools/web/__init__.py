"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — Web Tools Package
═══════════════════════════════════════════════════════════════════════════════

  Dedicated package for all web operations, intelligent search & browser control:
    - search.py  : High-accuracy clean query extraction, Google, Wikipedia,
                   DuckDuckGo quick factual answers & web search.
    - control.py : Browser tab management, navigation (back/forward), refresh,
                   zoom, scrolling, and in-page find.
    - reader.py  : Web page content extraction & clean article text reading.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from . import search, control, reader
from .search import (
    extract_clean_search_query,
    web_search,
    google_search,
    wikipedia_search,
    web_quick_answer,
    search_web,
)

__all__ = [
    "extract_clean_search_query",
    "web_search",
    "google_search",
    "wikipedia_search",
    "web_quick_answer",
    "search_web",
]
