"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — YouTube Watch History Manager
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import datetime
import sqlite3
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..registry import register_tool

_DB_PATH = Path(__file__).resolve().parents[3] / "star_memory.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS youtube_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL DEFAULT '',
    url        TEXT    NOT NULL,
    query      TEXT    NOT NULL DEFAULT '',
    played_at  TEXT    NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


def record_youtube_play(url: str, title: str = "", query: str = "") -> bool:
    """Record played YouTube video into local history."""
    try:
        conn = _get_conn()
        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO youtube_history (title, url, query, played_at) VALUES (?, ?, ?, ?)",
            (title, url, query, now),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[YT History] Record error: {e}")
        return False


@register_tool(
    name="youtube_get_history",
    description="Retrieve the user's local YouTube watch history."
)
def youtube_get_history(limit: int = 10) -> Dict[str, Any]:
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT title, url, query, played_at FROM youtube_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()

        history = [
            {"title": title or query or "Unknown", "url": url, "query": query, "played_at": played_at}
            for title, url, query, played_at in rows
        ]
        return {"success": True, "history": history, "count": len(history)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_play_last",
    description="Replay the last YouTube video that was played."
)
def youtube_play_last() -> Dict[str, Any]:
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT title, url, query FROM youtube_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()

        if not row:
            return {"success": False, "message": "কোনো YouTube history নেই বন্ধু!"}

        title, url, query = row
        webbrowser.open(url)
        display = title or query or url
        return {"success": True, "title": display, "url": url, "message": f"আগেরবার দেখা '{display}' আবার চালিয়ে দিয়েছি!"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_play_from_history",
    description="Play a specific video from watch history by position (1 = most recent, 2 = second most recent, etc.)."
)
def youtube_play_from_history(position: int = 1) -> Dict[str, Any]:
    try:
        idx = max(1, position)
        conn = _get_conn()
        rows = conn.execute(
            "SELECT title, url, query FROM youtube_history ORDER BY id DESC LIMIT ? OFFSET ?",
            (1, idx - 1),
        ).fetchall()
        conn.close()

        if not rows:
            return {"success": False, "error": f"History-তে {idx} নম্বর video নেই।"}

        title, url, query = rows[0]
        webbrowser.open(url)
        display = title or query or url
        return {"success": True, "title": display, "url": url, "position": idx, "message": f"History থেকে '{display}' চালিয়ে দিয়েছি!"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_open_history_page",
    description="Open the YouTube watch history page in the browser."
)
def youtube_open_history_page() -> Dict[str, Any]:
    try:
        webbrowser.open("https://www.youtube.com/feed/history")
        return {"success": True, "action": "opened_youtube_history_page"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_clear_local_history",
    description="Clear all locally stored YouTube watch history."
)
def youtube_clear_local_history() -> Dict[str, Any]:
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM youtube_history")
        conn.commit()
        conn.close()
        return {"success": True, "message": "Local YouTube history cleared."}
    except Exception as e:
        return {"success": False, "error": str(e)}
