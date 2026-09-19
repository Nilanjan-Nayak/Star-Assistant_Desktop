"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — YouTube Search & Direct Media Playback
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict

from ..registry import register_tool


def resolve_youtube_play_url(query: str) -> str:
    """Resolve top YouTube video ID for a query to directly play it in browser."""
    encoded = urllib.parse.quote_plus(query.strip())
    search_url = f"https://www.youtube.com/results?search_query={encoded}"
    try:
        req = urllib.request.Request(
            search_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            video_ids = re.findall(r"/watch\?v=([a-zA-Z0-9_-]{11})", html)
            if video_ids:
                return f"https://www.youtube.com/watch?v={video_ids[0]}"
    except Exception as e:
        print(f"[YouTube Play] Direct video resolution error: {e}")

    return search_url


@register_tool(
    name="search_web",
    description="Search YouTube or Google, or play songs/videos on YouTube in the default browser."
)
def search_web(query: str, target: str = "youtube", play: bool = True) -> Dict[str, Any]:
    """Open YouTube or Google in browser. If target is youtube and play=True, directly plays top video."""
    encoded = urllib.parse.quote_plus(query.strip())
    if target.lower() == "youtube" or "youtube" in target.lower():
        if play:
            url = resolve_youtube_play_url(query)
        else:
            url = f"https://www.youtube.com/results?search_query={encoded}"
    else:
        url = f"https://www.google.com/search?q={encoded}"

    try:
        webbrowser.open(url)
        return {"searched": query, "url": url}
    except Exception as e:
        return {"searched": query, "error": str(e)}


@register_tool(
    name="play_media",
    description="Search and directly play a song, music, or video on YouTube."
)
def play_media(query: str) -> Dict[str, Any]:
    """Find and directly play a song or video on YouTube."""
    return search_web(query=query, target="youtube", play=True)


@register_tool(
    name="youtube_search_only",
    description="Search YouTube and open the search results page without auto-playing the first video."
)
def youtube_search_only(query: str) -> Dict[str, Any]:
    """Search YouTube without immediately auto-playing."""
    return search_web(query=query, target="youtube", play=False)

