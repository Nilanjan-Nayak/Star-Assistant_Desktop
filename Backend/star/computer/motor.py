"""The motor stack — STAR 2.0's handle on the *existing* ``agent/`` motor layer.

Nothing here is a new implementation of mouse/keyboard control. It assembles the
pieces the repository already has:

* ``agent.motor.controller.MotorController``  — retries, circuit breakers, verify
* ``agent.motor.backends.NullBackend``        — records actions, executes nothing
* ``agent.motor.backends.PyAutoGUIBackend``   — the real thing (desktop only)
* ``agent.world`` (``FakeCapture``/``ScreenCapture`` + ``WorldModel``) — the screen
  state the controller verifies against

Selection rule (blueprint #3, dry-run first):

``backend="auto"``  → real backend only when dry-run is OFF *and* pyautogui imports
``backend="null"``  → always the recording backend (tests, headless, CI)
anything else       → fall back to null and say so in ``mode_note``
"""

from __future__ import annotations

from typing import Any

from agent.geometry.monitor import MonitorInfo
from agent.geometry.raster import solid
from agent.motor.backends.null_backend import NullBackend
from agent.motor.controller import MotorController
from agent.motor.spec import ActionSpec
from agent.world.differ import ScreenDiffer
from agent.world.fake import FakeCapture
from agent.world.model import WorldModel

from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger

__all__ = ["MotorStack", "build_motor", "describe_action"]

_log = star_logger("star2.computer.motor")


def describe_action(spec: ActionSpec) -> dict[str, Any]:
    """A JSON-safe, honest description of one physical action (audit + dry-run preview)."""
    kind = getattr(spec, "kind", "")
    data: dict[str, Any] = {"kind": str(getattr(kind, "value", kind))}
    coord = getattr(spec, "coord", None)
    if coord is not None:
        data["coord"] = {"x": int(coord.x), "y": int(coord.y)}
    for field in ("text", "key", "keys", "amount", "direction", "button", "clicks", "duration"):
        value = getattr(spec, field, None)
        if value is None:
            continue
        data[field] = value.value if hasattr(value, "value") else value
    data["verify"] = bool(getattr(spec, "verify", True))
    data["action_id"] = str(getattr(spec, "action_id", ""))
    return data


class MotorStack:
    """Controller + backend + world model, with an honest label for which one it is."""

    def __init__(
        self,
        controller: MotorController,
        backend: Any,
        world: WorldModel,
        *,
        mode: str,
        dry_run: bool,
        note: str = "",
    ) -> None:
        self.controller = controller
        self.backend = backend
        self.world = world
        self.mode = mode                      # "null" | "pyautogui"
        self.dry_run = dry_run
        self.note = note
        self.stats: dict[str, int] = {"actions": 0, "succeeded": 0, "failed": 0}

    async def execute(self, spec: ActionSpec) -> Any:
        """Run one action through the controller (which retries, verifies, breaks).

        In null mode the screen is fake, so pixel verification can never succeed;
        asking for it would only burn retries and report a lie. Recording *is* the
        verification there — the audit trail holds the intended action.
        """
        self.stats["actions"] += 1
        if self.mode == "null" and bool(getattr(spec, "verify", False)):
            spec = spec.model_copy(update={"verify": False})
        result = await self.controller.execute(spec)
        self.stats["succeeded" if getattr(result, "success", False) else "failed"] += 1
        return result

    def observe(self) -> Any:
        return self.world.observe()

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "backend": type(self.backend).__name__,
            "dry_run": self.dry_run,
            "note": self.note,
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if self.mode == "pyautogui" and not self.dry_run:
            problems.append("real mouse/keyboard backend is live")
        if self.stats["actions"] and self.stats["failed"] > self.stats["succeeded"]:
            problems.append("more motor failures than successes")
        return {"status": "degraded" if problems else "ok", "detail": {"problems": problems, **self.describe()}}


def build_motor(settings: Settings | None = None, *, dry_run: bool | None = None, bus: Any = None) -> MotorStack:
    """Assemble the stack. Never raises — a missing pyautogui just means null mode."""
    cfg = settings or Settings()
    wanted = str(cfg.computer.backend or "auto").lower()
    effective_dry_run = bool(cfg.security.dry_run) if dry_run is None else bool(dry_run)
    width, height = max(64, int(cfg.computer.screen_width)), max(48, int(cfg.computer.screen_height))

    world = WorldModel(
        FakeCapture(raster=solid(min(width, 512), min(height, 384), (24, 24, 24)),
                    monitor=MonitorInfo(index=1, x=0, y=0, width=width, height=height, is_primary=True, name="star-virtual")),
        ScreenDiffer(),
    )

    if wanted in ("auto", "pyautogui") and not effective_dry_run:
        try:
            from agent.motor.backends.pyautogui_backend import PyAutoGUIBackend
            from agent.world.capture import ScreenCapture

            backend: Any = PyAutoGUIBackend()
            world = WorldModel(ScreenCapture(), ScreenDiffer())
            stack = MotorStack(
                MotorController(backend, world, verify_delay=cfg.computer.verify_delay_s,
                                change_threshold=cfg.computer.change_threshold),
                backend,
                world,
                mode="pyautogui",
                dry_run=False,
                note="real desktop control is LIVE — every action still passes policy",
            )
            if bus is not None:
                bus.emit("agent.ready", phase="task", agent="computer", mode="pyautogui", dry_run=False)
            _log.warning("star2.computer.motor.live backend=pyautogui")
            return stack
        except Exception as exc:  # noqa: BLE001 — no pyautogui / no display / no permission
            note = f"pyautogui backend unavailable ({type(exc).__name__}: {exc}) — recording instead"
            _log.info("star2.computer.motor.fallback reason=%s", note)
    else:
        note = (
            "dry-run: actions are recorded, nothing moves"
            if effective_dry_run
            else f"backend='{wanted}' — recording only"
        )

    null_backend = NullBackend()
    stack = MotorStack(
        MotorController(null_backend, world, verify_delay=0.0, change_threshold=cfg.computer.change_threshold),
        null_backend,
        world,
        mode="null",
        dry_run=effective_dry_run,
        note=note,
    )
    if bus is not None:
        bus.emit("agent.ready", phase="task", agent="computer", mode="null", dry_run=effective_dry_run)
    return stack
