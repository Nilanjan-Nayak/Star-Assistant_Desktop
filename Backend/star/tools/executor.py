"""The single execution path for every tool call.

Blueprint §7 Phase 4: "typed tool registry with permissions, risk levels and audit
trails"; §2 non-negotiables #2 (safety layer), #3 (dry-run default), #8
(OBSERVE → ACT → VERIFY → RECOVER).

    spec lookup → argument validation → permission decision → audit(before)
        → run in a worker thread with a timeout → normalise → audit(after) → event

Nothing else in STAR 2.0 calls a tool handler directly. The brain's
``executor`` seam (:meth:`execute_plan`) uses this class, so a `PROPOSED` tool
call from an LLM proposal becomes a permission-checked, audited action.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from Backend.star.brain.planning import task_state_from_calls
from Backend.star.brain.schemas import Context, Plan, Task, TaskState, ToolCall, ToolCallState, Verification
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.security.audit import AuditLog
from Backend.star.security.confirmations import Confirmation, ConfirmationStore
from Backend.star.tools.legacy_gate import LegacyToolGate
from Backend.star.tools.permissions import Decision, PermissionEngine
from Backend.star.tools.registry import StarToolRegistry
from Backend.star.tools.spec import ToolResult, ToolSpec, validate_arguments

__all__ = ["ToolExecutor", "build_tool_executor"]

_log = star_logger("executor")


def _jsonable(value: Any) -> Any:
    """Best-effort JSON-safe conversion (tool handlers return anything)."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {"repr": repr(value)[:500]}


class ToolExecutor:
    """Validates, gates, runs and audits tool calls."""

    name = "tool-executor"

    def __init__(
        self,
        settings: Settings,
        *,
        registry: StarToolRegistry,
        permissions: PermissionEngine | None = None,
        audit: AuditLog | None = None,
        confirmations: ConfirmationStore | None = None,
        bus: StarEventBus | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.bus = bus or StarEventBus()
        self.confirmations = confirmations if confirmations is not None else ConfirmationStore(settings, bus=self.bus)
        self.permissions = permissions or PermissionEngine(settings, bus=self.bus, confirmations=self.confirmations)
        self.audit = audit or AuditLog(settings.paths.data_dir / "audit.jsonl", bus=self.bus)
        if self.permissions.confirmations is not self.confirmations:
            self.confirmations = self.permissions.confirmations
        #: the shim that puts the legacy ``execute_tool`` funnel under the same policy
        self.gate = LegacyToolGate(self, enabled=settings.security.legacy_gate)
        self.stats: dict[str, Any] = {
            "calls": 0,
            "executed": 0,
            "simulated": 0,
            "denied": 0,
            "needs_confirmation": 0,
            "invalid_args": 0,
            "unknown_tool": 0,
            "timeouts": 0,
            "errors": 0,
            "plans": 0,
        }

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def startup(self) -> None:
        """Install the legacy gate so *every* OS action passes policy (blueprint #2)."""
        patched = self.gate.install()
        self.bus.emit(
            "tool.gate_installed",
            phase=EventPhase.TOOL,
            enabled=self.gate.enabled,
            modules=len(patched),
            patched=sorted(patched),
        )

    async def aclose(self) -> None:
        restored = self.gate.uninstall()
        if restored:
            self.bus.emit("tool.gate_removed", phase=EventPhase.TOOL, modules=restored)

    # ── one call ──────────────────────────────────────────────────────────
    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        request_id: str = "",
        plan_id: str = "",
        task_id: str = "",
        call_id: str = "",
        dry_run: bool | None = None,
        confirmation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> ToolResult:
        started = time.perf_counter()
        self.stats["calls"] += 1
        args = dict(arguments or {})
        ids = {
            "session_id": session_id or self.settings.session_id,
            "request_id": request_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "call_id": call_id,
        }

        spec = self.registry.get(tool)
        if spec is None:
            self.stats["unknown_tool"] += 1
            return await self._finish(
                ToolResult(
                    ok=False,
                    tool=str(tool),
                    call_id=call_id,
                    decision="unknown_tool",
                    error=f"tool '{tool}' is not registered",
                ),
                ids,
                started,
                arguments=args,
            )

        args, errors, notes = validate_arguments(spec, args)
        if errors:
            self.stats["invalid_args"] += 1
            return await self._finish(
                ToolResult(
                    ok=False,
                    tool=spec.name,
                    call_id=call_id,
                    decision="invalid_args",
                    risk=spec.risk,
                    error="; ".join(errors),
                    notes=notes,
                ),
                ids,
                started,
                arguments=args,
            )

        decision = self.permissions.check(
            spec,
            args,
            session_id=ids["session_id"],
            request_id=request_id,
            plan_id=plan_id,
            task_id=task_id,
            call_id=call_id,
            dry_run=dry_run,
            confirmation_id=confirmation_id,
        )

        if decision.decision is Decision.DENY:
            self.stats["denied"] += 1
            return await self._finish(
                ToolResult(
                    ok=False,
                    tool=spec.name,
                    call_id=call_id,
                    decision="denied",
                    risk=decision.risk,
                    error=decision.reason,
                    notes=[*notes, *decision.rules],
                ),
                ids,
                started,
                arguments=args,
                decision=decision,
            )

        if decision.decision is Decision.NEEDS_CONFIRMATION:
            self.stats["needs_confirmation"] += 1
            return await self._finish(
                ToolResult(
                    ok=False,
                    tool=spec.name,
                    call_id=call_id,
                    decision="needs_confirmation",
                    risk=decision.risk,
                    error=decision.reason,
                    confirmation_id=decision.confirmation_id,
                    notes=[*notes, *decision.rules],
                ),
                ids,
                started,
                arguments=args,
                decision=decision,
            )

        if decision.decision is Decision.SIMULATE:
            self.stats["simulated"] += 1
            self.permissions.record_execution(ids["session_id"])
            return await self._finish(
                ToolResult(
                    ok=True,
                    tool=spec.name,
                    call_id=call_id,
                    decision="simulated",
                    risk=decision.risk,
                    dry_run=True,
                    data={
                        "simulated": True,
                        "tool": spec.name,
                        "arguments": args,
                        "category": spec.category.value,
                        "agent": spec.agent.value,
                        "reversible": spec.reversible,
                    },
                    output=f"dry-run: {spec.name}({', '.join(f'{k}={v}' for k, v in args.items())})"[:400],
                    notes=[*notes, *decision.rules],
                ),
                ids,
                started,
                arguments=args,
                decision=decision,
            )

        # ── ALLOW: really run it, in a thread, with a timeout ──────────────
        limit = timeout_s or spec.timeout_s
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(spec.handler, **args), timeout=limit)
        except TimeoutError:
            self.stats["timeouts"] += 1
            return await self._finish(
                ToolResult(
                    ok=False,
                    tool=spec.name,
                    call_id=call_id,
                    decision="timeout",
                    risk=decision.risk,
                    error=f"'{spec.name}' did not finish within {limit:.1f}s",
                    notes=notes,
                ),
                ids,
                started,
                arguments=args,
                decision=decision,
            )
        except Exception as exc:  # noqa: BLE001 — a tool crash must not crash Star
            self.stats["errors"] += 1
            _log.warning("tool %s raised: %s", spec.name, exc)
            return await self._finish(
                ToolResult(
                    ok=False,
                    tool=spec.name,
                    call_id=call_id,
                    decision="error",
                    risk=decision.risk,
                    error=repr(exc),
                    notes=notes,
                ),
                ids,
                started,
                arguments=args,
                decision=decision,
            )

        self.stats["executed"] += 1
        self.permissions.record_execution(ids["session_id"])
        result = self._normalise(spec, raw)
        result.call_id = call_id
        result.risk = decision.risk
        result.notes = [*notes, *decision.rules]
        result.confirmation_id = decision.confirmation_id
        return await self._finish(result, ids, started, arguments=args, decision=decision)

    def _normalise(self, spec: ToolSpec, raw: Any) -> ToolResult:
        """Turn whatever a handler returned into a typed :class:`ToolResult`."""
        if isinstance(raw, ToolResult):
            return raw
        if raw is None:
            return ToolResult(ok=True, tool=spec.name, decision="executed", output="")
        if isinstance(raw, dict):
            ok = bool(raw.get("success", raw.get("ok", True)))
            error = raw.get("error") if not ok else None
            data = _jsonable({key: value for key, value in raw.items() if key not in {"success"}})
            output = str(raw.get("output") or raw.get("message") or raw.get("response") or "")
            if not output and isinstance(raw.get("result"), (str, int, float)):
                output = str(raw["result"])
            return ToolResult(
                ok=ok,
                tool=spec.name,
                decision="executed",
                data=data if isinstance(data, dict) else {"value": data},
                output=output[:500],
                error=str(error)[:500] if error else None,
            )
        return ToolResult(
            ok=True,
            tool=spec.name,
            decision="executed",
            data={"value": _jsonable(raw)} if not isinstance(raw, (str, int, float, bool)) else {"value": raw},
            output=str(raw)[:500],
        )

    async def _finish(
        self,
        result: ToolResult,
        ids: dict[str, str],
        started: float,
        *,
        arguments: dict[str, Any] | None = None,
        decision: Any = None,
    ) -> ToolResult:
        result.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        entry = {
            "tool": result.tool,
            "decision": result.decision,
            "ok": result.ok,
            "risk": result.risk,
            "dry_run": result.dry_run,
            "arguments": _jsonable(arguments or {}),
            "duration_ms": result.duration_ms,
            "error": result.error,
            "output": result.output[:200] if result.output else None,
            "confirmation_id": result.confirmation_id,
            "rules": list(decision.rules) if decision is not None else None,
            "reason": decision.reason if decision is not None else result.error,
            **ids,
        }
        try:
            record = self.audit.record(entry)
            result.audited = bool(record.get("persisted", True))
        except Exception as exc:  # noqa: BLE001 — auditing must never break the call
            _log.warning("audit failed for %s: %s", result.tool, exc)
            result.audited = False
        self.bus.emit(
            "tool.call",
            phase=EventPhase.TOOL,
            tool=result.tool,
            decision=result.decision,
            ok=result.ok,
            risk=result.risk,
            dry_run=result.dry_run,
            duration_ms=result.duration_ms,
            error=result.error,
            confirmation_id=result.confirmation_id,
            **ids,
        )
        return result

    # ── calls inside a plan ───────────────────────────────────────────────
    async def run_call(
        self,
        call: ToolCall,
        *,
        session_id: str = "",
        request_id: str = "",
        plan_id: str = "",
        task_id: str = "",
        dry_run: bool | None = None,
    ) -> ToolCall:
        """Execute one :class:`ToolCall` in place and return it."""
        call.state = ToolCallState.RUNNING
        call.attempt += 1
        result = await self.call(
            call.tool,
            call.arguments,
            session_id=session_id,
            request_id=request_id,
            plan_id=plan_id,
            task_id=task_id,
            call_id=call.call_id,
            dry_run=dry_run,
            confirmation_id=call.confirmation_id,
        )
        call.risk = result.risk or call.risk
        call.duration_ms = result.duration_ms
        call.result = result.as_call_result()
        call.error = result.error
        call.confirmation_id = result.confirmation_id or call.confirmation_id
        call.requires_confirmation = call.requires_confirmation or result.decision == "needs_confirmation"
        call.state = {
            "executed": ToolCallState.DONE,
            "simulated": ToolCallState.DONE,
            "denied": ToolCallState.DENIED,
            "needs_confirmation": ToolCallState.PROPOSED,
            "invalid_args": ToolCallState.FAILED,
            "unknown_tool": ToolCallState.FAILED,
            "timeout": ToolCallState.FAILED,
            "error": ToolCallState.FAILED,
        }.get(result.decision, ToolCallState.FAILED)
        if result.ok is False and call.state is ToolCallState.DONE:
            call.state = ToolCallState.FAILED
        return call

    async def execute_plan(
        self,
        plan: Plan,
        *,
        context: Context | None = None,
        session_id: str = "",
        request_id: str = "",
        dry_run: bool | None = None,
    ) -> Plan:
        """Run every ``PROPOSED`` call in a plan through policy, then refresh states."""
        self.stats["plans"] += 1
        simulate = self.settings.security.dry_run if dry_run is None else bool(dry_run)
        executed = denied = waiting = failed = 0
        for task in plan.tasks:
            if not task.steps:
                continue
            if task.started_at is None:
                task.started_at = _now()
            for call in task.steps:
                if call.state not in (ToolCallState.PROPOSED, ToolCallState.APPROVED):
                    continue
                await self.run_call(
                    call,
                    session_id=session_id,
                    request_id=request_id or plan.request_id,
                    plan_id=plan.plan_id,
                    task_id=task.task_id,
                    dry_run=simulate,
                )
                if call.state is ToolCallState.DONE:
                    executed += 1
                elif call.state is ToolCallState.DENIED:
                    denied += 1
                elif call.state is ToolCallState.FAILED:
                    failed += 1
                elif call.state is ToolCallState.PROPOSED:
                    waiting += 1
            task.state = task_state_from_calls(
                task.steps,
                risk=task.risk,
                confirm_above=self.settings.security.confirm_above_risk,
                deny_risk=self.settings.security.deny_risk,
            )
            task.verification = Verification(
                expected=f"{len(task.steps)} call(s) for agent {task.agent.value} succeed",
                observed=" ".join(f"{call.tool}={call.state.value}" for call in task.steps)
                + (" dry_run" if simulate else ""),
                ok=bool(task.steps)
                and all(
                    (call.state is ToolCallState.DONE and call.result.get("success", True))
                    or call.state is ToolCallState.SKIPPED
                    for call in task.steps
                ),
                method="result_flag",
                note="dry-run: simulated, nothing touched the OS" if simulate else "",
            )
            task.summary = ", ".join(call.tool for call in task.steps)[:180]
            task.error = next((call.error for call in task.steps if call.error), None)
            task.ended_at = task.ended_at or _now()
        self.bus.emit(
            "plan.executed",
            phase=EventPhase.TOOL,
            plan_id=plan.plan_id,
            request_id=request_id or plan.request_id,
            session_id=session_id,
            executed=executed,
            denied=denied,
            waiting_confirmation=waiting,
            failed=failed,
            dry_run=simulate,
        )
        return plan

    async def execute(
        self,
        plan: Plan,
        *,
        context: Context | None = None,
        session_id: str = "",
        request_id: str = "",
        dry_run: bool | None = None,
    ) -> Plan:
        """Alias for :meth:`execute_plan` satisfying the Phase 10 execution seam."""
        return await self.execute_plan(
            plan, context=context, session_id=session_id, request_id=request_id, dry_run=dry_run
        )

    # ── confirmation follow-through ───────────────────────────────────────
    async def execute_confirmed(self, confirmation: Confirmation, approved: bool = True) -> dict[str, Any]:
        """Run a call the user just approved (``POST /api/v1/confirmations``)."""
        if not approved:
            return {"ok": False, "decision": "denied", "reason": "confirmation denied by user"}
        # the human said yes: make the store agree before policy re-checks the call
        self.confirmations.mark_approved(confirmation.confirmation_id, note="approved by user")
        result = await self.call(
            confirmation.tool,
            confirmation.arguments,
            session_id=confirmation.session_id,
            request_id=confirmation.request_id,
            plan_id=confirmation.plan_id,
            task_id=confirmation.task_id,
            call_id=confirmation.call_id,
            confirmation_id=confirmation.confirmation_id,
            dry_run=False,          # an explicit human yes means: really do it
        )
        confirmation.result = result.as_call_result()
        payload = result.as_call_result()
        payload.update({"tool": result.tool, "decision": result.decision, "confirmation_id": confirmation.confirmation_id})
        return payload

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tools": len(self.registry),
            "dry_run": self.settings.security.dry_run,
            "permissions": self.permissions.describe(),
            "audit": self.audit.describe(),
            "gate": self.gate.describe(),
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if not self.settings.security.dry_run:
            problems.append("dry-run is off")
        if self.stats["errors"] + self.stats["timeouts"] > max(5, self.stats["calls"] // 2):
            problems.append("high tool error rate")
        return {
            "status": "degraded" if problems else "ok",
            "detail": {"problems": problems, "calls": self.stats["calls"], "tools": len(self.registry)},
        }


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def build_tool_executor(
    settings: Settings,
    *,
    registry: StarToolRegistry | None = None,
    bus: StarEventBus | None = None,
    audit: AuditLog | None = None,
    confirmations: ConfirmationStore | None = None,
    permissions: PermissionEngine | None = None,
) -> ToolExecutor:
    """Factory used by the composition root."""
    store = confirmations if confirmations is not None else ConfirmationStore(settings, bus=bus)
    engine = permissions or PermissionEngine(settings, bus=bus, confirmations=store)
    return ToolExecutor(
        settings,
        registry=registry if registry is not None else StarToolRegistry(settings, bus=bus),
        permissions=engine,
        audit=audit or AuditLog(settings.paths.data_dir / "audit.jsonl", bus=bus),
        confirmations=store,
        bus=bus,
    )
