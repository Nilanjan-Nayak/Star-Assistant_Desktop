"""STAR 2.0 observability — events, metrics, structured logs, tracing.

Thin adapters over ``agent.core`` primitives (metrics/tracing/logging) so there is
exactly one metrics registry and one correlation-id context in the process.
"""

from __future__ import annotations

from Backend.star.observability.events import (
    EventPhase,
    StarEvent,
    StarEventBus,
    get_event_bus,
    new_event,
    reset_event_bus,
)
from Backend.star.observability.metrics import StarMetrics, get_metrics
from Backend.star.observability.tracing import correlation_id, span, task_span

__all__ = [
    "EventPhase",
    "StarEvent",
    "StarEventBus",
    "StarMetrics",
    "correlation_id",
    "get_event_bus",
    "get_metrics",
    "new_event",
    "reset_event_bus",
    "span",
    "task_span",
]
