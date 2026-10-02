"""STAR 2.0 browser package (Phase 5).

Four pieces, deliberately separate so each can be tested on its own:

* :mod:`.guardrails` — the URL policy (schemes, private hosts, allow/deny lists,
  ports, optional DNS check). Nothing reaches the network without a verdict.
* :mod:`.extract` — bounded stdlib fetching + text-only page extraction.
* :mod:`.tools` — the browser capabilities registered as typed Phase 4 tool
  specs, so they inherit validation, permissions, confirmation, audit and
  dry-run. Legacy desktop tools are reused when they are importable.
* :mod:`.agent` — :class:`BrowserAgent`, the goal-oriented worker that plans
  bounded steps and runs the OBSERVE → ACT → VERIFY → RECOVER loop.

Exports are lazy (PEP 562) — importing the package must stay cheap.
"""

from __future__ import annotations

__all__ = [
    "BrowserAgent",
    "BrowserGuardrails",
    "BrowserToolkit",
    "FetchResult",
    "PageContent",
    "UrlVerdict",
    "build_browser",
    "extract_page",
    "fetch_url",
    "find_urls",
    "read_page",
    "register_browser_tools",
]

_LAZY = {
    "BrowserGuardrails": ("Backend.star.browser.guardrails", "BrowserGuardrails"),
    "UrlVerdict": ("Backend.star.browser.guardrails", "UrlVerdict"),
    "find_urls": ("Backend.star.browser.guardrails", "find_urls"),
    "FetchResult": ("Backend.star.browser.extract", "FetchResult"),
    "PageContent": ("Backend.star.browser.extract", "PageContent"),
    "extract_page": ("Backend.star.browser.extract", "extract_page"),
    "fetch_url": ("Backend.star.browser.extract", "fetch_url"),
    "read_page": ("Backend.star.browser.extract", "read_page"),
    "BrowserToolkit": ("Backend.star.browser.tools", "BrowserToolkit"),
    "register_browser_tools": ("Backend.star.browser.tools", "register_browser_tools"),
    "BrowserAgent": ("Backend.star.browser.agent", "BrowserAgent"),
    "build_browser": ("Backend.star.browser.agent", "build_browser"),
}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        module_name, attribute = _LAZY[name]
        return getattr(importlib.import_module(module_name), attribute)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
