"""All enums used across the agent. Frozen, exhaustive, string-valued."""

from __future__ import annotations

import enum
from typing import Final


@enum.unique
class OSFamily(str, enum.Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    UNKNOWN = "unknown"


@enum.unique
class ElementRole(str, enum.Enum):
    BUTTON = "button"
    LINK = "link"
    TEXTBOX = "textbox"
    IMAGE = "image"
    ICON = "icon"
    MENU = "menu"
    MENU_ITEM = "menu_item"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    LIST = "list"
    LIST_ITEM = "list_item"
    TAB = "tab"
    HEADING = "heading"
    TEXT = "text"
    SLIDER = "slider"
    DIALOG = "dialog"
    WINDOW = "window"
    TOOLBAR = "toolbar"
    SCROLLBAR = "scrollbar"
    UNKNOWN = "unknown"


@enum.unique
class MouseButton(str, enum.Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


@enum.unique
class ScrollDirection(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


@enum.unique
class ActionKind(str, enum.Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    MOVE = "move"
    DRAG = "drag"
    TYPE = "type"
    PRESS = "press"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    WAIT = "wait"
    SCREENSHOT = "screenshot"


@enum.unique
class PerceptionSource(str, enum.Enum):
    A11Y = "a11y"
    OCR = "ocr"
    TEMPLATE = "template"
    VISION_LLM = "vision_llm"
    DOM = "dom"
    CACHE = "cache"


@enum.unique
class SafetyLevel(str, enum.Enum):
    PERMISSIVE = "permissive"
    NORMAL = "normal"
    STRICT = "strict"
    PARANOID = "paranoid"


@enum.unique
class AgentState(str, enum.Enum):
    IDLE = "idle"
    PERCEIVING = "perceiving"
    PLANNING = "planning"
    ACTING = "acting"
    VERIFYING = "verifying"
    REFLECTING = "reflecting"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


@enum.unique
class BreakerState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@enum.unique
class HealthStatus(str, enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAIL = "fail"


@enum.unique
class MemoryKind(str, enum.Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODE = "episode"


@enum.unique
class LogLevel(str, enum.Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# Pointer actions that must carry a coordinate.
COORD_REQUIRED_ACTIONS: Final[frozenset[ActionKind]] = frozenset(
    {
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
        ActionKind.MOVE,
        ActionKind.DRAG,
    }
)

# Kinds that never mutate pixels in a way we can (or should) verify.
NO_SCREEN_VERIFY_ACTIONS: Final[frozenset[ActionKind]] = frozenset(
    {
        ActionKind.WAIT,
        ActionKind.MOVE,
        ActionKind.SCREENSHOT,
    }
)

# Terminal FSM states — once entered, only IDLE is a legal successor.
TERMINAL_STATES: Final[frozenset[AgentState]] = frozenset(
    {
        AgentState.SUCCEEDED,
        AgentState.FAILED,
        AgentState.ABORTED,
    }
)
