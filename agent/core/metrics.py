"""In-memory Prometheus-style metrics with percentile summaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Final

_MAX_HISTOGRAM_SAMPLES: Final[int] = 10_000


class Metrics:
    def __init__(self, *, max_histogram_samples: int = _MAX_HISTOGRAM_SAMPLES) -> None:
        self._counters: dict[str, float] = defaultdict(float)
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._gauges: dict[str, float] = {}
        self._max_histogram_samples = max_histogram_samples

    @staticmethod
    def _key(name: str, labels: Mapping[str, str]) -> str:
        if not labels:
            return name
        joined = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{joined}}}"

    def inc(self, name: str, value: float = 1.0, /, **labels: str) -> None:
        if value < 0:
            raise ValueError("counter increments must be non-negative")
        self._counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, /, **labels: str) -> None:
        bucket = self._histograms[self._key(name, labels)]
        bucket.append(value)
        overflow = len(bucket) - self._max_histogram_samples
        if overflow > 0:
            del bucket[:overflow]

    def gauge(self, name: str, value: float, /, **labels: str) -> None:
        self._gauges[self._key(name, labels)] = value

    def snapshot(self) -> dict[str, object]:
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": {k: self._summarize(v) for k, v in self._histograms.items()},
        }

    def reset(self) -> None:
        self._counters.clear()
        self._histograms.clear()
        self._gauges.clear()

    @staticmethod
    def _summarize(values: list[float]) -> dict[str, float]:
        if not values:
            return {"count": 0.0, "sum": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
        ordered = sorted(values)
        n = len(ordered)
        total = sum(ordered)
        return {
            "count": float(n),
            "sum": total,
            "avg": total / n,
            "p50": ordered[n // 2],
            "p95": ordered[min(n - 1, int(n * 0.95))],
            "p99": ordered[min(n - 1, int(n * 0.99))],
        }


METRICS: Metrics = Metrics()
