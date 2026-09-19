"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — Web Content Reader & Page Summarizer
═══════════════════════════════════════════════════════════════════════════════

  Safely extracts clean readable text and metadata from web URLs:
    - HTML strip without heavy third-party dependencies
    - Article title, paragraph extraction
    - Max length truncation for TTS reading
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict

from ..registry import register_tool

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._pieces = []
        self._ignore = False
        self._title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header", "noscript"):
            self._ignore = True
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header", "noscript"):
            self._ignore = False
        elif tag == "title":
            self._in_title = False
        elif tag in ("p", "h1", "h2", "h3", "li", "br"):
            self._pieces.append("\n")

    def handle_data(self, data):
        if self._in_title and not self._title:
            self._title = data.strip()
        if not self._ignore:
            text = data.strip()
            if text:
                self._pieces.append(text + " ")

    def get_text(self) -> str:
        raw = "".join(self._pieces)
        return re.sub(r"\n\s*\n+", "\n\n", raw).strip()

    def get_title(self) -> str:
        return self._title


@register_tool(
    name="web_read_page",
    description="Fetch and extract readable text and title from a webpage URL."
)
def web_read_page(url: str, max_chars: int = 1500) -> Dict[str, Any]:
    """Fetch article text from a URL for assistant reading/summarizing."""
    target_url = url.strip()
    if not target_url.startswith(("http://", "https://")):
        target_url = f"https://{target_url}"

    try:
        req = urllib.request.Request(
            target_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "bn,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return {"success": False, "error": f"Unsupported content type: {content_type}"}
            charset = resp.headers.get_content_charset() or "utf-8"
            html_bytes = resp.read()
            html_text = html_bytes.decode(charset, errors="ignore")

        parser = _TextExtractor()
        parser.feed(html_text)
        title = parser.get_title()
        full_text = parser.get_text()

        truncated = full_text[:max_chars].strip()
        if len(full_text) > max_chars:
            truncated += "..."

        return {
            "success": True,
            "url": target_url,
            "title": title,
            "content": truncated,
            "total_length": len(full_text),
            "message": f"ওয়েব পেজ '{title}' থেকে তথ্য আনা হয়েছে।"
        }
    except Exception as e:
        return {"success": False, "url": target_url, "error": str(e)}
