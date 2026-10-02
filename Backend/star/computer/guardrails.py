"""Computer-action guardrails — the local policy in front of the safety governor.

Two layers, on purpose:

1. **Local checks** (this file, synchronous, cheap): are the coordinates on the
   screen we know about? Is the click inside a forbidden region? Is the text too
   long or a destructive command? Is the hotkey on the blocked list?
2. **The existing safety governor** (`agent/safety/governor.py`) with its six
   invariants — total budget, rate limit, wall clock, forbidden regions, keyword
   blocklist, capability grants. It is *reused*, not reimplemented (blueprint #1).

Everything here is also what makes dry-run honest: the intended coordinates and
keystrokes are fully described before anything moves, so the audit entry is a
faithful record of what *would* have happened.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from agent.core.enums import ActionKind, SafetyLevel
from agent.geometry.bbox import BoundingBox
from agent.motor.spec import ActionSpec
from agent.safety.config import GovernorConfig
from agent.safety.governor import SafetyGovernor

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase
from Backend.star.observability.logging import star_logger

__all__ = ["ActionVerdict", "ComputerGuardrails", "DESTRUCTIVE_TEXT"]

_log = star_logger("star2.computer.guardrails")

#: text a "type this" request must never contain, whatever the goal said
DESTRUCTIVE_TEXT: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    "format c:",
    "del /f /s /q",
    "shutdown /s /t 0",
    "shutdown -h now",
    ":(){:|:&};:",
    "reg delete",
    "diskpart",
)

_COORD_KINDS = frozenset(
    {ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK, ActionKind.MOVE, ActionKind.DRAG}
)


def _kind_value(spec: ActionSpec) -> str:
    kind = getattr(spec, "kind", "")
    return str(getattr(kind, "value", kind))


@dataclass(frozen=True, slots=True)
class ActionVerdict:
    """May this physical action happen? Plus the reason it may not."""

    ok: bool
    reason: str = ""
    rule: str = ""            # "" | bounds | forbidden_region | text_length | destructive_text |
                              # blocked_hotkey | scroll | wait | governor
    risk: str = "medium"
    governor_rule: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "rule": self.rule,
            "risk": self.risk,
            "governor_rule": self.governor_rule,
        }


class ComputerGuardrails:
    """Bounds + blocklists + the reused :class:`SafetyGovernor`."""

    def __init__(self, settings: Settings | None = None, *, bus: Any = None, governor: SafetyGovernor | None = None) -> None:
        self.settings = settings or Settings()
        self.cfg = self.settings.computer
        self.bus = bus
        self.governor = governor or SafetyGovernor(self.governor_config())
        self.stats: dict[str, int] = {"checks": 0, "allowed": 0, "refused": 0, "governor_refused": 0}
        # a short, honest memory of what was refused — the console shows it
        self._refusals: deque[dict[str, Any]] = deque(maxlen=8)

    # ── configuration ─────────────────────────────────────────────────────
    def governor_config(self) -> GovernorConfig:
        """Map STAR 2.0 settings onto the existing governor's config."""
        try:
            level = SafetyLevel(str(self.settings.security.safety_level).lower())
        except ValueError:
            level = SafetyLevel.NORMAL
        regions: list[BoundingBox] = []
        for raw in self.cfg.forbidden_regions:
            parts = [int(part) for part in str(raw).split(",") if part.strip()]
            if len(parts) == 4:
                regions.append(BoundingBox(x=parts[0], y=parts[1], width=parts[2], height=parts[3]))
        return GovernorConfig.for_level(
            level,
            dry_run=bool(self.settings.security.dry_run),
            forbidden_regions=regions,
            max_actions_per_minute=int(self.settings.security.max_actions_per_minute),
            max_total_actions=int(self.settings.security.max_actions_per_session),
        )

    # ── the verdict ───────────────────────────────────────────────────────
    def check(self, spec: ActionSpec) -> ActionVerdict:
        """Synchronous local policy. Never raises."""
        self.stats["checks"] += 1
        verdict = self._evaluate(spec)
        self.stats["allowed" if verdict.ok else "refused"] += 1
        self._remember(spec, verdict)
        if not verdict.ok and self.bus is not None:
            self.bus.emit(
                "safety.blocked",
                phase=EventPhase.SECURITY,
                layer="computer_guardrails",
                rule=verdict.rule,
                reason=verdict.reason,
                action=_kind_value(spec),
            )
        return verdict

    def _evaluate(self, spec: ActionSpec) -> ActionVerdict:
        kind = getattr(spec, "kind", None)
        width, height = int(self.cfg.screen_width), int(self.cfg.screen_height)

        if kind in _COORD_KINDS:
            coord = getattr(spec, "coord", None)
            x, y = int(getattr(coord, "x", 0)), int(getattr(coord, "y", 0))
            if not (0 <= x < width and 0 <= y < height):
                return ActionVerdict(
                    False,
                    reason=f"({x}, {y}) is outside the known screen {width}x{height}",
                    rule="bounds",
                    risk="high",
                )
            for region in self.cfg.forbidden_regions:
                box = self._box(region)
                if box is not None and box.x <= x < box.x + box.width and box.y <= y < box.y + box.height:
                    return ActionVerdict(
                        False,
                        reason=f"({x}, {y}) is inside a forbidden region {region}",
                        rule="forbidden_region",
                        risk="critical",
                    )

        if kind is ActionKind.SCROLL:
            coord = getattr(spec, "coord", None)
            if coord is not None and not (0 <= int(coord.x) < width and 0 <= int(coord.y) < height):
                return ActionVerdict(False, reason="scroll position is off-screen", rule="bounds")
            if abs(int(getattr(spec, "amount", 0))) > int(self.cfg.max_scroll):
                return ActionVerdict(
                    False, reason=f"scroll amount above the cap of {self.cfg.max_scroll}", rule="scroll"
                )

        if kind is ActionKind.TYPE:
            text = str(getattr(spec, "text", "") or "")
            if len(text) > int(self.cfg.max_text_chars):
                return ActionVerdict(
                    False,
                    reason=f"typed text is {len(text)} chars — the cap is {self.cfg.max_text_chars}",
                    rule="text_length",
                    risk="high",
                )
            lowered = text.lower()
            for needle in DESTRUCTIVE_TEXT:
                if needle in lowered:
                    return ActionVerdict(
                        False,
                        reason=f"refusing to type a destructive command ({needle!r})",
                        rule="destructive_text",
                        risk="critical",
                    )

        if kind in (ActionKind.HOTKEY, ActionKind.PRESS):
            keys = [str(k).lower() for k in getattr(spec, "keys", None) or [getattr(spec, "key", "")]]
            combo = "+".join(key for key in keys if key)
            blocked = {"+".join(sorted(b.lower().split("+"))) for b in self.cfg.blocked_hotkeys}
            if "+".join(sorted(keys)) in blocked or combo in {b.lower() for b in self.cfg.blocked_hotkeys}:
                return ActionVerdict(
                    False, reason=f"hotkey '{combo}' is on the blocked list", rule="blocked_hotkey", risk="critical"
                )

        if kind is ActionKind.WAIT and float(getattr(spec, "duration", 0.0)) > float(self.cfg.max_wait_s):
            return ActionVerdict(False, reason=f"wait above the cap of {self.cfg.max_wait_s}s", rule="wait")

        risk = "low" if kind in (ActionKind.MOVE, ActionKind.WAIT, ActionKind.SCREENSHOT, ActionKind.SCROLL) else "medium"
        return ActionVerdict(True, reason="allowed", risk=risk)

    async def authorise(self, spec: ActionSpec, capability: Any = None) -> ActionVerdict:
        """Local policy **and** the six governor invariants."""
        verdict = self.check(spec)
        if not verdict.ok:
            return verdict
        try:
            await self.governor.check(spec, capability)
        except Exception as exc:  # noqa: BLE001 — SafetyViolation and anything the governor raises
            self.stats["refused"] += 1
            self.stats["governor_refused"] += 1
            rule = str(getattr(exc, "name", "")) or type(exc).__name__
            blocked = ActionVerdict(
                False,
                reason=f"safety governor refused: {exc}",
                rule="governor",
                governor_rule=rule,
                risk="high",
            )
            self._remember(spec, blocked)
            if self.bus is not None:
                self.bus.emit(
                    "safety.blocked",
                    phase=EventPhase.SECURITY,
                    layer="safety_governor",
                    rule=rule,
                    reason=str(exc)[:200],
                    action=_kind_value(spec),
                )
            return blocked
        return verdict

    @staticmethod
    def _box(raw: str) -> BoundingBox | None:
        parts = [int(part) for part in str(raw).split(",") if part.strip()]
        if len(parts) != 4:
            return None
        return BoundingBox(x=parts[0], y=parts[1], width=parts[2], height=parts[3])

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        config = self.governor.config
        return {
            "screen": f"{self.cfg.screen_width}x{self.cfg.screen_height}",
            "backend": self.cfg.backend,
            "forbidden_regions": list(self.cfg.forbidden_regions),
            "blocked_hotkeys": list(self.cfg.blocked_hotkeys),
            "max_text_chars": self.cfg.max_text_chars,
            "max_scroll": self.cfg.max_scroll,
            "max_wait_s": self.cfg.max_wait_s,
            "safety_level": str(config.level.value),
            "governor_dry_run": bool(config.dry_run),
            "governor_invariants": [getattr(inv, "name", type(inv).__name__) for inv in self.governor.invariants],
            "refusals": list(self._refusals),
            **self.stats,
        }

    def _remember(self, spec: ActionSpec, verdict: ActionVerdict) -> None:
        """Keep the last few refusals for the UI/audit trail. Never raises."""
        if verdict.ok:
            return
        try:
            self._refusals.append(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "action": _kind_value(spec),
                    "rule": verdict.rule,
                    "risk": verdict.risk,
                    "reason": str(verdict.reason)[:160],
                }
            )
        except Exception:  # noqa: BLE001 — observability must never break a refusal
            pass

    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if not self.settings.security.dry_run and self.cfg.backend != "null":
            problems.append("real mouse/keyboard actions are permitted")
        if self.cfg.screen_width <= 0 or self.cfg.screen_height <= 0:
            problems.append("screen size is not configured")
        return {"status": "degraded" if problems else "ok", "detail": {"problems": problems, **self.stats}}
