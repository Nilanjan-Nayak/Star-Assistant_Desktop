"""Real pyautogui backend. Exhaustive ``match`` on the ActionSpec union."""

from __future__ import annotations

import asyncio

from agent.core.enums import OSFamily
from agent.core.errors import BackendUnavailable, MotorError
from agent.core.exhaustiveness import assert_never
from agent.geometry.monitor import CURRENT_OS
from agent.motor.spec import (
    ActionSpec,
    ClickAction,
    DoubleClickAction,
    DragAction,
    HotkeyAction,
    MoveAction,
    PressAction,
    RightClickAction,
    ScreenshotAction,
    ScrollAction,
    TypeAction,
    WaitAction,
)


class PyAutoGUIBackend:
    def __init__(self) -> None:
        try:
            import pyautogui
        except ImportError as exc:
            raise BackendUnavailable("pyautogui not installed") from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.03
        self._pg = pyautogui

    async def execute(self, spec: ActionSpec) -> None:
        pg = self._pg
        try:
            match spec:
                case ClickAction(coord=coord, button=button, clicks=clicks):
                    pg.click(coord.x, coord.y, button=button.value, clicks=clicks)
                case DoubleClickAction(coord=coord):
                    pg.doubleClick(coord.x, coord.y)
                case RightClickAction(coord=coord):
                    pg.rightClick(coord.x, coord.y)
                case MoveAction(coord=coord, duration=duration):
                    pg.moveTo(coord.x, coord.y, duration=duration)
                case DragAction(coord=coord, duration=duration, button=button):
                    pg.dragTo(coord.x, coord.y, duration=duration, button=button.value)
                case TypeAction(text=text):
                    self._type_unicode_safe(text)
                case PressAction(key=key):
                    pg.press(key)
                case HotkeyAction(keys=keys):
                    pg.hotkey(*keys)
                case ScrollAction(amount=amount, coord=coord):
                    x = coord.x if coord is not None else None
                    y = coord.y if coord is not None else None
                    pg.scroll(amount, x=x, y=y)
                case WaitAction(duration=duration):
                    await asyncio.sleep(duration)
                case ScreenshotAction():
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
        except MotorError:
            raise
        except Exception as exc:
            raise MotorError(f"pyautogui error: {exc}", kind=spec.kind.value) from exc

    def _type_unicode_safe(self, text: str) -> None:
        try:
            import pyperclip

            pyperclip.copy(text)
            mod = "command" if CURRENT_OS is OSFamily.MACOS else "ctrl"
            self._pg.hotkey(mod, "v")
        except ImportError:
            ascii_text = text.encode("ascii", "ignore").decode()
            self._pg.typewrite(ascii_text, interval=0.02)
