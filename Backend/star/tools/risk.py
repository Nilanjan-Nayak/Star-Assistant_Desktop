"""Risk tiers shared by the tool registry, the planner and the policy engine.

Blueprint §7 Phase 4 ("typed tool registry with permissions, risk levels and
audit trails") and Phase 11 ("risk tiers: Low → Critical").

Moved here from ``brain/planning.py`` so the dependency points the right way
(``brain → tools``), and re-exported from ``brain.planning`` for compatibility.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = ["RISK_KEYWORDS", "RISK_ORDER", "classify_risk", "default_risk", "highest", "risk_at_least", "risk_rank"]

#: blueprint order: Low → Medium → High → Critical (``unknown`` sorts lowest).
RISK_ORDER: tuple[str, ...] = ("unknown", "low", "medium", "high", "critical")

#: keyword → risk, checked most dangerous first. Conservative on purpose:
#: anything unrecognised is ``medium`` (visible, audited, not auto-confirmed).
RISK_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("format", "wipe", "delete_all", "purge", "destroy", "registry", "disable_security", "rm_rf"), "critical"),
    (
        (
            "shutdown",
            "restart",
            "reboot",
            "poweroff",
            "sleep_system",
            "hibernate",
            "lock",
            "delete",
            "remove",
            "kill",
            "terminate",
            "uninstall",
            "push",
            "force",
            "sudo",
            "autonomous",
            "computer_skill",
        ),
        "high",
    ),
    (
        (
            "set_",
            "adjust_",
            "launch",
            "close_",
            "open_",
            "play",
            "write",
            "create",
            "move",
            "rename",
            "remember",
            "clear",
            "mute",
            "send",
            "install",
        ),
        "medium",
    ),
    (
        (
            "get_",
            "list_",
            "read_",
            "find_",
            "search",
            "recall",
            "calculate",
            "status",
            "metrics",
            "screenshot",
            "see_",
            "history",
            "answer",
            "describe",
            "check",
        ),
        "low",
    ),
)


def risk_rank(risk: str) -> int:
    """Position in :data:`RISK_ORDER`; unknown values rank as ``medium``."""
    try:
        return RISK_ORDER.index(str(risk).lower())
    except ValueError:
        return RISK_ORDER.index("medium")


def risk_at_least(risk: str, threshold: str) -> bool:
    """``True`` when ``risk`` is at or above ``threshold``."""
    return risk_rank(risk) >= risk_rank(threshold)


def classify_risk(tool: str, arguments: Mapping[str, Any] | None = None) -> str:
    """Keyword-based risk estimate.

    This is the *default*. A registered :class:`~Backend.star.tools.spec.ToolSpec`
    carries an explicit risk that always wins, and Phase 11's policy engine can
    override both (safety level, session state, argument inspection).
    """
    # NOTE: only the tool name and argument *keys* are inspected. Free-text
    # argument values are user content — "search how to delete files" must not
    # look like a destructive action.
    haystack = f"{tool or ''} {' '.join(str(key) for key in (arguments or {}))}".lower()
    for keywords, risk in RISK_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return risk
    return "medium"


#: backwards-compatible alias used by the brain's planner
default_risk = classify_risk


def highest(risks: Iterable[str], default: str = "low") -> str:
    """The most dangerous risk in an iterable (empty → ``default``)."""
    values = [str(risk) for risk in risks]
    if not values:
        return default
    return max(values, key=risk_rank)
