"""Hardened security policy engine for Phase 11.

Implements safety levels (permissive, normal, strict, paranoid),
role-based execution, secret leak prevention, and confirmation gating.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.security.budgets import SecurityBudget
from Backend.star.security.confirmations import ConfirmationStore
from Backend.star.security.identity import OperatorIdentity, Role
from Backend.star.security.secrets import SecretScanner
from Backend.star.tools.risk import classify_risk, highest, risk_at_least
from Backend.star.tools.spec import ToolSpec

if TYPE_CHECKING:
    from Backend.star.tools.permissions import Decision, PermissionDecision

_log = star_logger("security.policy")


class SecurityPolicyEngine:
    """Multi-tiered security policy validator."""

    def __init__(
        self,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
        confirmations: ConfirmationStore | None = None,
        identity: OperatorIdentity | None = None,
        budgets: SecurityBudget | None = None,
        scanner: SecretScanner | None = None,
        stop_gate: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.confirmations = confirmations or ConfirmationStore(settings, bus=bus)
        self.identity = identity or OperatorIdentity(settings)
        self.budgets = budgets or SecurityBudget(settings, bus=bus)
        self.scanner = scanner or SecretScanner()
        self.stop_gate = stop_gate
        self.stats = {"checks": 0, "allowed": 0, "denied": 0, "confirmations": 0, "simulated": 0}

    def evaluate(
        self,
        spec: ToolSpec | None,
        arguments: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        operator_role: Role = Role.OPERATOR,
        confirmation_id: str | None = None,
        dry_run: bool | None = None,
    ) -> PermissionDecision:
        """Full evaluation ladder for an intended action."""
        from Backend.star.tools.permissions import Decision, PermissionDecision

        self.stats["checks"] += 1
        args = dict(arguments or {})
        rules: list[str] = []

        if spec is None:
            rules.append("unknown_tool")
            self.stats["denied"] += 1
            return PermissionDecision(
                decision=Decision.DENY,
                reason="tool is not registered",
                tool="",
                risk="unknown",
                session_id=session_id,
                rules=rules,
            )

        risk = highest([spec.risk, classify_risk(spec.name, args)])
        base: dict[str, Any] = {"tool": spec.name, "risk": risk, "session_id": session_id, "rules": rules}

        # 0. Emergency Stop
        if self.stop_gate is not None and self.stop_gate():
            rules.append("emergency_stop")
            self.stats["denied"] += 1
            return PermissionDecision(decision=Decision.DENY, reason="emergency stop is active", **base)

        # 1. Identity & Role check
        if not self.identity.can_execute_tier(operator_role, risk):
            rules.append(f"role_restriction:{operator_role.value}")
            self.stats["denied"] += 1
            return PermissionDecision(
                decision=Decision.DENY,
                reason=f"role '{operator_role.value}' cannot execute {risk}-risk actions",
                **base,
            )

        # 2. Denylist & Allowlist
        sec = self.settings.security
        denylist = {n.lower() for n in sec.tool_denylist}
        allowlist = {n.lower() for n in sec.tool_allowlist}
        if spec.name.lower() in denylist:
            rules.append("denylist")
            self.stats["denied"] += 1
            return PermissionDecision(decision=Decision.DENY, reason=f"'{spec.name}' is on the tool deny-list", **base)
        if allowlist and spec.name.lower() not in allowlist:
            rules.append("allowlist")
            self.stats["denied"] += 1
            return PermissionDecision(decision=Decision.DENY, reason=f"'{spec.name}' is not on the tool allow-list", **base)

        # 3. Secret Leak Scanner in arguments
        has_secret, secret_kinds = self.scanner.scan_payload(args)
        if has_secret:
            if not spec.name.startswith("memory_") and not spec.name.startswith("security_"):
                rules.append(f"secret_leak_refused:{','.join(secret_kinds)}")
                self.stats["denied"] += 1
                if self.bus is not None:
                    self.bus.emit(
                        "security.secret_detected",
                        phase=EventPhase.SECURITY,
                        tool=spec.name,
                        kinds=secret_kinds,
                    )
                return PermissionDecision(
                    decision=Decision.DENY,
                    reason=f"tool arguments contain sensitive credentials: {', '.join(secret_kinds)}",
                    **base,
                )

        # 4. Safety Level Escalation
        level = sec.safety_level
        effective_deny_risk = sec.deny_risk
        effective_confirm_risk = sec.confirm_above_risk

        if level == "paranoid":
            effective_deny_risk = "high"
            effective_confirm_risk = "low"
        elif level == "strict":
            effective_confirm_risk = "medium"

        if risk_at_least(risk, effective_deny_risk):
            rules.append(f"risk>={effective_deny_risk}")
            self.stats["denied"] += 1
            return PermissionDecision(
                decision=Decision.DENY,
                reason=f"'{spec.name}' is {risk}-risk (denied under safety level '{level}')",
                **base,
            )

        # 5. Shell Policy
        if not sec.allow_shell:
            from Backend.star.tools.permissions import _find_shell_payload

            offending = _find_shell_payload(args)
            if offending is not None:
                rules.append("shell_policy")
                self.stats["denied"] += 1
                return PermissionDecision(
                    decision=Decision.DENY,
                    reason=f"raw shell payloads are refused (STAR_ALLOW_SHELL=false); argument '{offending}' was rejected",
                    **base,
                )

        # 6. Budget & Rate Limiting
        allowed, budget_reason = self.budgets.check(session_id)
        if not allowed:
            rules.append("budget_exceeded")
            self.stats["denied"] += 1
            return PermissionDecision(decision=Decision.DENY, reason=budget_reason, **base)

        # 7. Confirmation Gate
        if risk_at_least(risk, effective_confirm_risk):
            approved = self.confirmations.approved_token(spec.name, args, session_id=session_id)
            if approved is not None or (confirmation_id and self.confirmations.get(confirmation_id) and self.confirmations.get(confirmation_id).state == "approved"):
                rules.append(f"confirmed:{approved or confirmation_id}")
                base["confirmation_id"] = approved or confirmation_id
            else:
                confirmation = self.confirmations.request(
                    tool=spec.name,
                    arguments=args,
                    risk=risk,
                    reason=f"'{spec.name}' is {risk}-risk and needs your explicit approval (safety level: {level})",
                    session_id=session_id,
                )
                rules.append("confirmation_required")
                self.stats["confirmations"] += 1
                dec = PermissionDecision(
                    decision=Decision.NEEDS_CONFIRMATION,
                    reason=confirmation.reason,
                    requires_confirmation=True,
                    confirmation_id=confirmation.confirmation_id,
                    **base,
                )
                dec.confirmation = confirmation
                return dec

        # 8. Dry-run or Allow
        simulate = sec.dry_run if dry_run is None else bool(dry_run)
        if level == "paranoid":
            simulate = True

        if simulate:
            rules.append("dry_run")
            self.stats["simulated"] += 1
            return PermissionDecision(
                decision=Decision.SIMULATE,
                reason="simulated in dry-run mode — nothing touched the OS",
                simulated=True,
                **base,
            )

        # Consume budget on execution
        self.budgets.consume(session_id)
        rules.append("allow")
        self.stats["allowed"] += 1
        return PermissionDecision(decision=Decision.ALLOW, reason="allowed by security policy", **base)
