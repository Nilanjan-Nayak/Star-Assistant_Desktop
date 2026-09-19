"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — YouTube Trending & Analytics
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..registry import register_tool

_API_KEY: Optional[str] = None
try:
    import os
    _API_KEY = os.environ.get("YOUTUBE_API_KEY")
except Exception:
    pass

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_CATEGORY_MAP = {
    "default": "",
    "music": "10",
    "gaming": "20",
    "news": "25",
    "movies": "30",
    "sports": "17",
    "education": "27",
    "science": "28",
    "entertainment": "24",
    "comedy": "23",
}


def _fetch_trending_api(category: str = "default", region: str = "IN",
                        max_results: int = 10) -> Optional[List[Dict[str, str]]]:
    if not _API_KEY:
        return None
    cat_id = _CATEGORY_MAP.get(category.lower(), "")
    params = {
        "part": "snippet,statistics",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": str(max_results),
        "key": _API_KEY,
    }
    if cat_id:
        params["videoCategoryId"] = cat_id
    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        videos = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            videos.append({
                "title": snippet.get("title", "Unknown"),
                "channel": snippet.get("channelTitle", "Unknown"),
                "url": f"https://www.youtube.com/watch?v={item['id']}",
                "views": stats.get("viewCount", "N/A"),
                "category": category,
            })
        return videos if videos else None
    except Exception as e:
        print(f"[YT Analytics] API error: {e}")
        return None


def _fetch_trending_scrape(category: str = "default",
                           max_results: int = 10) -> Optional[List[Dict[str, str]]]:
    cat_map_url = {
        "music": "https://www.youtube.com/feed/trending?bp=4gINGgt5dG1hX2NoYXJ0cw%3D%3D",
        "gaming": "https://www.youtube.com/gaming",
        "movies": "https://www.youtube.com/feed/trending?bp=4gIcGhpnYW1pbmdfY29ycHVzX21vc3RfcG9wdWxhcg%3D%3D",
        "default": "https://www.youtube.com/feed/trending",
    }
    url = cat_map_url.get(category.lower(), cat_map_url["default"])
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "bn-IN,bn;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        videos = []
        vid_pattern = re.findall(
            r'"videoRenderer":\s*\{[^}]*"videoId":\s*"([a-zA-Z0-9_-]{11})".*?"text":\s*"([^"]{3,120})"',
            html
        )
        seen = set()
        for vid, title in vid_pattern:
            if vid not in seen:
                seen.add(vid)
                videos.append({
                    "title": title,
                    "channel": "",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "views": "N/A",
                    "category": category,
                })
            if len(videos) >= max_results:
                break
        return videos if videos else None
    except Exception as e:
        print(f"[YT Analytics] Scrape error: {e}")
        return None


@register_tool(
    name="youtube_get_trending",
    description="Get a list of currently trending YouTube videos. Categories: music, gaming, news, movies, sports."
)
def youtube_get_trending(category: str = "default", max_results: int = 10) -> Dict[str, Any]:
    videos = _fetch_trending_api(category, max_results=max_results)
    if not videos:
        videos = _fetch_trending_scrape(category, max_results=max_results)

    if not videos:
        return {"success": False, "error": "YouTube trending ডেটা আনতে পারিনি।", "videos": []}

    summary_parts = []
    for i, v in enumerate(videos[:5], 1):
        channel_info = f" — {v['channel']}" if v.get("channel") else ""
        summary_parts.append(f"{i}. {v['title']}{channel_info}")

    summary = "\n".join(summary_parts)
    cat_label = category if category != "default" else "সব ক্যাটাগরি"
    return {
        "success": True,
        "videos": videos,
        "count": len(videos),
        "category": category,
        "summary": summary,
        "message": f"এখন YouTube-এ {cat_label} ট্রেন্ডিং ভিডিও:\n{summary}",
    }


@register_tool(
    name="youtube_trending_music",
    description="Get currently trending music videos on YouTube."
)
def youtube_trending_music(max_results: int = 10) -> Dict[str, Any]:
    return youtube_get_trending(category="music", max_results=max_results)


@register_tool(
    name="youtube_analyse",
    description="Analyse what's trending on YouTube right now across top categories."
)
def youtube_analyse() -> Dict[str, Any]:
    results = {}
    for cat in ["default", "music", "gaming"]:
        trending = youtube_get_trending(category=cat, max_results=5)
        if trending.get("success"):
            results[cat] = trending.get("videos", [])

    if not any(results.values()):
        return {"success": False, "error": "YouTube ট্রেন্ডিং ডেটা আনতে পারিনি।"}

    parts = []
    if results.get("default"):
        parts.append("🔥 **Top Trending:**")
        for i, v in enumerate(results["default"][:3], 1):
            parts.append(f"  {i}. {v['title']}")

    if results.get("music"):
        parts.append("\n🎵 **Trending Music:**")
        for i, v in enumerate(results["music"][:3], 1):
            parts.append(f"  {i}. {v['title']}")

    full_msg = "\n".join(parts)
    return {"success": True, "analysis": results, "message": f"YouTube-এ এখন যা ট্রেন্ড করছে:\n{full_msg}"}


@register_tool(
    name="youtube_search_channel",
    description="Search and open a specific channel directly on YouTube."
)
def youtube_search_channel(channel_name: str) -> Dict[str, Any]:
    """Search for a YouTube channel."""
    try:
        import webbrowser
        encoded = urllib.parse.quote_plus(channel_name.strip())
        url = f"https://www.youtube.com/results?search_query={encoded}&sp=EgIQAg%253D%253D"
        webbrowser.open(url)
        return {"success": True, "channel": channel_name, "url": url, "message": f"YouTube চ্যানেল '{channel_name}' খুলে দিয়েছি!"}
    except Exception as e:
        return {"success": False, "error": str(e)}

