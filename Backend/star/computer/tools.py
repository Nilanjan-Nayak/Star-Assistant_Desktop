"""Computer tools — screen, mouse and keyboard as typed, policy-checked ToolSpecs.

Every tool follows the same path:

``arguments → ActionSpec (typed, validated by agent/motor) → guardrails + the six
governor invariants → motor controller (NullBackend in dry-run, pyautogui live)
→ ActionResult → audit``

Two rules worth spelling out:

* **Dry-run records intent faithfully.** The executor simulates the call, and the
  audit entry carries the exact tool + arguments (coordinates, keys, text). The
  agent *also* runs the guardrails while planning, so an impossible or forbidden
  action is refused in dry-run too — a rehearsal that catches mistakes, not a
  formality. That is why these tools are ``dry_run_safe=True`` while
  ``browser_click`` is not: a mouse action is fully described by its arguments,
  an in-page click depends on a DOM we cannot see headlessly.
* **Nothing here drives the OS directly.** The real work belongs to the existing
  ``agent/motor`` + ``agent/safety`` layers (blueprint #1: reuse, don't rebuild).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Callable

from agent.core.enums import ActionKind, MouseButton, ScrollDirection
from agent.motor.spec import spec_from_kind

from Backend.star.computer.guardrails import ComputerGuardrails
from Backend.star.computer.motor import MotorStack, build_motor, describe_action
from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger
from Backend.star.tools.spec import ToolSpec

__all__ = ["ComputerToolkit", "register_computer_tools"]

_log = star_logger("star2.computer.tools")

#: legacy desktop tools we prefer for perception (they already do OCR/vision)
_LEGACY_PERCEPTION = ("see_screen", "take_screenshot")


def _run_async(coro: Any, *, timeout: float = 60.0) -> Any:
    """Run a coroutine from a sync handler (the executor calls us in a worker thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # called from the event-loop thread (e.g. the legacy gate): give it its own loop
    with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="star2-computer") as pool:
        return pool.submit(asyncio.run, coro).result(timeout=timeout)


class ComputerToolkit:
    """Holds the motor stack + guardrails the computer tools share."""

    def __init__(
        self,
        settings: Settings,
        *,
        motor: MotorStack | None = None,
        guard: ComputerGuardrails | None = None,
        registry: Any = None,
        bus: Any = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.computer
        self.bus = bus
        self.registry = registry
        self.motor = motor or build_motor(settings, bus=bus)
        self.guard = guard or ComputerGuardrails(settings, bus=bus)
        self.stats: dict[str, int] = {"calls": 0, "refused": 0, "executed": 0, "invalid": 0, "legacy": 0}

    # ── the one action path ───────────────────────────────────────────────
    def act(self, kind: ActionKind, params: dict[str, Any], *, tool: str) -> dict[str, Any]:
        self.stats["calls"] += 1
        try:
            spec = spec_from_kind(kind, params)
        except Exception as exc:  # noqa: BLE001 — pydantic ValidationError and friends
            self.stats["invalid"] += 1
            detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            return {
                "success": False,
                "error": f"invalid {kind.value} action: {detail[:200]}",
                "result": {"kind": kind.value, "arguments": params},
            }

        verdict = _run_async(self.guard.authorise(spec), timeout=self.cfg.step_timeout_s + 5.0)
        action = describe_action(spec)
        if not verdict.ok:
            self.stats["refused"] += 1
            return {
                "success": False,
                "error": f"blocked by computer guardrails ({verdict.rule}): {verdict.reason}",
                "result": {"action": action, "verdict": verdict.public(), "tool": tool},
            }

        result = _run_async(self.motor.execute(spec), timeout=self.cfg.step_timeout_s + 5.0)
        self.stats["executed"] += 1
        payload: dict[str, Any] = {
            "success": bool(getattr(result, "success", False)),
            "result": {
                "action": action,
                "verdict": verdict.public(),
                "motor": self.motor.describe(),
                "detail": str(getattr(result, "detail", "") or "")[:300],
                "screen_changed": bool(getattr(result, "screen_changed", False)),
                "diff_ratio": round(float(getattr(result, "diff_ratio", 0.0) or 0.0), 4),
                "retries": int(getattr(result, "retries", 0) or 0),
                "duration_ms": round(float(getattr(result, "duration_ms", 0.0) or 0.0), 2),
                "action_id": str(getattr(result, "action_id", "")),
                "via": f"agent.motor.{self.motor.mode}",
            },
        }
        if not payload["success"]:
            payload["error"] = payload["result"]["detail"] or f"the motor could not complete the {kind.value}"
        return payload

    def preview(self, kind: ActionKind, params: dict[str, Any]) -> dict[str, Any]:
        """Guardrail a planned action **without** running it.

        The agent calls this before every step, so a forbidden or impossible
        action is refused in dry-run too — the rehearsal catches the mistake.
        """
        try:
            spec = spec_from_kind(kind, params)
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            return {"ok": False, "error": f"invalid {kind.value} action: {detail[:200]}", "action": {"kind": kind.value}}
        verdict = _run_async(self.guard.authorise(spec), timeout=self.cfg.step_timeout_s + 5.0)
        return {"ok": verdict.ok, "verdict": verdict.public(), "action": describe_action(spec),
                **({} if verdict.ok else {"error": f"blocked by computer guardrails ({verdict.rule}): {verdict.reason}"})}

    def perceive(self, action: str) -> dict[str, Any]:
        """Screen perception: reuse the legacy vision tools when they are importable."""
        self.stats["calls"] += 1
        for name in _LEGACY_PERCEPTION:
            spec = self.registry.get(name) if self.registry is not None else None
            handler: Callable[..., Any] | None = getattr(spec, "handler", None) if spec is not None else None
            if callable(handler):
                self.stats["legacy"] += 1
                try:
                    out = handler()
                except Exception as exc:  # noqa: BLE001 — a desktop tool may fail anywhere
                    return {"success": False, "error": f"{name} failed: {type(exc).__name__}: {exc}"[:300]}
                wrapped = dict(out) if isinstance(out, dict) else {"success": True, "result": out}
                wrapped.setdefault("success", True)
                result = wrapped.get("result")
                if not isinstance(result, dict):
                    result = {"value": result}
                result["via"] = name
                wrapped["result"] = result
                return wrapped
        return {
            "success": False,
            "error": f"{action} needs the desktop vision tools (pyautogui/OCR) — not available here",
        }

    def describe(self) -> dict[str, Any]:
        return {
            "motor": self.motor.describe(),
            "guardrails": self.guard.describe(),
            **self.stats,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────


def _coord_props(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "x": {"type": "integer", "description": "pixels from the left edge"},
        "y": {"type": "integer", "description": "pixels from the top edge"},
    }
    properties.update(extra or {})
    return {"type": "object", "properties": properties, "required": ["x", "y"]}


def register_computer_tools(
    registry: Any,
    settings: Settings | None = None,
    *,
    bus: Any = None,
    toolkit: ComputerToolkit | None = None,
) -> ComputerToolkit:
    """Add the computer tools to a :class:`StarToolRegistry`; return the toolkit."""
    cfg = settings or Settings()
    kit = toolkit or ComputerToolkit(cfg, registry=registry, bus=bus)
    kit.registry = registry
    timeout = float(cfg.computer.step_timeout_s)

    specs = (
        ToolSpec(
            name="screen_capture",
            description="Capture the screen (or a region) for verification. Perception only — moves nothing.",
            category="vision", agent="computer", risk="low",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: kit.perceive("screen capture"),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            origin="star2", tags=("computer", "vision", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="screen_read",
            description="Read what is on screen (OCR / vision) so an action can be verified against reality.",
            category="vision", agent="computer", risk="low",
            parameters={"type": "object", "properties": {"query": {"type": "string", "default": ""}}, "required": []},
            handler=lambda query="": kit.perceive("screen reading"),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            origin="star2", tags=("computer", "vision", "ocr", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="mouse_move",
            description="Move the pointer to a coordinate. Guarded by screen bounds and forbidden regions.",
            category="system", agent="computer", risk="low",
            parameters=_coord_props(),
            handler=lambda x, y: kit.act(ActionKind.MOVE, {"x": int(x), "y": int(y)}, tool="mouse_move"),
            timeout_s=timeout, idempotent=True, reversible=True, dry_run_safe=True,
            origin="star2", tags=("computer", "mouse", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="mouse_click",
            description="Click at a coordinate (left/right/middle, 1-3 clicks). Bounds + forbidden regions checked.",
            category="system", agent="computer", risk="medium",
            parameters=_coord_props({
                "button": {"type": "string", "default": "left", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
            }),
            handler=lambda x, y, button="left", clicks=1: kit.act(
                ActionKind.DOUBLE_CLICK if int(clicks) >= 2 and str(button) == "left"
                else ActionKind.RIGHT_CLICK if str(button) == "right"
                else ActionKind.CLICK,
                {"x": int(x), "y": int(y), "button": str(button), "clicks": max(1, min(3, int(clicks)))},
                tool="mouse_click",
            ),
            timeout_s=timeout, idempotent=False, reversible=False, dry_run_safe=True,
            origin="star2", tags=("computer", "mouse", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="mouse_drag",
            description="Drag from the current pointer position to a coordinate.",
            category="system", agent="computer", risk="high",
            parameters=_coord_props(),
            handler=lambda x, y: kit.act(ActionKind.DRAG, {"x": int(x), "y": int(y)}, tool="mouse_drag"),
            timeout_s=timeout, idempotent=False, reversible=False, dry_run_safe=True,
            origin="star2", tags=("computer", "mouse", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="keyboard_type",
            description="Type text into the focused field. Length-capped; destructive commands refused.",
            category="system", agent="computer", risk="medium",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "text to type"}},
                "required": ["text"],
            },
            handler=lambda text: kit.act(ActionKind.TYPE, {"text": str(text)}, tool="keyboard_type"),
            timeout_s=timeout, idempotent=False, reversible=True, dry_run_safe=True,
            origin="star2", tags=("computer", "keyboard", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="key_press",
            description="Press one key (enter, tab, esc, space, backspace, …).",
            category="system", agent="computer", risk="medium",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string", "description": "key name"}},
                "required": ["key"],
            },
            handler=lambda key: kit.act(ActionKind.PRESS, {"key": str(key)}, tool="key_press"),
            timeout_s=timeout, idempotent=False, reversible=True, dry_run_safe=True,
            origin="star2", tags=("computer", "keyboard", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="hotkey",
            description="Press a key combination (e.g. alt+tab). Blocked combinations are refused outright.",
            category="system", agent="computer", risk="high",
            parameters={
                "type": "object",
                "properties": {"keys": {"type": "array", "items": {"type": "string"}, "description": "keys in order"}},
                "required": ["keys"],
            },
            handler=lambda keys: kit.act(ActionKind.HOTKEY, {"keys": [str(k) for k in (keys or [])]}, tool="hotkey"),
            timeout_s=timeout, idempotent=False, reversible=False, dry_run_safe=True,
            confirm_above="high",
            origin="star2", tags=("computer", "keyboard", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="scroll",
            description="Scroll the wheel (optionally at a coordinate), capped per call.",
            category="system", agent="computer", risk="low",
            parameters={
                "type": "object",
                "properties": {
                    "amount": {"type": "integer", "default": 3},
                    "direction": {"type": "string", "default": "down", "enum": ["up", "down", "left", "right"]},
                    "x": {"type": "integer", "default": -1},
                    "y": {"type": "integer", "default": -1},
                },
                "required": [],
            },
            handler=lambda amount=3, direction="down", x=-1, y=-1: kit.act(
                ActionKind.SCROLL,
                {
                    "amount": int(amount),
                    "direction": str(direction),
                    **({"x": int(x), "y": int(y)} if int(x) >= 0 and int(y) >= 0 else {}),
                },
                tool="scroll",
            ),
            timeout_s=timeout, idempotent=False, reversible=True, dry_run_safe=True,
            origin="star2", tags=("computer", "mouse", "phase6"), module="Backend.star.computer.tools",
        ),
        ToolSpec(
            name="wait",
            description="Wait a bounded number of seconds (e.g. for a window to appear).",
            category="system", agent="computer", risk="low",
            parameters={
                "type": "object",
                "properties": {"seconds": {"type": "number", "default": 1.0}},
                "required": [],
            },
            handler=lambda seconds=1.0: kit.act(
                ActionKind.WAIT, {"duration": max(0.0, min(float(seconds), float(cfg.computer.max_wait_s)))}, tool="wait"
            ),
            timeout_s=timeout + float(cfg.computer.max_wait_s), idempotent=True, reversible=True, dry_run_safe=True,
            origin="star2", tags=("computer", "phase6"), module="Backend.star.computer.tools",
        ),
    )

    for spec in specs:
        # the registry announces every registration on the bus (one rich event per tool)
        registry.register(spec, replace=True)
    _log.info("star2.computer.tools.registered count=%s motor=%s", len(specs), kit.motor.mode)
    return kit


#: re-exported so callers can build directions/buttons without importing agent.core
DIRECTIONS = tuple(item.value for item in ScrollDirection)
BUTTONS = tuple(item.value for item in MouseButton)
