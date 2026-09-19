"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — YouTube Tools Package
═══════════════════════════════════════════════════════════════════════════════

  Dedicated package for all YouTube features:
    - control.py   : High-accuracy playback control (play, pause, speed, HD, etc.)
    - history.py   : SQLite watch history tracking and replay
    - analytics.py : Trending videos, trending music, and trend analysis
    - search.py    : YouTube video search and direct playback
═══════════════════════════════════════════════════════════════════════════════
"""

from .history import record_youtube_play
from . import control, history, analytics, search

