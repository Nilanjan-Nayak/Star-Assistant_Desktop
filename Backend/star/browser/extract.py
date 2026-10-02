"""Fetching and reading web pages with the standard library only.

Two jobs, kept separate so tests can exercise them independently:

* :func:`fetch_url` — bounded HTTP GET (timeout, byte cap, redirect cap, and a
  guard callback re-checked on *every* redirect so a public URL cannot bounce
  the agent into ``169.254.169.254`` or ``localhost``).
* :func:`extract_page` — turn HTML into a small, safe, text-only observation
  (title, description, headings, readable text, links). No scripts, no styles,
  no binaries, no third-party parser dependency.

Everything here is blocking I/O; callers run it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable

__all__ = [
    "FetchResult",
    "PageContent",
    "PageLink",
    "extract_page",
    "fetch_url",
    "read_page",
]

_CHUNK = 64 * 1024
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "canvas"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "li", "tr", "td", "th", "br", "hr",
        "ul", "ol", "table", "blockquote", "pre", "figure", "figcaption", "header", "footer",
    }
)
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_MAX_LINKS = 60


@dataclass(frozen=True, slots=True)
class PageLink:
    url: str
    text: str = ""

    def public(self) -> dict[str, Any]:
        return {"url": self.url, "text": self.text}


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What came back from the wire — or why nothing did."""

    ok: bool
    status: int = 0
    final_url: str = ""
    content_type: str = ""
    html: str = ""
    bytes_read: int = 0
    elapsed_ms: float = 0.0
    truncated: bool = False
    redirects: int = 0
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "bytes_read": self.bytes_read,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "truncated": self.truncated,
            "redirects": self.redirects,
            "error": self.error,
            "chars": len(self.html),
        }


@dataclass(frozen=True, slots=True)
class PageContent:
    """A text-only observation of one page — this is what the agent 'sees'."""

    url: str = ""
    title: str = ""
    description: str = ""
    text: str = ""
    headings: tuple[str, ...] = ()
    links: tuple[PageLink, ...] = ()
    word_count: int = 0
    truncated: bool = False
    status: int = 0
    error: str = ""
    elapsed_ms: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "text_chars": len(self.text),
            "word_count": self.word_count,
            "headings": list(self.headings[:20]),
            "links": [link.public() for link in self.links[:_MAX_LINKS]],
            "truncated": self.truncated,
            "status": self.status,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }

    def as_text(self, max_chars: int = 4000) -> str:
        head = self.title or self.url
        body = self.text[:max_chars]
        return f"{head}\n\n{body}".strip() if head else body


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Count redirects and re-run the guard on every hop."""

    def __init__(self, guard: Callable[[str], bool], max_redirects: int) -> None:
        super().__init__()
        self._guard = guard
        self._max = max_redirects
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 — stdlib signature
        self.hops += 1
        if self.hops > self._max:
            raise urllib.error.HTTPError(req.full_url, code, f"too many redirects (>{self._max})", headers, fp)
        if not self._guard(newurl):
            raise urllib.error.HTTPError(req.full_url, code, f"redirect to a refused url: {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(
    url: str,
    *,
    timeout_s: float = 8.0,
    max_bytes: int = 2_000_000,
    user_agent: str = "StarAssistant/2.0",
    max_redirects: int = 3,
    guard: Callable[[str], bool] | None = None,
) -> FetchResult:
    """Bounded GET. Never raises — every failure mode becomes ``FetchResult.ok=False``."""
    started = time.monotonic()
    check: Callable[[str], bool] = guard or (lambda _url: True)
    handler = _GuardedRedirectHandler(check, max_redirects)
    opener = urllib.request.build_opener(handler, urllib.request.HTTPSHandler())
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "bn,en;q=0.8",
            "Connection": "close",
        },
    )
    try:
        with opener.open(request, timeout=timeout_s) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            charset = _charset(content_type)
            chunks: list[bytes] = []
            read = 0
            truncated = False
            while True:
                piece = response.read(min(_CHUNK, max(1, max_bytes - read)))
                if not piece:
                    break
                chunks.append(piece)
                read += len(piece)
                if read >= max_bytes:
                    truncated = response.read(1) != b""
                    break
            body = b"".join(chunks)
            return FetchResult(
                ok=True,
                status=int(getattr(response, "status", 200) or 200),
                final_url=response.geturl(),
                content_type=content_type,
                html=body.decode(charset, errors="replace"),
                bytes_read=read,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                truncated=truncated,
                redirects=handler.hops,
            )
    except urllib.error.HTTPError as exc:
        return FetchResult(
            ok=False, status=int(exc.code or 0), final_url=url,
            error=f"http {exc.code}: {exc.reason}", redirects=handler.hops,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return FetchResult(
            ok=False, final_url=url, error=f"{type(exc).__name__}: {reason}",
            redirects=handler.hops, elapsed_ms=(time.monotonic() - started) * 1000.0,
        )


def _charset(content_type: str) -> str:
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip().strip('"') or "utf-8"
    return "utf-8"


class _PageParser(HTMLParser):
    """Collects title / description / headings / text / links. Ignores markup noise."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.description = ""
        self.headings: list[str] = []
        self.links: list[PageLink] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._heading: str | None = None
        self._heading_text: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    # -- structure ---------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag in _HEADING_TAGS:
            self._heading = tag
            self._heading_text = []
        elif tag == "a":
            href = attributes.get("href", "").strip()
            self._anchor_href = href or None
            self._anchor_text = []
        elif tag == "meta":
            name = attributes.get("name", "").lower()
            prop = attributes.get("property", "").lower()
            content = attributes.get("content", "").strip()
            if content and not self.description and (
                name in ("description", "twitter:description") or prop in ("og:description",)
            ):
                self.description = content
        elif tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == self._heading:
            text = _collapse("".join(self._heading_text))
            if text:
                self.headings.append(text)
            self._heading = None
        elif tag == "a" and self._anchor_href:
            absolute = urllib.parse.urljoin(self.base_url, self._anchor_href)
            if absolute.lower().startswith(("http://", "https://")) and len(self.links) < _MAX_LINKS:
                self.links.append(PageLink(url=absolute, text=_collapse("".join(self._anchor_text))[:120]))
            self._anchor_href = None
        elif tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._heading is not None:
            self._heading_text.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        self.text_parts.append(data)


def extract_page(html: str, base_url: str = "", *, max_chars: int = 20_000) -> PageContent:
    """HTML → :class:`PageContent`. Tolerates broken markup; never raises."""
    parser = _PageParser(base_url)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as exc:  # noqa: BLE001 — malformed pages must not kill a run
        return PageContent(url=base_url, error=f"parse error: {type(exc).__name__}: {exc}")

    text = _collapse(parser_text(parser), keep_newlines=True)[:max_chars]
    words = len(text.split())
    seen: set[str] = set()
    links: list[PageLink] = []
    for link in parser.links:
        if link.url in seen:
            continue
        seen.add(link.url)
        links.append(link)
    return PageContent(
        url=base_url,
        title=_collapse("".join(parser.title_parts))[:200],
        description=_collapse(parser.description)[:400],
        text=text,
        headings=tuple(dict.fromkeys(parser.headings))[:20],
        links=tuple(links[:_MAX_LINKS]),
        word_count=words,
        truncated=len(text) >= max_chars,
    )


def parser_text(parser: _PageParser) -> str:
    return "".join(parser.text_parts)


def read_page(
    url: str,
    *,
    timeout_s: float = 8.0,
    max_bytes: int = 2_000_000,
    max_chars: int = 4000,
    user_agent: str = "StarAssistant/2.0",
    guard: Callable[[str], bool] | None = None,
) -> PageContent:
    """Fetch + extract in one call (blocking — run it in a thread)."""
    fetched = fetch_url(url, timeout_s=timeout_s, max_bytes=max_bytes, user_agent=user_agent, guard=guard)
    if not fetched.ok:
        return PageContent(url=url, status=fetched.status, error=fetched.error, elapsed_ms=fetched.elapsed_ms)
    page = extract_page(fetched.html, fetched.final_url or url, max_chars=max_chars)
    return PageContent(
        url=page.url,
        title=page.title,
        description=page.description,
        text=page.text,
        headings=page.headings,
        links=page.links,
        word_count=page.word_count,
        truncated=page.truncated or fetched.truncated,
        status=fetched.status,
        elapsed_ms=fetched.elapsed_ms,
        extras={"content_type": fetched.content_type, "bytes_read": fetched.bytes_read, "redirects": fetched.redirects},
    )


def _collapse(text: str, *, keep_newlines: bool = False) -> str:
    if keep_newlines:
        lines = [" ".join(line.split()) for line in (text or "").splitlines()]
        out: list[str] = []
        for line in lines:
            if line:
                out.append(line)
            elif out and out[-1]:
                out.append("")
        return "\n".join(out).strip()
    return " ".join((text or "").split())
