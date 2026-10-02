"""Latency + reliability metrics for STAR 2.0, layered on ``agent.core.metrics``.

The agent package already owns the process-wide ``METRICS`` registry, so this
module only *adds* Star-level counters (gateway, plan, task, tool, voice) and a
small ``timer`` context manager. No second registry — that would be a duplicate
subsystem.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from agent.core.metrics import METRICS

__all__ = ["StarMetrics", "get_metrics"]


class StarMetrics:
    """Namespace wrapper: ``get_metrics().inc("task.completed", agent="browser")``."""

    def __init__(self, registry: Any = METRICS) -> None:
        self._registry = registry

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        self._registry.inc(f"star.{name}", value, **labels)

    def observe(self, name: str, value: float, **labels: str) -> None:
        self._registry.observe(f"star.{name}", value, **labels)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        if hasattr(self._registry, "gauge"):
            self._registry.gauge(f"star.{name}", value, **labels)
        else:  # pragma: no cover - older agent cores
            self._registry.observe(f"star.{name}", value, **labels)

    @contextmanager
    def timer(self, name: str, **labels: str) -> Iterator[dict[str, float]]:
        """``with metrics.timer("plan.build") as out: ... ; out["ms"]``"""
        holder: dict[str, float] = {}
        started = time.perf_counter()
        try:
            yield holder
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            holder["ms"] = elapsed_ms
            self.observe(f"{name}.ms", elapsed_ms, **labels)

    def snapshot(self) -> dict[str, Any]:
        """``agent.core.metrics`` returns ``{counters, gauges, histograms}``; keep
        the same shape but split Star-level series from the low-level ones."""
        raw = self._registry.snapshot()
        star: dict[str, Any] = {}
        other: dict[str, Any] = {}
        for group, entries in raw.items():
            if not isinstance(entries, dict):
                other[str(group)] = entries
                continue
            star_group = {k: v for k, v in entries.items() if str(k).startswith("star.")}
            other_group = {k: v for k, v in entries.items() if not str(k).startswith("star.")}
            if star_group:
                star[str(group)] = star_group
            if other_group:
                other[str(group)] = other_group
        return {"star": star, "agent": other}


_METRICS: StarMetrics | None = None


def get_metrics() -> StarMetrics:
    global _METRICS
    if _METRICS is None:
        _METRICS = StarMetrics()
    return _METRICS
