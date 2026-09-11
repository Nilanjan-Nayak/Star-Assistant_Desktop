"""Formal safety invariants — pure predicates plus an explicit ``commit``.

``check`` must not mutate counters (so a later failing invariant does not
consume budget). ``commit`` is called only after every invariant accepted
the spec. This was a v5 bug: ``TotalBudgetInvariant.check`` incremented
even when a subsequent invariant raised.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Protocol

from agent.core.clock import DEFAULT_CLOCK, Clock
from agent.core.errors import BudgetExhausted, SafetyViolation
from agent.core.ids import CapToken
from agent.motor.spec import ActionSpec, action_coord, action_payload_text
from agent.safety.capability import CapabilityGrant
from agent.safety.config import GovernorConfig


class Invariant(Protocol):
    name: str

    def check(
        self,
        spec: ActionSpec,
        capability: CapToken | None,
        grants: dict[CapToken, CapabilityGrant],
    ) -> None: ...

    def commit(self, spec: ActionSpec) -> None: ...


@dataclass
class TotalBudgetInvariant:
    """INV-1: total committed actions ≤ max_total_actions."""

    config: GovernorConfig
    name: str = "total_budget"
    _count: int = 0

    def check(
        self,
        spec: ActionSpec,
        capability: CapToken | None,
        grants: dict[CapToken, CapabilityGrant],
    ) -> None:
        if self._count >= self.config.max_total_actions:
            raise BudgetExhausted(
                "total action budget exhausted",
                limit=self.config.max_total_actions,
                used=self._count,
            )

    def commit(self, spec: ActionSpec) -> None:
        self._count += 1

    @property
    def used(self) -> int:
        return self._count


@dataclass
class RateLimitInvariant:
    """INV-2: committed rate over trailing 60s ≤ limit."""

    config: GovernorConfig
    clock: Clock = field(default_factory=lambda: DEFAULT_CLOCK)
    name: str = "rate_limit"
    _recent: Deque[float] = field(default_factory=lambda: deque(maxlen=1000))

    def check(
        self,
        spec: ActionSpec,
        capability: CapToken | None,
        grants: dict[CapToken, CapabilityGrant],
    ) -> None:
        now = self.clock.monotonic()
        rate = sum(1 for t in self._recent if t > now - 60.0)
        if rate >= self.config.max_actions_per_minute:
            raise SafetyViolation(
                "rate limit exceeded",
                rate=rate,
                limit=self.config.max_actions_per_minute,
            )

    def commit(self, spec: ActionSpec) -> None:
        self._recent.append(self.clock.monotonic())


@dataclass
class WallClockInvariant:
    """INV-3: episode elapsed time ≤ max_wall_clock_seconds."""

    config: GovernorConfig
    clock: Clock = field(default_factory=lambda: DEFAULT_CLOCK)
    name: str = "wall_clock"
    _started_at: float | None = None

    def begin_episode(self) -> None:
        self._started_at = self.clock.monotonic()

    def check(
        self,
        spec: ActionSpec,
        capability: CapToken | None,
        grants: dict[CapToken, CapabilityGrant],
    ) -> None:
        if self._started_at is None:
            return
        elapsed = self.clock.monotonic() - self._started_at
        if elapsed > self.config.max_wall_clock_seconds:
            raise BudgetExhausted("wall clock exhausted", elapsed=elapsed)

    def commit(self, spec: ActionSpec) -> None:
        return


@dataclass
class ForbiddenRegionInvariant:
    """INV-4: click coordinates avoid all forbidden regions."""

    config: GovernorConfig
    name: str = "forbidden_region"

    def check(
        self,
        spec: ActionSpec,
        capability: CapToken | None,
        grants: dict[CapToken, CapabilityGrant],
    ) -> None:
        coord = action_coord(spec)
        if coord is None:
            return
        for region in self.config.forbidden_regions:
            if region.contains(coord):
                raise SafetyViolation(
                    "coordinate in forbidden region",
                    coord=coord.as_tuple(),
                    region=region.as_tuple(),
                )

    def commit(self, spec: ActionSpec) -> None:
        return


@dataclass
class KeywordBlocklistInvariant:
    """INV-5: text/key payload contains no forbidden keyword."""

    config: GovernorConfig
    name: str = "keyword_blocklist"

    def check(
        self,
        spec: ActionSpec,
        capability: CapToken | None,
        grants: dict[CapToken, CapabilityGrant],
    ) -> None:
        payload = action_payload_text(spec).lower()
        if not payload:
            return
        for kw in self.config.forbidden_keywords:
            if kw in payload:
                raise SafetyViolation(
                    f"forbidden keyword {kw!r}",
                    action=spec.kind.value,
                )

    def commit(self, spec: ActionSpec) -> None:
        return


@dataclass
class CapabilityInvariant:
    """INV-6: if required, a valid capability grant covers the action."""

    config: GovernorConfig
    name: str = "capability"

    def check(
        self,
        spec: ActionSpec,
        capability: CapToken | None,
        grants: dict[CapToken, CapabilityGrant],
    ) -> None:
        if not self.config.require_capability:
            return
        grant = grants.get(capability) if capability is not None else None
        if grant is None or not grant.permits(spec.kind):
            raise SafetyViolation("capability required", needed=spec.kind.value)

    def commit(self, spec: ActionSpec) -> None:
        return
