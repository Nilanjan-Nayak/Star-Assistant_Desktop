"""Permission decisions — the gate every tool call passes before it runs.

Blueprint §2 non-negotiable #2 ("every OS action passes through the safety/policy
layer"), #3 ("dry-run by default") and #7 ("explicit confirmation for sensitive
actions"); §7 Phase 4 ("permissions, risk levels").

The engine answers one question: *may this exact call happen right now?*

    ALLOW               → run it for real
    SIMULATE            → dry-run: report what would happen, never touch the OS
    NEEDS_CONFIRMATION  → stop, create/reuse a pending confirmation, wait for a human
    DENY                → refuse (deny-list, risk tier, shell policy, rate limit)

It never executes anything itself — :class:`~Backend.star.tools.executor.ToolExecutor`
does, and it always audits the decision either way.
"""

from __future__ import annotations

import enum
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.security.confirmations import Confirmation, ConfirmationStore
from Backend.star.tools.risk import classify_risk, highest, risk_at_least
from Backend.star.tools.spec import ToolSpec

__all__ = ["Decision", "PermissionDecision", "PermissionEngine", "SHELLY_ARG_KEYS"]

_log = star_logger("permissions")

SHELLY_ARG_KEYS = frozenset({"command", "cmd", "shell", "script", "code", "exec", "bash", "powershell"})


@enum.unique
class Decision(str, enum.Enum):
    ALLOW = "allow"
    SIMULATE = "simulate"
    NEEDS_CONFIRMATION = "needs_confirmation"
    DENY = "deny"


@dataclass
class PermissionDecision:
    decision: Decision
    reason: str = ""
    risk: str = "unknown"
    dry_run: bool = False
    requires_confirmation: bool = False
    confirmation_id: str | None = None
    confirmation: Confirmation | None = None
    tool: str = ""
    session_id: str = ""
    rules: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.SIMULATE)

    def public(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "risk": self.risk,
            "dry_run": self.dry_run,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_id": self.confirmation_id,
            "tool": self.tool,
            "session_id": self.session_id,
            "rules": self.rules,
        }


class PermissionEngine:
    """Policy for tool calls: lists, risk tiers, shell, rate limits, confirmations, dry-run."""

    def __init__(
        self,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
        confirmations: ConfirmationStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        stop_gate: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus or StarEventBus()
        self.confirmations = confirmations if confirmations is not None else ConfirmationStore(settings, bus=self.bus)
        self._clock = clock
        self._stop_gate = stop_gate
        self._session_counts: dict[str, int] = defaultdict(int)
        self._minute_window: dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=512))
        self.stats: dict[str, int] = {
            "checks": 0,
            "allow": 0,
            "simulate": 0,
            "needs_confirmation": 0,
            "deny": 0,
        }

    # ── the decision ──────────────────────────────────────────────────────
    def check(
        self,
        spec: ToolSpec | None,
        arguments: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        request_id: str = "",
        plan_id: str = "",
        task_id: str = "",
        call_id: str = "",
        dry_run: bool | None = None,
        confirmation_id: str | None = None,
    ) -> PermissionDecision:
        self.stats["checks"] += 1
        args = dict(arguments or {})
        security = self.settings.security
        rules: list[str] = []

        if spec is None:
            rules.append("unknown_tool")
            return self._decide(
                Decision.DENY, "tool is not registered", tool="", risk="unknown", session_id=session_id, rules=rules
            )

        risk = highest([spec.risk, classify_risk(spec.name, args)])
        base: dict[str, Any] = {"tool": spec.name, "risk": risk, "session_id": session_id, "rules": rules}

        # 0. emergency stop (Phase 11 owns the switch; the gate is here already)
        if self._stop_gate is not None and self._stop_gate():
            rules.append("emergency_stop")
            return self._decide(Decision.DENY, "emergency stop is active", **base)

        # 1. explicit lists
        denylist = {name.lower() for name in security.tool_denylist}
        allowlist = {name.lower() for name in security.tool_allowlist}
        if spec.name.lower() in denylist:
            rules.append("denylist")
            return self._decide(Decision.DENY, f"'{spec.name}' is on the tool deny-list", **base)
        if allowlist and spec.name.lower() not in allowlist:
            rules.append("allowlist")
            return self._decide(Decision.DENY, f"'{spec.name}' is not on the tool allow-list", **base)

        # 2. risk tier ceiling
        if risk_at_least(risk, security.deny_risk):
            rules.append(f"risk>={security.deny_risk}")
            return self._decide(Decision.DENY, f"'{spec.name}' is {risk}-risk (deny tier: {security.deny_risk})", **base)

        # 3. shell policy — deny-by-default forever unless explicitly enabled
        if not security.allow_shell:
            offending = _find_shell_payload(args)
            if offending is not None:
                rules.append("shell_policy")
                return self._decide(
                    Decision.DENY,
                    f"raw shell payloads are refused (STAR_ALLOW_SHELL=false); argument '{offending}' was rejected",
                    **base,
                )

        # 4. rate limits
        limit_reason = self._rate_limited(session_id)
        if limit_reason:
            rules.append("rate_limit")
            return self._decide(Decision.DENY, limit_reason, **base)

        # 5. confirmation gate
        threshold = spec.confirm_above or security.confirm_above_risk
        if risk_at_least(risk, threshold):
            approved = self._approved_confirmation(
                spec.name, args, session_id=session_id, confirmation_id=confirmation_id
            )
            if approved is not None:
                rules.append(f"confirmed:{approved.confirmation_id}")
                base["confirmation_id"] = approved.confirmation_id
            else:
                confirmation = self.confirmations.request(
                    tool=spec.name,
                    arguments=args,
                    risk=risk,
                    reason=f"'{spec.name}' is {risk}-risk and needs your explicit approval",
                    session_id=session_id,
                    request_id=request_id,
                    plan_id=plan_id,
                    task_id=task_id,
                    call_id=call_id,
                )
                rules.append("confirmation_required")
                decision = self._decide(
                    Decision.NEEDS_CONFIRMATION,
                    confirmation.reason,
                    requires_confirmation=True,
                    confirmation_id=confirmation.confirmation_id,
                    **base,
                )
                decision.confirmation = confirmation
                return decision

        # 6. dry-run (blueprint default) — simulate instead of touching the OS
        simulate = self.settings.security.dry_run if dry_run is None else bool(dry_run)
        if simulate:
            if spec.dry_run_safe:
                rules.append("dry_run")
                return self._decide(
                    Decision.SIMULATE,
                    f"dry-run: '{spec.name}' would run with {len(args)} argument(s)",
                    dry_run=True,
                    **base,
                )
            rules.append("dry_run_unsafe")
            return self._decide(
                Decision.DENY,
                f"'{spec.name}' cannot be simulated safely, and dry-run is on",
                dry_run=True,
                **base,
            )

        rules.append("allow")
        return self._decide(Decision.ALLOW, "permitted by policy", **base)

    def bind_stop_gate(self, gate: Callable[[], bool] | None) -> None:
        """Attach the emergency-stop probe (wired by the security surface)."""
        self._stop_gate = gate

    # ── helpers ───────────────────────────────────────────────────────────
    def _decide(self, decision: Decision, reason: str, **kwargs: Any) -> PermissionDecision:
        result = PermissionDecision(decision=decision, reason=reason, **kwargs)
        self.stats[decision.value] = self.stats.get(decision.value, 0) + 1
        self.bus.emit(
            "security.decision",
            phase=EventPhase.SECURITY,
            tool=result.tool,
            decision=decision.value,
            reason=reason,
            risk=result.risk,
            dry_run=result.dry_run,
            session_id=result.session_id,
            requires_confirmation=result.requires_confirmation,
            confirmation_id=result.confirmation_id,
            rules=result.rules,
        )
        return result

    def _approved_confirmation(
        self, tool: str, args: dict[str, Any], *, session_id: str, confirmation_id: str | None
    ) -> Confirmation | None:
        if confirmation_id:
            item = self.confirmations.get(confirmation_id)
            if item is not None and item.state == "approved" and item.tool == tool:
                return item
        token = self.confirmations.approved_token(tool, args, session_id)
        if token:
            return self.confirmations.get(token)
        return None

    def _rate_limited(self, session_id: str) -> str:
        security = self.settings.security
        key = session_id or "*"
        total = self._session_counts.get(key, 0)
        if total >= security.max_actions_per_session:
            return f"session action limit reached ({security.max_actions_per_session})"
        now = self._clock()
        window = self._minute_window[key]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= security.max_actions_per_minute:
            return f"rate limit reached ({security.max_actions_per_minute} actions/minute)"
        return ""

    def record_execution(self, session_id: str = "") -> None:
        """Count an action that was allowed/simulated (rate-limit accounting)."""
        key = session_id or "*"
        self._session_counts[key] += 1
        self._minute_window[key].append(self._clock())

    def reset(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._session_counts.clear()
            self._minute_window.clear()
        else:
            self._session_counts.pop(session_id, None)
            self._minute_window.pop(session_id, None)

    def describe(self) -> dict[str, Any]:
        security = self.settings.security
        return {
            "safety_level": security.safety_level,
            "dry_run": security.dry_run,
            "confirm_above_risk": security.confirm_above_risk,
            "deny_risk": security.deny_risk,
            "allow_shell": security.allow_shell,
            "allowlist": list(security.tool_allowlist),
            "denylist": list(security.tool_denylist),
            "rate_limits": {
                "per_session": security.max_actions_per_session,
                "per_minute": security.max_actions_per_minute,
            },
            "sessions_tracked": len(self._session_counts),
            "confirmations": self.confirmations.describe(),
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if not self.settings.security.dry_run:
            problems.append("dry-run disabled — real OS actions are permitted")
        if self.settings.security.allow_shell:
            problems.append("shell payloads allowed")
        if self.stats["deny"] > max(10, self.stats["checks"] // 2):
            problems.append("high deny rate")
        return {
            "status": "degraded" if problems else "ok",
            "detail": {"problems": problems, **{key: self.stats[key] for key in ("checks", "allow", "simulate", "needs_confirmation", "deny")}},
        }


def _find_shell_payload(value: Any, path: str = "") -> str | None:
    """Depth-first search for a command-like argument holding shell metacharacters."""
    from Backend.star.brain.reasoning import SHELLY

    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in SHELLY_ARG_KEYS:
                if isinstance(item, str) and SHELLY.search(item):
                    return key_path
                if isinstance(item, (list, tuple, dict)) and _looks_like_shell(item):
                    return key_path
            found = _find_shell_payload(item, key_path)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_shell_payload(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _looks_like_shell(value: Any) -> bool:
    from Backend.star.brain.reasoning import SHELLY

    if isinstance(value, str):
        return bool(SHELLY.search(value))
    if isinstance(value, (list, tuple)):
        return any(_looks_like_shell(item) for item in value)
    if isinstance(value, dict):
        return any(_looks_like_shell(item) for item in value.values())
    return False
