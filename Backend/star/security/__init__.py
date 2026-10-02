"""STAR 2.0 security layer (Phase 11).

Consolidates:
* Audit trails with active redaction (AuditLog)
* Explicit human confirmation gate (ConfirmationStore)
* Permission engine & policy evaluation (PermissionEngine / SecurityPolicyEngine)
* Secret vault and active credential scanner (SecretVault / SecretScanner)
* Operator identity & role validation (OperatorIdentity / Role)
* Action budgets and rate limits (SecurityBudget)
* Emergency stop circuit breaker
"""

from __future__ import annotations

from Backend.star.security.audit import AuditLog, fingerprint
from Backend.star.security.budgets import SecurityBudget
from Backend.star.security.confirmations import Confirmation, ConfirmationStore
from Backend.star.security.identity import OperatorIdentity, Role
from Backend.star.security.policy import SecurityPolicyEngine
from Backend.star.security.secrets import (
    SecretScanner,
    SecretVault,
    contains_secrets,
    redact_secrets,
)
from Backend.star.security.surface import SecuritySurface, build_security
from Backend.star.security.tools import register_security_tools

__all__ = [
    "AuditLog",
    "Confirmation",
    "ConfirmationStore",
    "OperatorIdentity",
    "Role",
    "SecretScanner",
    "SecretVault",
    "SecurityBudget",
    "SecurityPolicyEngine",
    "SecuritySurface",
    "build_security",
    "contains_secrets",
    "fingerprint",
    "redact_secrets",
    "register_security_tools",
]
