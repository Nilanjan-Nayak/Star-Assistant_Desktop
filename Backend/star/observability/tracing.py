"""Tracing helpers — reuse the agent's correlation-id context and span stack.

STAR 2.0 adds one thing the low-level core does not have: a *task-level* span
that carries the Star task id, so a gateway request can be followed through
brain → orchestrator → agent → tool in one trace.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from agent.core.logging import current_correlation
from agent.core.tracing import span as _agent_span

__all__ = ["correlation_id", "span", "task_span"]


def correlation_id() -> str:
    """Current correlation id ("" when unbound)."""
    try:
        return str(current_correlation() or "")
    except Exception:  # noqa: BLE001 — older cores may not expose the getter
        return ""


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    with _agent_span(name, **attrs) as ctx:
        yield ctx


@contextmanager
def task_span(task_id: str, agent: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    with _agent_span(f"star.task.{agent}", task_id=task_id, **attrs) as ctx:
        yield ctx
