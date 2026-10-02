"""Security tools for Phase 11.

Allows operators and diagnostic routines to inspect security health,
query audit trails, trigger the emergency stop, and scan payloads for secrets.
"""

from __future__ import annotations

from typing import Any

from Backend.star.brain.schemas import AgentName
from Backend.star.config.settings import Settings
from Backend.star.observability.events import StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.security.surface import SecuritySurface
from Backend.star.tools.registry import StarToolRegistry
from Backend.star.tools.spec import ToolCategory, ToolSpec

_log = star_logger("security.tools")


class SecurityToolkit:
    """Tool handlers for security introspection and control."""

    def __init__(
        self,
        security: SecuritySurface,
        settings: Settings,
        *,
        bus: StarEventBus | None = None,
    ) -> None:
        self.security = security
        self.settings = settings
        self.bus = bus

    def security_status(self) -> dict[str, Any]:
        """Return runtime security state, safety level, budgets, and stats."""
        return {"ok": True, **self.security.describe()}

    def security_audit_query(
        self,
        tool: str | None = None,
        session_id: str | None = None,
        decision: str | None = None,
        risk: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query the append-only audit trail with structured filters."""
        entries = self.security.audit_query(
            tool=tool or None,
            session_id=session_id or None,
            decision=decision or None,
            risk=risk or None,
            limit=min(limit, 100),
        )
        return {"ok": True, "count": len(entries), "entries": entries}

    def security_emergency_stop(self, reason: str = "manual_tool_trigger") -> dict[str, Any]:
        """Trigger emergency stop across all subsystems."""
        return self.security.mute(reason=reason)

    def security_resume(self) -> dict[str, Any]:
        """Resume operations from emergency stop."""
        return self.security.unmute()

    def security_scan_secrets(self, text: str = "") -> dict[str, Any]:
        """Scan text for credentials and return redacted version with match types."""
        has_secrets, kinds = self.security.scan_secrets(text)
        redacted = self.security.redact_secrets(text)
        return {
            "ok": True,
            "has_secrets": has_secrets,
            "secret_kinds": kinds,
            "redacted_text": redacted,
        }


def register_security_tools(
    registry: StarToolRegistry,
    settings: Settings,
    *,
    bus: StarEventBus | None = None,
    security: SecuritySurface,
) -> list[ToolSpec]:
    """Register Phase 11 security tools in the tool registry."""
    toolkit = SecurityToolkit(security, settings, bus=bus)

    specs = [
        ToolSpec(
            name="security_status",
            category=ToolCategory.META,
            risk="low",
            agent=AgentName.SYSTEM,
            description="Inspect active security policies, safety levels, and budget consumption",
            parameters={"type": "object", "properties": {}},
            handler=toolkit.security_status,
        ),
        ToolSpec(
            name="security_audit_query",
            category=ToolCategory.META,
            risk="low",
            agent=AgentName.SYSTEM,
            description="Query the audit log with filters for tool, decision, risk, or session",
            parameters={
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "description": "Filter by tool name"},
                    "decision": {"type": "string", "description": "Filter by decision (executed, denied, simulated)"},
                    "risk": {"type": "string", "description": "Filter by risk level"},
                    "limit": {"type": "integer", "description": "Maximum entries to return", "default": 50},
                },
            },
            handler=toolkit.security_audit_query,
        ),
        ToolSpec(
            name="security_emergency_stop",
            category=ToolCategory.META,
            risk="medium",
            confirm_above="critical",  # Never block emergency stop on confirmation!
            agent=AgentName.SYSTEM,
            description="Immediately stop all computer control, automation, and background processes",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "Reason for emergency stop", "default": "tool"}},
            },
            handler=toolkit.security_emergency_stop,
        ),
        ToolSpec(
            name="security_resume",
            category=ToolCategory.META,
            risk="medium",
            agent=AgentName.SYSTEM,
            description="Resume normal operations from emergency stop",
            parameters={"type": "object", "properties": {}},
            handler=toolkit.security_resume,
        ),
        ToolSpec(
            name="security_scan_secrets",
            category=ToolCategory.META,
            risk="low",
            agent=AgentName.SYSTEM,
            description="Scan text or payloads for credentials and return redacted output",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Text to scan for credentials"}},
                "required": ["text"],
            },
            handler=toolkit.security_scan_secrets,
        ),
    ]

    for spec in specs:
        registry.register(spec)

    _log.info("star2.security.tools.registered count=%d", len(specs))
    return specs
