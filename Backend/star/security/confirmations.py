"""Human confirmations for sensitive actions (the "explicit yes" gate).

Blueprint §2 non-negotiable #7 ("explicit confirmation for sensitive actions") and
§7 Phase 11. Phase 4 needs the store so the tool executor can *stop* before a
risky call; Phase 11's policy engine decides **when** to ask, using the same store.

Confirmations are matched to calls by a stable fingerprint
(:func:`~Backend.star.security.audit.fingerprint` of tool + canonical args +
session), expire after ``STAR_CONFIRMATION_TTL_S``, and can only be resolved once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.security.audit import fingerprint

__all__ = ["Confirmation", "ConfirmationStore"]

_log = star_logger("confirmations")

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"


@dataclass
class Confirmation:
    confirmation_id: str
    tool: str
    risk: str
    reason: str
    fingerprint: str
    session_id: str = ""
    request_id: str = ""
    plan_id: str = ""
    task_id: str = ""
    call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    state: str = PENDING
    note: str = ""
    resolved_at: float | None = None
    result: dict[str, Any] = field(default_factory=dict)

    def expired(self, now: float | None = None) -> bool:
        return self.state == PENDING and self.expires_at > 0 and (now or time.time()) > self.expires_at

    def public(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "tool": self.tool,
            "risk": self.risk,
            "reason": self.reason,
            "arguments": self.arguments,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "call_id": self.call_id,
            "state": self.state,
            "note": self.note,
            "created_at": round(self.created_at, 3),
            "expires_at": round(self.expires_at, 3),
            "ttl_remaining_s": max(0.0, round(self.expires_at - time.time(), 1)) if self.state == PENDING else 0.0,
            "resolved_at": self.resolved_at,
            "result": self.result,
        }


class ConfirmationStore:
    """TTL-bound pending approvals with an optional on-resolve callback."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: StarEventBus | None = None,
        ttl_s: float | None = None,
        on_resolve: Callable[[Confirmation, bool], Any] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus or StarEventBus()
        self.ttl_s = ttl_s if ttl_s is not None else float(getattr(getattr(settings, "security", None), "confirmation_ttl_s", 120.0))
        self.on_resolve = on_resolve
        self._id_factory = id_factory
        self._items: dict[str, Confirmation] = {}
        self._by_fingerprint: dict[str, str] = {}
        self.stats = {"requested": 0, "approved": 0, "denied": 0, "expired": 0, "reused": 0, "unknown": 0}

    # ── creation ──────────────────────────────────────────────────────────
    def request(
        self,
        *,
        tool: str,
        arguments: dict[str, Any] | None = None,
        risk: str = "high",
        reason: str = "",
        session_id: str = "",
        request_id: str = "",
        plan_id: str = "",
        task_id: str = "",
        call_id: str = "",
        ttl_s: float | None = None,
    ) -> Confirmation:
        """Create (or reuse) a pending confirmation for one exact call."""
        args = dict(arguments or {})
        stamp = fingerprint(tool, args, session_id)
        self.purge_expired()
        existing_id = self._by_fingerprint.get(stamp)
        if existing_id and existing_id in self._items:
            existing = self._items[existing_id]
            if existing.state == PENDING:
                self.stats["reused"] += 1
                return existing

        confirmation_id = self._make_id(tool)
        now = time.time()
        confirmation = Confirmation(
            confirmation_id=confirmation_id,
            tool=str(tool),
            risk=str(risk),
            reason=reason or f"'{tool}' is a {risk}-risk action",
            fingerprint=stamp,
            session_id=session_id,
            request_id=request_id,
            plan_id=plan_id,
            task_id=task_id,
            call_id=call_id,
            arguments=args,
            created_at=now,
            expires_at=now + (ttl_s if ttl_s is not None else self.ttl_s),
        )
        self._items[confirmation_id] = confirmation
        self._by_fingerprint[stamp] = confirmation_id
        self.stats["requested"] += 1
        self.bus.emit(
            "security.confirmation_requested",
            phase=EventPhase.SECURITY,
            confirmation_id=confirmation_id,
            tool=tool,
            risk=risk,
            reason=confirmation.reason,
            session_id=session_id,
            request_id=request_id,
            task_id=task_id,
            ttl_s=round(confirmation.expires_at - now, 1),
        )
        return confirmation

    def _make_id(self, tool: str) -> str:
        if self._id_factory is not None:
            return str(self._id_factory(tool))
        try:
            from agent.core.ids import new_id

            return new_id("conf")
        except Exception:  # noqa: BLE001
            return f"conf_{int(time.time() * 1000) % 10**8:08d}"

    # ── lookup ────────────────────────────────────────────────────────────
    def get(self, confirmation_id: str) -> Confirmation | None:
        item = self._items.get(str(confirmation_id or ""))
        if item is not None and item.expired():
            self._expire(item)
        return item

    def pending(self) -> list[dict[str, Any]]:
        self.purge_expired()
        return [item.public() for item in self._items.values() if item.state == PENDING]

    def approved_token(self, tool: str, arguments: dict[str, Any] | None = None, session_id: str = "") -> str | None:
        """Return the confirmation id when this exact call was already approved."""
        stamp = fingerprint(tool, arguments or {}, session_id)
        confirmation_id = self._by_fingerprint.get(stamp)
        if not confirmation_id:
            return None
        item = self._items.get(confirmation_id)
        if item is None:
            return None
        if item.state == APPROVED and not (item.expires_at and time.time() > item.expires_at):
            return item.confirmation_id
        return None

    def _expire(self, item: Confirmation) -> None:
        item.state = EXPIRED
        item.resolved_at = time.time()
        self.stats["expired"] += 1
        self.bus.emit(
            "security.confirmation_expired",
            phase=EventPhase.SECURITY,
            confirmation_id=item.confirmation_id,
            tool=item.tool,
            session_id=item.session_id,
        )

    def purge_expired(self) -> int:
        purged = 0
        for item in list(self._items.values()):
            if item.expired():
                self._expire(item)
                purged += 1
        return purged

    # ── resolution ────────────────────────────────────────────────────────
    def resolve(self, confirmation_id: str, *, approve: bool, note: str = "") -> dict[str, Any]:
        item = self.get(confirmation_id)
        if item is None:
            self.stats["unknown"] += 1
            return {"ok": False, "error": f"unknown confirmation {confirmation_id!r}"}
        if item.state != PENDING:
            return {"ok": False, "error": f"confirmation already {item.state}", "state": item.state}
        item.state = APPROVED if approve else DENIED
        item.note = str(note or "")
        item.resolved_at = time.time()
        self.stats["approved" if approve else "denied"] += 1
        self.bus.emit(
            "security.confirmation_resolved",
            phase=EventPhase.SECURITY,
            confirmation_id=item.confirmation_id,
            tool=item.tool,
            approved=bool(approve),
            note=item.note,
            session_id=item.session_id,
            request_id=item.request_id,
        )
        callback_result: Any = None
        if approve and self.on_resolve is not None:
            try:
                callback_result = self.on_resolve(item, True)
            except Exception as exc:  # noqa: BLE001
                _log.warning("on_resolve callback failed: %s", exc)
                callback_result = {"ok": False, "error": repr(exc)}
        payload = {"ok": True, "state": item.state, "confirmation": item.public()}
        if callback_result is not None:
            payload["execution"] = callback_result
        return payload

    def mark_approved(self, confirmation_id: str, *, note: str = "") -> dict[str, Any]:
        """Record an approval that was granted elsewhere (no ``on_resolve`` callback).

        Used by the tool executor when a human ``yes`` arrives through
        ``SecuritySurface.resolve`` — the store must agree before policy re-checks
        the call, and firing the callback again would recurse.
        """
        item = self.get(confirmation_id)
        if item is None:
            self.stats["unknown"] += 1
            return {"ok": False, "error": f"unknown confirmation {confirmation_id!r}"}
        if item.state != PENDING:
            return {"ok": True, "state": item.state}
        item.state = APPROVED
        item.note = str(note or "")
        item.resolved_at = time.time()
        self.stats["approved"] += 1
        self.bus.emit(
            "security.confirmation_resolved",
            phase=EventPhase.SECURITY,
            confirmation_id=item.confirmation_id,
            tool=item.tool,
            approved=True,
            note=item.note,
            via="executor",
            session_id=item.session_id,
            request_id=item.request_id,
        )
        return {"ok": True, "state": item.state}

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "ttl_s": self.ttl_s,
            "tracked": len(self._items),
            "pending": len([item for item in self._items.values() if item.state == PENDING]),
            "has_callback": self.on_resolve is not None,
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "detail": self.describe()}
