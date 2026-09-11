"""SafetyGovernor — enforces all invariants, then commits counters."""

from __future__ import annotations

import json
from pathlib import Path

from agent.core.errors import SafetyViolation
from agent.core.events import BUS, SafetyBlockedEvent
from agent.core.ids import CapToken
from agent.motor.result import ActionResult
from agent.motor.spec import ActionSpec
from agent.safety.capability import CapabilityGrant
from agent.safety.config import GovernorConfig
from agent.safety.human import Approver
from agent.safety.invariants import (
    CapabilityInvariant,
    ForbiddenRegionInvariant,
    Invariant,
    KeywordBlocklistInvariant,
    RateLimitInvariant,
    TotalBudgetInvariant,
    WallClockInvariant,
)


class SafetyGovernor:
    def __init__(
        self,
        config: GovernorConfig | None = None,
        invariants: list[Invariant] | None = None,
        approver: Approver | None = None,
    ) -> None:
        self.config = config or GovernorConfig()
        self._wall_clock = WallClockInvariant(self.config)
        self.invariants: list[Invariant] = invariants or [
            TotalBudgetInvariant(self.config),
            RateLimitInvariant(self.config),
            self._wall_clock,
            ForbiddenRegionInvariant(self.config),
            KeywordBlocklistInvariant(self.config),
            CapabilityInvariant(self.config),
        ]
        self._grants: dict[CapToken, CapabilityGrant] = {}
        self._approver = approver
        self._audit: list[dict[str, object]] = []

    def set_approver(self, approver: Approver) -> None:
        self._approver = approver

    def grant(self, capability: CapabilityGrant) -> None:
        self._grants[capability.token] = capability

    def begin_episode(self) -> None:
        self._wall_clock.begin_episode()

    async def check(
        self,
        spec: ActionSpec,
        capability: CapToken | None = None,
    ) -> None:
        for inv in self.invariants:
            try:
                inv.check(spec, capability, self._grants)
            except SafetyViolation as sv:
                if self._approver is not None:
                    approved = await self._approver.approve(f"[{inv.name}] {sv}. Approve?", spec)
                    if approved:
                        continue
                await BUS.publish(
                    SafetyBlockedEvent(reason=inv.name, action=spec.kind.value)
                )
                raise
        for inv in self.invariants:
            inv.commit(spec)

    def log(self, result: ActionResult) -> None:
        self._audit.append(result.model_dump(mode="json"))

    def dump_audit(self, path: Path | None = None) -> Path:
        target = path or Path(self.config.audit_path)
        with target.open("w", encoding="utf-8") as handle:
            for entry in self._audit:
                handle.write(json.dumps(entry, default=str) + "\n")
        return target

    @property
    def audit_log(self) -> tuple[dict[str, object], ...]:
        return tuple(self._audit)
