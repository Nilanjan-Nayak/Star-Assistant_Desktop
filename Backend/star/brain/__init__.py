"""STAR 2.0 brain — schemas, context, reasoning, planning, prediction, reflection.

Public surface (Phase 3):

    from Backend.star.brain import StarBrain, build_brain, Plan, Task, ToolCall

Only :mod:`Backend.star.brain.schemas` is imported eagerly — it has no heavy
dependencies and both the tool layer and the voice layer import it. Everything
else is resolved lazily (PEP 562) so ``brain → tools → brain`` stays acyclic and
importing the brain never pulls in optional legacy pieces (LLM providers, the
command router, the SQLite memory store).
"""

from __future__ import annotations

from Backend.star.brain.schemas import (
    AgentName,
    Context,
    MemoryHit,
    Plan,
    Prediction,
    RawAction,
    Reasoning,
    Reflection,
    Task,
    TaskResult,
    TaskState,
    ToolCall,
    ToolCallState,
    UserRequest,
    Verification,
)

_LAZY: dict[str, str] = {
    # context
    "ContextBuilder": "brain.context",
    "MemoryRetriever": "brain.context",
    "NullRetriever": "brain.context",
    "StoreRetriever": "brain.context",
    "time_of_day": "brain.context",
    # pipeline
    "HONEST_NOTES": "brain.pipeline",
    "StarBrain": "brain.pipeline",
    "build_brain": "brain.pipeline",
    "open_memory_store": "brain.pipeline",
    # planning
    "StarPlanner": "brain.planning",
    "task_state_from_calls": "brain.planning",
    # prediction
    "SAFE_SEED_PATTERNS": "brain.prediction",
    "HistoryPredictor": "brain.prediction",
    "NullPredictor": "brain.prediction",
    "PatternStore": "brain.prediction",
    "Predictor": "brain.prediction",
    # reflection
    "NullReflector": "brain.reflection",
    "Reflector": "brain.reflection",
    "StarReflector": "brain.reflection",
    "call_succeeded": "brain.reflection",
    # reasoning
    "FallbackReasoner": "brain.reasoning",
    "ProviderReasoner": "brain.reasoning",
    "Reasoner": "brain.reasoning",
    "ReasoningCascade": "brain.reasoning",
    "RouterReasoner": "brain.reasoning",
    "build_reasoner": "brain.reasoning",
    "registered_tools": "brain.reasoning",
    "sanitize_actions": "brain.reasoning",
    "sanitize_proposed_calls": "brain.reasoning",
    # re-exported from the tool layer (single source of truth)
    "PREFIX_AGENT": "tools.spec",
    "RISK_KEYWORDS": "tools.risk",
    "RISK_ORDER": "tools.risk",
    "TOOL_AGENT": "tools.spec",
    "agent_for_tool": "tools.spec",
    "classify_risk": "tools.risk",
    "default_risk": "tools.risk",
    "highest": "tools.risk",
    "risk_at_least": "tools.risk",
    "risk_rank": "tools.risk",
}

__all__ = [
    "AgentName",
    "Context",
    "ContextBuilder",
    "FallbackReasoner",
    "HONEST_NOTES",
    "HistoryPredictor",
    "MemoryHit",
    "MemoryRetriever",
    "NullPredictor",
    "NullReflector",
    "NullRetriever",
    "PREFIX_AGENT",
    "PatternStore",
    "Plan",
    "Prediction",
    "Predictor",
    "ProviderReasoner",
    "RISK_KEYWORDS",
    "RISK_ORDER",
    "RawAction",
    "Reasoner",
    "Reasoning",
    "ReasoningCascade",
    "Reflection",
    "Reflector",
    "RouterReasoner",
    "SAFE_SEED_PATTERNS",
    "StarBrain",
    "StarPlanner",
    "StarReflector",
    "StoreRetriever",
    "TOOL_AGENT",
    "Task",
    "TaskResult",
    "TaskState",
    "ToolCall",
    "ToolCallState",
    "UserRequest",
    "Verification",
    "agent_for_tool",
    "build_brain",
    "build_reasoner",
    "call_succeeded",
    "classify_risk",
    "default_risk",
    "highest",
    "open_memory_store",
    "registered_tools",
    "risk_at_least",
    "risk_rank",
    "sanitize_actions",
    "sanitize_proposed_calls",
    "task_state_from_calls",
    "time_of_day",
]


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"Backend.star.{target}")
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
