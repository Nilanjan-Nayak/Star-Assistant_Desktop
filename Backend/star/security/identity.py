"""Operator identity and role-based capability gating for Phase 11.

Enforces operator identity, owner privileges (Blueprint: Nilanjan),
and ensures guests or automated processes cannot bypass confirmation gates.
"""

from __future__ import annotations

import enum
from typing import Any

from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger

_log = star_logger("security.identity")


@enum.unique
class Role(str, enum.Enum):
    OWNER = "owner"
    OPERATOR = "operator"
    GUEST = "guest"
    SYSTEM = "system"


class OperatorIdentity:
    """Manages identity validation and role-based permissions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.owner_name = settings.owner or "Nilanjan"
        self._tokens: dict[str, Role] = {}

    def register_token(self, token: str, role: Role) -> None:
        """Register an authentication token with a role."""
        if token:
            self._tokens[token] = role

    def get_role(self, identifier: str = "", token: str = "") -> Role:
        """Determine role from identifier or token."""
        if token and token in self._tokens:
            return self._tokens[token]
        ident = (identifier or "").strip().lower()
        if not ident or ident == self.owner_name.lower() or ident == "owner":
            return Role.OWNER
        if ident in ("guest", "anonymous"):
            return Role.GUEST
        return Role.OPERATOR

    def is_owner(self, identifier: str = "", token: str = "") -> bool:
        """Check if caller has owner privilege."""
        return self.get_role(identifier, token) is Role.OWNER

    def can_execute_tier(self, role: Role, risk: str) -> bool:
        """Role-based execution permissions."""
        r = str(risk).lower()
        if role is Role.OWNER:
            return True
        if role is Role.SYSTEM:
            return r in ("low", "medium")
        if role is Role.OPERATOR:
            return r in ("low", "medium", "high")
        # GUEST
        return r == "low"

    def can_resolve_confirmation(self, role: Role, risk: str) -> bool:
        """Guests cannot approve confirmations; only operators/owners can."""
        if role in (Role.OWNER, Role.OPERATOR):
            return True
        return False

    def describe(self) -> dict[str, Any]:
        return {
            "owner": self.owner_name,
            "registered_tokens": len(self._tokens),
            "safety_level": self.settings.security.safety_level,
        }
