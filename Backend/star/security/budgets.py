"""Action budgets and rate limiters for Phase 11 security.

Prevents unbounded agent execution loops, resource exhaustion, and
runaway automated motor/web calls (Blueprint §11: budgets).
"""

from __future__ import annotations

from collections import deque
import time
from typing import Any

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger

_log = star_logger("security.budgets")


class SecurityBudget:
    """Session action budgeting and rolling-window rate limiting."""

    def __init__(self, settings: Settings, *, bus: StarEventBus | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self.max_per_session = settings.security.max_actions_per_session
        self.max_per_minute = settings.security.max_actions_per_minute
        self._session_counts: dict[str, int] = {}
        self._action_timestamps: deque[float] = deque()

    def check(self, session_id: str = "") -> tuple[bool, str]:
        """Verify whether an action is permitted under current budget."""
        sid = session_id or self.settings.session_id
        session_used = self._session_counts.get(sid, 0)
        if session_used >= self.max_per_session:
            return False, f"session action budget exhausted ({session_used}/{self.max_per_session})"

        # Clean old timestamps (> 60s)
        now = time.monotonic()
        while self._action_timestamps and (now - self._action_timestamps[0]) > 60.0:
            self._action_timestamps.popleft()

        if len(self._action_timestamps) >= self.max_per_minute:
            return False, f"per-minute rate limit reached ({len(self._action_timestamps)}/{self.max_per_minute})"

        return True, "ok"

    def consume(self, session_id: str = "") -> tuple[bool, str]:
        """Consume one action from the budget."""
        allowed, reason = self.check(session_id)
        if not allowed:
            if self.bus is not None:
                self.bus.emit(
                    "security.budget_exceeded",
                    phase=EventPhase.SECURITY,
                    session_id=session_id,
                    reason=reason,
                )
            return False, reason

        sid = session_id or self.settings.session_id
        self._session_counts[sid] = self._session_counts.get(sid, 0) + 1
        self._action_timestamps.append(time.monotonic())
        return True, "ok"

    def reset(self, session_id: str = "") -> None:
        """Reset budget counters."""
        if session_id:
            self._session_counts.pop(session_id, None)
        else:
            self._session_counts.clear()
            self._action_timestamps.clear()

    def usage(self, session_id: str = "") -> dict[str, Any]:
        sid = session_id or self.settings.session_id
        return {
            "session_id": sid,
            "session_actions": self._session_counts.get(sid, 0),
            "max_per_session": self.max_per_session,
            "actions_last_minute": len(self._action_timestamps),
            "max_per_minute": self.max_per_minute,
        }
