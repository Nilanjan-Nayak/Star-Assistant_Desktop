"""
Backward-compatibility shim.
Tools have moved to Backend/tools/media/youtube_control.py
"""

from .media.youtube_control import (
    youtube_play_pause,
    youtube_next,
    youtube_previous,
    youtube_set_speed,
    youtube_set_quality,
    youtube_fullscreen,
    youtube_mute_video,
    youtube_seek_forward,
    youtube_seek_rewind,
    youtube_toggle_captions,
    youtube_close_tab,
)

__all__ = [
    "youtube_play_pause",
    "youtube_next",
    "youtube_previous",
    "youtube_set_speed",
    "youtube_set_quality",
    "youtube_fullscreen",
    "youtube_mute_video",
    "youtube_seek_forward",
    "youtube_seek_rewind",
    "youtube_toggle_captions",
    "youtube_close_tab",
]
