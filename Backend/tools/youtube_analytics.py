"""
Backward-compatibility shim.
Tools have moved to Backend/tools/media/youtube_analytics.py
"""

from .media.youtube_analytics import (
    youtube_get_trending,
    youtube_trending_music,
    youtube_analyse,
)

__all__ = [
    "youtube_get_trending",
    "youtube_trending_music",
    "youtube_analyse",
]
