"""Capability tokens — must be presented to perform sensitive actions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent.core.enums import ActionKind
from agent.core.ids import CapToken, new_cap_token


class CapabilityGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    token: CapToken
    allowed: frozenset[ActionKind]
    expires_at: datetime
    granted_by: StrictStr = "user"
    note: StrictStr = ""

    def is_valid(self, *, now: datetime | None = None) -> bool:
        clock = now or datetime.now(timezone.utc)
        return clock < self.expires_at

    def permits(self, kind: ActionKind, *, now: datetime | None = None) -> bool:
        return self.is_valid(now=now) and kind in self.allowed


def mint_grant(
    allowed: frozenset[ActionKind],
    *,
    ttl_seconds: float = 300.0,
    granted_by: str = "user",
    note: str = "",
) -> CapabilityGrant:
    return CapabilityGrant(
        token=new_cap_token(),
        allowed=allowed,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        granted_by=granted_by,
        note=note,
    )
