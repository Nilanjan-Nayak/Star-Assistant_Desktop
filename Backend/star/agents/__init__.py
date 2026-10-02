"""STAR 2.0 agents — goal-oriented workers (blueprint §4).

*"Agents are goal-oriented workers… Tools are capabilities. The orchestrator
sequences agents."*  Phase 5 adds the shared spine (:mod:`.base`) and the first
real worker, the browser agent (which lives in :mod:`Backend.star.browser`
because the blueprint gives the browser its own package).

Every export is lazy (PEP 562): importing ``Backend.star.agents`` must not drag
in the browser stack — or, later, pyautogui for the computer agent.
"""

from __future__ import annotations

__all__ = [
    "AgentRegistry",
    "AgentRun",
    "AgentRunState",
    "AgentStep",
    "AgentStepPlan",
    "BrowserAgent",
    "StarAgent",
    "build_browser",
]

_LAZY = {
    "StarAgent": ("Backend.star.agents.base", "StarAgent"),
    "AgentRegistry": ("Backend.star.agents.base", "AgentRegistry"),
    "AgentRun": ("Backend.star.agents.base", "AgentRun"),
    "AgentRunState": ("Backend.star.agents.base", "AgentRunState"),
    "AgentStep": ("Backend.star.agents.base", "AgentStep"),
    "AgentStepPlan": ("Backend.star.agents.base", "AgentStepPlan"),
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
