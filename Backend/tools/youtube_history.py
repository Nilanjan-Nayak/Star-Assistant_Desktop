"""
Backward-compatibility shim.
Tools have moved to Backend/tools/media/youtube_history.py
"""

from .media.youtube_history import (
    record_youtube_play,
    youtube_get_history,
    youtube_play_last,
    youtube_play_from_history,
    youtube_open_history_page,
    youtube_clear_local_history,
)

__all__ = [
    "record_youtube_play",
    "youtube_get_history",
    "youtube_play_last",
    "youtube_play_from_history",
    "youtube_open_history_page",
    "youtube_clear_local_history",
]
