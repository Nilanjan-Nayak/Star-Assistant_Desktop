"""The STAR 2.0 Security Surface (Phase 11).

Consolidates:
* Audit trail with active redaction (AuditLog)
* Explicit human confirmation gate (ConfirmationStore)
* Permission engine and safety-level escalation (PermissionEngine / SecurityPolicyEngine)
* Secret vault and credential scanner (SecretVault / SecretScanner)
* Operator identity & role validation (OperatorIdentity)
* Action budgets and rate limits (SecurityBudget)
* Emergency stop circuit breaker
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.security.audit import AuditLog
from Backend.star.security.budgets import SecurityBudget
from Backend.star.security.confirmations import ConfirmationStore
from Backend.star.security.identity import OperatorIdentity, Role
from Backend.star.security.policy import SecurityPolicyEngine
from Backend.star.security.secrets import SecretScanner, SecretVault
from Backend.star.tools.spec import ToolSpec

if TYPE_CHECKING:
    from Backend.star.tools.permissions import PermissionDecision, PermissionEngine

__all__ = ["SecuritySurface", "build_security"]

_log = star_logger("security")


class SecuritySurface:
    """The central security coordinator for STAR 2.0."""

    name = "security"

    def __init__(
        self,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
        audit: AuditLog | None = None,
        confirmations: ConfirmationStore | None = None,
        permissions: PermissionEngine | None = None,
        executor: Any = None,
    ) -> None:
        self.settings = settings
        self.bus = bus or StarEventBus()
        self._stopped = False

        self.audit = audit if audit is not None else AuditLog(settings.security.audit_path, bus=self.bus)
        self.confirmations = confirmations if confirmations is not None else ConfirmationStore(settings, bus=self.bus)
        if permissions is not None:
            self.permissions = permissions
        else:
            from Backend.star.tools.permissions import PermissionEngine
            self.permissions = PermissionEngine(
                settings, bus=self.bus, confirmations=self.confirmations, stop_gate=lambda: self._stopped
            )
        if self.permissions._stop_gate is None:
            self.permissions.bind_stop_gate(lambda: self._stopped)
        self.executor = executor

        # Phase 11 extensions
        self.vault = SecretVault()
        self.scanner = SecretScanner()
        self.identity = OperatorIdentity(settings)
        self.budgets = SecurityBudget(settings, bus=self.bus)
        self.policy = SecurityPolicyEngine(
            settings,
            bus=self.bus,
            confirmations=self.confirmations,
            identity=self.identity,
            budgets=self.budgets,
            scanner=self.scanner,
            stop_gate=lambda: self._stopped,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def startup(self) -> None:
        self.bus.emit(
            "security.ready",
            phase=EventPhase.SECURITY,
            dry_run=self.settings.security.dry_run,
            safety_level=self.settings.security.safety_level,
            confirm_above_risk=self.settings.security.confirm_above_risk,
            deny_risk=self.settings.security.deny_risk,
            allow_shell=self.settings.security.allow_shell,
            audit_path=str(self.audit.path),
            owner=self.settings.owner,
        )
        _log.info("star2.security.ready dry_run=%s safety_level=%s", self.settings.security.dry_run, self.settings.security.safety_level)

    async def aclose(self) -> None:
        return None

    # ── emergency stop circuit breaker ────────────────────────────────────
    def mute(self, *, reason: str = "user") -> dict[str, Any]:
        """Emergency stop: refuse all actions across all layers."""
        self._stopped = True
        self.audit.record(
            {"tool": "*", "decision": "emergency_stop", "reason": reason, "ok": True},
            kind="security.stop",
        )
        if self.bus is not None:
            self.bus.emit("security.stop", phase=EventPhase.SECURITY, reason=reason)
        return {"ok": True, "stopped": True, "reason": reason}

    def unmute(self) -> dict[str, Any]:
        """Resume normal security operations."""
        self._stopped = False
        self.audit.record({"tool": "*", "decision": "resume", "ok": True}, kind="security.resume")
        if self.bus is not None:
            self.bus.emit("security.resume", phase=EventPhase.SECURITY)
        return {"ok": True, "stopped": False}

    stop = mute
    resume = unmute

    @property
    def stopped(self) -> bool:
        return self._stopped

    # ── policy & action checks ────────────────────────────────────────────
    def check_action(
        self,
        spec: ToolSpec | None,
        arguments: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        operator: str = "",
        confirmation_id: str | None = None,
        dry_run: bool | None = None,
    ) -> PermissionDecision:
        """Evaluate an action through the Phase 11 hardened policy engine."""
        role = self.identity.get_role(operator)
        return self.policy.evaluate(
            spec,
            arguments,
            session_id=session_id,
            operator_role=role,
            confirmation_id=confirmation_id,
            dry_run=dry_run,
        )

    # ── secrets & scanning ────────────────────────────────────────────────
    def scan_secrets(self, data: Any) -> tuple[bool, list[str]]:
        """Scan arbitrary data for credentials."""
        return self.scanner.scan_payload(data)

    def redact_secrets(self, data: Any) -> Any:
        """Deeply scrub credentials from an object."""
        return self.scanner.redact_payload(data)

    # ── confirmations & gateway contract ──────────────────────────────────
    def pending(self) -> list[dict[str, Any]]:
        return self.confirmations.pending()

    async def resolve(
        self,
        confirmation_id: str,
        *,
        approve: bool,
        note: str = "",
        operator: str = "",
    ) -> dict[str, Any]:
        role = self.identity.get_role(operator)
        conf = self.confirmations.get(confirmation_id)
        if conf is not None and not self.identity.can_resolve_confirmation(role, conf.risk):
            return {
                "ok": False,
                "error": f"role '{role.value}' is not permitted to resolve {conf.risk}-risk confirmations",
            }

        result = self.confirmations.resolve(confirmation_id, approve=approve, note=note)
        if not result.get("ok"):
            return result
        if approve and conf is not None and self.executor is not None:
            try:
                result["execution"] = await self.executor.execute_confirmed(conf, True)
            except Exception as exc:  # noqa: BLE001
                _log.warning("confirmed execution failed: %s", exc)
                result["execution"] = {"ok": False, "error": repr(exc)}
        return result

    def audit_tail(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.audit.tail(limit=limit)

    def audit_query(self, **filters: Any) -> list[dict[str, Any]]:
        return self.audit.query(**filters)

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "ok": True,
            "name": self.name,
            "phase": 11,
            "dry_run": self.settings.security.dry_run,
            "safety_level": self.settings.security.safety_level,
            "stopped": self._stopped,
            "owner": self.settings.owner,
            "permissions": self.permissions.describe(),
            "confirmations": self.confirmations.describe(),
            "audit": self.audit.describe(),
            "secrets": self.vault.public_dict(),
            "identity": self.identity.describe(),
            "budget": self.budgets.usage(),
            "executor": getattr(self.executor, "name", None),
        }

    def health(self) -> dict[str, Any]:
        permission_health = self.permissions.health()
        problems = list(permission_health.get("detail", {}).get("problems", []))
        if self._stopped:
            problems.append("emergency stop active")
        if self.settings.security.safety_level == "paranoid":
            problems.append("running in paranoid safety level")
        status = "degraded" if problems else "ok"
        return {
            "status": status,
            "detail": {
                "problems": problems,
                "dry_run": self.settings.security.dry_run,
                "safety_level": self.settings.security.safety_level,
                "pending_confirmations": len(self.confirmations.pending()),
                "audit_entries": self.audit.describe()["entries"],
            },
        }


def build_security(
    settings: Settings,
    *,
    bus: StarEventBus | None = None,
    audit: AuditLog | None = None,
    confirmations: ConfirmationStore | None = None,
    permissions: PermissionEngine | None = None,
    executor: Any = None,
) -> SecuritySurface:
    """Factory used by the composition root."""
    return SecuritySurface(
        settings,
        bus=bus,
        audit=audit,
        confirmations=confirmations,
        permissions=permissions,
        executor=executor,
    )
