"""Composable health checks for backends, clocks, and optional extras."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agent.core.enums import HealthStatus

type HealthProbe = Callable[[], HealthStatus]


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: HealthStatus
    checks: dict[str, HealthStatus]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "checks": {name: status.value for name, status in self.checks.items()},
        }


class HealthRegistry:
    def __init__(self) -> None:
        self._probes: dict[str, HealthProbe] = {}

    def register(self, name: str, probe: HealthProbe) -> None:
        if not name:
            raise ValueError("health check name must be non-empty")
        self._probes[name] = probe

    def snapshot(self) -> HealthReport:
        checks: dict[str, HealthStatus] = {}
        worst = HealthStatus.OK
        order = {HealthStatus.OK: 0, HealthStatus.DEGRADED: 1, HealthStatus.FAIL: 2}
        for name, probe in self._probes.items():
            try:
                status = probe()
            except Exception:
                status = HealthStatus.FAIL
            checks[name] = status
            if order[status] > order[worst]:
                worst = status
        if not checks:
            worst = HealthStatus.DEGRADED
        return HealthReport(status=worst, checks=checks)


HEALTH: HealthRegistry = HealthRegistry()
