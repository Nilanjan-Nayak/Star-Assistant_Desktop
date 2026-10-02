"""Browser tools — the browser agent's hands, registered as typed Phase 4 specs.

Everything the browser can do is a tool, which means everything it can do passes
validation → permissions → confirmation → audit → dry-run. There is no private
execution path for an agent (blueprint #2).

Reuse before duplication (blueprint #1): where the existing desktop tools can do
the job — ``web_read_page``, ``web_open_url``, ``web_search``, ``take_screenshot``
— their implementations are called directly and the audit entry names them in
``via``. Where they cannot load (this sandbox has no ``pyautogui`` and the legacy
browser-control module is Windows-only) the tool says so instead of pretending.

The stdlib fallbacks (:mod:`Backend.star.browser.extract`) exist so *reading* the
web works everywhere, and so the guardrails are applied on every hop.
"""

from __future__ import annotations

import urllib.parse
import webbrowser
from typing import Any, Callable

from Backend.star.browser.extract import fetch_url, read_page
from Backend.star.browser.guardrails import BrowserGuardrails, UrlVerdict
from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger
from Backend.star.tools.spec import ToolSpec

__all__ = ["BrowserToolkit", "register_browser_tools", "SEARCH_ENGINES"]

_log = star_logger("star2.browser.tools")

SEARCH_ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
}

#: legacy desktop tools we prefer when they are importable on this machine
_LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    "open": ("web_open_url",),
    "read": ("web_read_page",),
    "search": ("web_search", "search_web"),
    "close_tab": ("web_close_tab",),
    "screenshot": ("take_screenshot", "see_screen"),
    "click": ("web_click_element", "execute_computer_skill"),
    "type": ("web_type_text", "execute_computer_skill"),
}


class BrowserToolkit:
    """Holds the guardrails + the counters the browser tools share."""

    def __init__(
        self,
        settings: Settings,
        *,
        registry: Any = None,
        guard: BrowserGuardrails | None = None,
        bus: Any = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.browser
        self.registry = registry
        self.guard = guard or BrowserGuardrails(settings, bus=bus)
        self.bus = bus
        self.stats: dict[str, int] = {"calls": 0, "refused": 0, "legacy": 0, "fallback": 0, "unavailable": 0}

    # ── shared plumbing ───────────────────────────────────────────────────
    def _legacy(self, action: str) -> Callable[..., Any] | None:
        """Find a registered legacy handler for this action (reuse, don't rebuild)."""
        if self.registry is None:
            return None
        for name in _LEGACY_ALIASES.get(action, ()):
            spec = self.registry.get(name)
            handler = getattr(spec, "handler", None) if spec is not None else None
            if callable(handler):
                self.stats["legacy"] += 1
                return handler
        return None

    def _refuse(self, verdict: UrlVerdict) -> dict[str, Any]:
        self.stats["refused"] += 1
        return {
            "success": False,
            "error": f"blocked by browser guardrails ({verdict.rule}): {verdict.reason}",
            "result": {"verdict": verdict.public()},
        }

    def _guard(self, url: str) -> UrlVerdict:
        return self.guard.check_url(url)

    def describe(self) -> dict[str, Any]:
        return {"guardrails": self.guard.describe(), **self.stats}


# ─────────────────────────────────────────────────────────────────────────────
#  Handlers (sync — the executor runs them in a worker thread with a timeout)
# ─────────────────────────────────────────────────────────────────────────────


def _open_url(kit: BrowserToolkit, url: str) -> dict[str, Any]:
    kit.stats["calls"] += 1
    verdict = kit._guard(url)
    if not verdict.ok:
        return kit._refuse(verdict)

    legacy = kit._legacy("open")
    if legacy is not None:
        out = legacy(verdict.url)
        return _wrap(out, via="web_open_url", verdict=verdict)

    kit.stats["fallback"] += 1
    opened = webbrowser.open(verdict.url, new=2)
    return {
        "success": bool(opened),
        "result": {"url": verdict.url, "host": verdict.host, "opened": bool(opened), "via": "webbrowser"},
        **({} if opened else {"error": "no browser could be launched on this machine"}),
    }


def _read_url(kit: BrowserToolkit, url: str, max_chars: int = 0) -> dict[str, Any]:
    kit.stats["calls"] += 1
    verdict = kit._guard(url)
    if not verdict.ok:
        return kit._refuse(verdict)

    limit = int(max_chars or kit.cfg.max_chars)
    legacy = kit._legacy("read")
    if legacy is not None:
        try:
            out = legacy(verdict.url, limit)
        except TypeError:                       # legacy signature is (url, max_chars) — be forgiving
            out = legacy(verdict.url)
        wrapped = _wrap(out, via="web_read_page", verdict=verdict)
        result = wrapped.get("result")
        if isinstance(result, dict):
            if "content" in result and "text" not in result:
                result["text"] = result.pop("content")
            if "total_length" in result:
                result["text_chars"] = result.pop("total_length")
        return wrapped

    kit.stats["fallback"] += 1
    page = read_page(
        verdict.url,
        timeout_s=kit.cfg.timeout_s,
        max_bytes=kit.cfg.max_bytes,
        max_chars=limit,
        user_agent=kit.cfg.user_agent,
        guard=lambda candidate: kit.guard.check_url(candidate).ok,
    )
    if page.error:
        return {
            "success": False,
            "error": page.error,
            "result": {"url": verdict.url, "host": verdict.host, "status": page.status, "via": "star.extract"},
        }
    return {
        "success": True,
        "result": {
            "url": page.url or verdict.url,
            "host": verdict.host,
            "title": page.title,
            "description": page.description,
            "text": page.text[:limit],
            "word_count": page.word_count,
            "headings": list(page.headings),
            "link_count": len(page.links),
            "truncated": page.truncated,
            "status": page.status,
            "via": "star.extract",
        },
    }


def _list_links(kit: BrowserToolkit, url: str, limit: int = 20) -> dict[str, Any]:
    kit.stats["calls"] += 1
    verdict = kit._guard(url)
    if not verdict.ok:
        return kit._refuse(verdict)

    kit.stats["fallback"] += 1
    fetched = fetch_url(
        verdict.url,
        timeout_s=kit.cfg.timeout_s,
        max_bytes=kit.cfg.max_bytes,
        user_agent=kit.cfg.user_agent,
        guard=lambda candidate: kit.guard.check_url(candidate).ok,
    )
    if not fetched.ok:
        return {"success": False, "error": fetched.error, "result": {"url": verdict.url, "via": "star.extract"}}

    from Backend.star.browser.extract import extract_page

    page = extract_page(fetched.html, fetched.final_url or verdict.url)
    wanted = max(1, min(int(limit), 60))
    return {
        "success": True,
        "result": {
            "url": page.url,
            "title": page.title,
            "links": [link.public() for link in page.links[:wanted]],
            "link_count": len(page.links),
            "via": "star.extract",
        },
    }


def _search(kit: BrowserToolkit, query: str, engine: str = "google", open_browser: bool | None = None) -> dict[str, Any]:
    kit.stats["calls"] += 1
    text = (query or "").strip()
    if not text:
        return {"success": False, "error": "field 'query' is required"}

    legacy = kit._legacy("search")
    should_open = kit.cfg.auto_open if open_browser is None else bool(open_browser)
    if legacy is not None:
        try:
            out = legacy(text, engine, should_open)
        except TypeError:
            out = legacy(text)
        return _wrap(out, via="web_search")

    kit.stats["fallback"] += 1
    template = SEARCH_ENGINES.get((engine or "google").lower(), SEARCH_ENGINES["google"])
    search_url = template.format(q=urllib.parse.quote_plus(text))
    verdict = kit._guard(search_url)
    if not verdict.ok:
        return kit._refuse(verdict)
    opened = bool(should_open) and webbrowser.open(verdict.url, new=2)
    return {
        "success": True,
        "result": {
            "query": text,
            "engine": engine,
            "url": verdict.url,
            "opened": opened,
            "note": "no desktop search tool available — returning the search URL"
            if not opened
            else "opened in the default browser",
            "via": "star.search_url",
        },
    }


def _close_tab(kit: BrowserToolkit) -> dict[str, Any]:
    kit.stats["calls"] += 1
    legacy = kit._legacy("close_tab")
    if legacy is None:
        kit.stats["unavailable"] += 1
        return {"success": False, "error": "tab control needs the desktop browser tools (pyautogui) — not available here"}
    return _wrap(legacy(), via="web_close_tab")


def _snapshot(kit: BrowserToolkit, save_path: str = "") -> dict[str, Any]:
    kit.stats["calls"] += 1
    legacy = kit._legacy("screenshot")
    if legacy is None:
        kit.stats["unavailable"] += 1
        return {"success": False, "error": "screenshots need the desktop vision tools — not available here"}
    try:
        out = legacy(save_path) if save_path else legacy()
    except TypeError:
        out = legacy()
    return _wrap(out, via="take_screenshot")


def _click(kit: BrowserToolkit, selector: str) -> dict[str, Any]:
    kit.stats["calls"] += 1
    if not (selector or "").strip():
        return {"success": False, "error": "field 'selector' is required"}
    legacy = kit._legacy("click")
    if legacy is None:
        kit.stats["unavailable"] += 1
        return {
            "success": False,
            "error": "in-page clicking needs the desktop browser tools (pyautogui) — not available here",
        }
    return _wrap(legacy(selector), via="web_click_element")


def _type_text(kit: BrowserToolkit, selector: str, text: str) -> dict[str, Any]:
    kit.stats["calls"] += 1
    if not (text or "").strip():
        return {"success": False, "error": "field 'text' is required"}
    legacy = kit._legacy("type")
    if legacy is None:
        kit.stats["unavailable"] += 1
        return {
            "success": False,
            "error": "in-page typing needs the desktop browser tools (pyautogui) — not available here",
        }
    try:
        out = legacy(selector, text)
    except TypeError:
        out = legacy(text)
    return _wrap(out, via="web_type_text")


def _wrap(out: Any, *, via: str, verdict: UrlVerdict | None = None) -> dict[str, Any]:
    """Normalise a legacy return into the one shape every browser tool reports.

    Legacy tools are inconsistent: ``web_read_page`` returns a *flat* dict
    (``url``/``title``/``content``/``message``) while others nest a ``result``.
    Everything leaves here as ``{"success": bool, "result": {...}, "error"?, "message"?}``
    so the agent's OBSERVE step has exactly one shape to read, whichever
    implementation actually ran.
    """
    if isinstance(out, dict):
        ok = bool(out.get("success", out.get("ok", True)))
        error = out.get("error") if not ok else None
        inner = out.get("result")
        if isinstance(inner, dict):
            inner = dict(inner)
        else:
            inner = {k: v for k, v in out.items() if k not in {"success", "ok", "error", "result", "message"}}
            if isinstance(out.get("result"), (str, int, float, bool)):
                inner["value"] = out["result"]
        message = out.get("message") or out.get("response")
    else:
        ok, error, inner, message = bool(out), None, {"value": out}, None

    inner["via"] = via
    if verdict is not None:
        inner.setdefault("url", verdict.url)
        inner.setdefault("host", verdict.host)

    wrapped: dict[str, Any] = {"success": ok, "result": inner}
    if error:
        wrapped["error"] = str(error)
    if message:
        wrapped["message"] = str(message)
    return wrapped


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────


def register_browser_tools(
    registry: Any,
    settings: Settings | None = None,
    *,
    bus: Any = None,
    toolkit: BrowserToolkit | None = None,
) -> BrowserToolkit:
    """Add the browser tools to a :class:`StarToolRegistry` and return the toolkit."""
    cfg = settings or Settings()
    kit = toolkit or BrowserToolkit(cfg, registry=registry, bus=bus)
    kit.registry = registry

    specs = (
        ToolSpec(
            name="browser_open",
            description="Open a URL in the default browser after guardrail checks (http/https, public hosts only).",
            category="web",
            agent="browser",
            risk="medium",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http(s) URL to open"}},
                "required": ["url"],
            },
            handler=lambda url: _open_url(kit, str(url)),
            timeout_s=15.0,
            idempotent=False,
            reversible=True,
            dry_run_safe=True,
            origin="star2",
            tags=("browser", "phase5"),
            module="Backend.star.browser.tools",
        ),
        ToolSpec(
            name="browser_read",
            description="Fetch a page and return its title, description and readable text (no scripts, no binaries).",
            category="web",
            agent="browser",
            risk="low",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 0, "description": "0 = the configured cap"},
                },
                "required": ["url"],
            },
            handler=lambda url, max_chars=0: _read_url(kit, str(url), int(max_chars or 0)),
            timeout_s=max(cfg.browser.timeout_s + 4.0, 12.0),
            idempotent=True,
            reversible=True,
            dry_run_safe=True,
            origin="star2",
            tags=("browser", "phase5"),
            module="Backend.star.browser.tools",
        ),
        ToolSpec(
            name="browser_links",
            description="List the links on a page (absolute URLs + anchor text).",
            category="web",
            agent="browser",
            risk="low",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}, "limit": {"type": "integer", "default": 20}},
                "required": ["url"],
            },
            handler=lambda url, limit=20: _list_links(kit, str(url), int(limit or 20)),
            timeout_s=max(cfg.browser.timeout_s + 4.0, 12.0),
            idempotent=True,
            reversible=True,
            dry_run_safe=True,
            origin="star2",
            tags=("browser", "phase5"),
            module="Backend.star.browser.tools",
        ),
        ToolSpec(
            name="browser_search",
            description="Build (and optionally open) a web search for a query. Opening obeys STAR_BROWSER_AUTO_OPEN.",
            category="web",
            agent="browser",
            risk="low",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "engine": {"type": "string", "default": "google"},
                    "open_browser": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
            handler=lambda query, engine="google", open_browser=False: _search(kit, str(query), str(engine), bool(open_browser)),
            timeout_s=20.0,
            idempotent=True,
            reversible=True,
            dry_run_safe=True,
            origin="star2",
            tags=("browser", "search", "phase5"),
            module="Backend.star.browser.tools",
        ),
        ToolSpec(
            name="browser_close_tab",
            description="Close the current browser tab (desktop only).",
            category="web",
            agent="browser",
            risk="medium",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: _close_tab(kit),
            timeout_s=10.0,
            idempotent=False,
            reversible=False,               # a closed tab is gone
            dry_run_safe=True,
            origin="star2",
            tags=("browser", "phase5"),
            module="Backend.star.browser.tools",
        ),
        ToolSpec(
            name="browser_snapshot",
            description="Take a screenshot of the browser window for verification (desktop only).",
            category="vision",
            agent="browser",
            risk="low",
            parameters={
                "type": "object",
                "properties": {"save_path": {"type": "string", "default": ""}},
                "required": [],
            },
            handler=lambda save_path="": _snapshot(kit, str(save_path or "")),
            timeout_s=20.0,
            idempotent=True,
            reversible=True,
            dry_run_safe=True,
            origin="star2",
            tags=("browser", "vision", "phase5"),
            module="Backend.star.browser.tools",
        ),
        ToolSpec(
            name="browser_click",
            description="Click an element in the focused browser page (desktop only, needs explicit approval).",
            category="web",
            agent="browser",
            risk="high",
            parameters={
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "visible text or CSS-ish selector"}},
                "required": ["selector"],
            },
            handler=lambda selector: _click(kit, str(selector)),
            timeout_s=15.0,
            idempotent=False,
            reversible=False,
            dry_run_safe=False,              # never pretend a click happened
            origin="star2",
            tags=("browser", "input", "phase5"),
            module="Backend.star.browser.tools",
        ),
        ToolSpec(
            name="browser_type",
            description="Type text into the focused browser field (desktop only, needs explicit approval).",
            category="web",
            agent="browser",
            risk="high",
            parameters={
                "type": "object",
                "properties": {"selector": {"type": "string", "default": ""}, "text": {"type": "string"}},
                "required": ["text"],
            },
            handler=lambda text, selector="": _type_text(kit, str(selector or ""), str(text)),
            timeout_s=15.0,
            idempotent=False,
            reversible=True,
            dry_run_safe=False,
            origin="star2",
            tags=("browser", "input", "phase5"),
            module="Backend.star.browser.tools",
        ),
    )

    for spec in specs:
        # the registry announces every registration on the bus (one rich event per tool)
        registry.register(spec, replace=True)
    _log.info("star2.browser.tools.registered count=%s", len(specs))
    return kit
