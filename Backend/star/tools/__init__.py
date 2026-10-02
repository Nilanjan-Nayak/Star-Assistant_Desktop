"""STAR 2.0 tool layer — typed specs, registry, permissions, executor.

Phase 4: the legacy ``Backend/tools`` implementations are *wrapped*, never
rewritten. Each gains a :class:`ToolSpec` (category, owning agent, risk tier,
JSON-schema parameters, timeout, reversibility) so the planner, the permission
engine, the audit log and the UI can reason about capabilities without importing
their implementations.

    from Backend.star.tools import build_tool_executor, StarToolRegistry
"""

from __future__ import annotations

from Backend.star.tools.permissions import Decision, PermissionDecision, PermissionEngine
from Backend.star.tools.registry import StarToolRegistry, build_tool_registry
from Backend.star.tools.risk import (
    RISK_KEYWORDS,
    RISK_ORDER,
    classify_risk,
    default_risk,
    highest,
    risk_at_least,
    risk_rank,
)
from Backend.star.tools.spec import (
    PREFIX_AGENT,
    TOOL_AGENT,
    TOOL_CATEGORY,
    ToolCategory,
    ToolResult,
    ToolSpec,
    agent_for_tool,
    category_for_tool,
    spec_from_function,
    validate_arguments,
)

__all__ = [
    "PREFIX_AGENT",
    "RISK_KEYWORDS",
    "RISK_ORDER",
    "TOOL_AGENT",
    "TOOL_CATEGORY",
    "Decision",
    "PermissionDecision",
    "PermissionEngine",
    "StarToolRegistry",
    "ToolCategory",
    "ToolExecutor",
    "ToolResult",
    "ToolSpec",
    "agent_for_tool",
    "build_tool_executor",
    "build_tool_registry",
    "category_for_tool",
    "classify_risk",
    "default_risk",
    "highest",
    "risk_at_least",
    "risk_rank",
    "spec_from_function",
    "validate_arguments",
]

#: ``tools.executor`` imports ``brain.planning`` (task-state maths) and
#: ``brain.planning`` imports ``tools.risk``/``tools.spec``. Exposing the executor
#: lazily (PEP 562) keeps ``from Backend.star.tools import build_tool_executor``
#: working without an import cycle.
_LAZY = {"ToolExecutor": "executor", "build_tool_executor": "executor"}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        module = importlib.import_module(f"Backend.star.tools.{_LAZY[name]}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
