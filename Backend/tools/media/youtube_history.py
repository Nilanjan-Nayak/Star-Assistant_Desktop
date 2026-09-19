"""Forwarding shim to Backend/tools/youtube/history.py"""
from ..youtube.history import (
    record_youtube_play,
    youtube_get_history,
    youtube_play_last,
    youtube_play_from_history,
    youtube_open_history_page,
    youtube_clear_local_history,
)
