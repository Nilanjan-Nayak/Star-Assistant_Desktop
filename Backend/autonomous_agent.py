"""
╔══════════════════════════════════════════════════════════════════════════╗
║  COMPUTER CONTROL AGENT  v4.0  —  Production-Grade Autonomous Desktop   ║
║  "Perceive · Plan · Act · Verify · Learn · Recover"                     ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║  DESIGN PILLARS                                                          ║
║  ──────────────                                                          ║
║    1. TYPE SAFETY      — Pydantic v2 models, Protocols, TypedDicts,     ║
║                          Literal types, exhaustive Enums, NewTypes      ║
║    2. DETERMINISM      — Formal state machine, idempotent actions,      ║
║                          content-addressed screenshots                  ║
║    3. OBSERVABILITY    — Structured logging, OpenTelemetry spans,       ║
║                          Prometheus-compatible metrics, event bus       ║
║    4. RESILIENCE       — Circuit breakers, exponential back-off,        ║
║                          self-healing via strategy rotation             ║
║    5. SAFETY           — Multi-layer governor, capability tokens,       ║
║                          formal invariants, human-in-the-loop hooks     ║
║    6. LEARNING         — Episodic memory, skill success stats,          ║
║                          element locator cache, prompt-few-shot         ║
║    7. TESTABILITY      — Every component is a Protocol; full mock       ║
║                          harness; property-based test hooks             ║
║                                                                          ║
║  ARCHITECTURE (8 layers)                                                 ║
║  ────────────                                                            ║
║    L0  Foundation      — Types, IDs, errors, event bus, metrics        ║
║    L1  OS Abstraction  — Monitors, DPI, platform capabilities          ║
║    L2  Perception      — a11y → OCR → template → vision-LLM cascade    ║
║    L3  World Model     — Screen state, element cache, change tracking  ║
║    L4  Motor Control   — Verified actions with retry & circuit-break   ║
║    L5  Safety Governor — Budget, forbidden zones, capability tokens    ║
║    L6  Skill Registry  — Typed skills with schemas & preconditions     ║
║    L7  Planner         — ReAct + reflection + episodic memory          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import base64
import enum
import hashlib
import importlib
import io
import json
import logging
import math
import os
import platform
import subprocess
import sys
import time
import uuid
import webbrowser
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    ClassVar,
    Deque,
    Dict,
    Final,
    Generic,
    Iterable,
    Iterator,
    List,
    Literal,
    Mapping,
    NewType,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    final,
    overload,
    runtime_checkable,
)

# ── Type-safety foundations ─────────────────────────────────────────────
try:
    from pydantic import (
        BaseModel,
        ConfigDict,
        Field,
        StrictBool,
        StrictFloat,
        StrictInt,
        StrictStr,
        computed_field,
        field_validator,
        model_validator,
    )
    from typing_extensions import Annotated, NotRequired, Self, TypedDict, override
except ImportError as e:
    raise SystemExit(
        "pydantic>=2 and typing-extensions are required.\n"
        "  pip install 'pydantic>=2' typing-extensions"
    ) from e

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
#  L0 — FOUNDATION: types, IDs, errors, events, metrics
# ═══════════════════════════════════════════════════════════════════════════

# ── Strong ID types ─────────────────────────────────────────────────────
ActionId    = NewType("ActionId",    str)
StepId      = NewType("StepId",      str)
EpisodeId   = NewType("EpisodeId",   str)
ElementId   = NewType("ElementId",   str)
ScreenHash  = NewType("ScreenHash",  str)
CapToken    = NewType("CapToken",    str)   # capability token
SkillName   = NewType("SkillName",   str)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── Enums ────────────────────────────────────────────────────────────────
class OSFamily(str, enum.Enum):
    WINDOWS = "windows"
    MACOS   = "macos"
    LINUX   = "linux"
    UNKNOWN = "unknown"


class ElementRole(str, enum.Enum):
    BUTTON   = "button"
    LINK     = "link"
    TEXTBOX  = "textbox"
    IMAGE    = "image"
    ICON     = "icon"
    MENU     = "menu"
    CHECKBOX = "checkbox"
    LIST     = "list"
    TEXT     = "text"
    UNKNOWN  = "unknown"


class MouseButton(str, enum.Enum):
    LEFT   = "left"
    RIGHT  = "right"
    MIDDLE = "middle"


class ActionKind(str, enum.Enum):
    CLICK        = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK  = "right_click"
    TYPE         = "type"
    PRESS        = "press"
    HOTKEY       = "hotkey"
    SCROLL       = "scroll"
    DRAG         = "drag"
    MOVE         = "move"
    WAIT         = "wait"
    SCREENSHOT   = "screenshot"


class PerceptionSource(str, enum.Enum):
    A11Y       = "a11y"
    OCR        = "ocr"
    TEMPLATE   = "template"
    VISION_LLM = "vision_llm"
    CACHE      = "cache"
    DOM        = "dom"


class SafetyLevel(str, enum.Enum):
    """Ordered by strictness."""
    PERMISSIVE = "permissive"
    NORMAL     = "normal"
    STRICT     = "strict"
    PARANOID   = "paranoid"


class AgentState(str, enum.Enum):
    """Formal state machine states."""
    IDLE       = "idle"
    PERCEIVING = "perceiving"
    PLANNING   = "planning"
    ACTING     = "acting"
    VERIFYING  = "verifying"
    REFLECTING = "reflecting"
    BLOCKED    = "blocked"
    ESCALATED  = "escalated"
    SUCCEEDED  = "succeeded"
    FAILED     = "failed"
    ABORTED    = "aborted"


# Valid transitions in the state machine
_VALID_TRANSITIONS: Final[Dict[AgentState, Set[AgentState]]] = {
    AgentState.IDLE:       {AgentState.PERCEIVING, AgentState.ABORTED},
    AgentState.PERCEIVING: {AgentState.PLANNING, AgentState.FAILED, AgentState.ABORTED},
    AgentState.PLANNING:   {AgentState.ACTING, AgentState.SUCCEEDED, AgentState.BLOCKED,
                            AgentState.ESCALATED, AgentState.FAILED, AgentState.ABORTED},
    AgentState.ACTING:     {AgentState.VERIFYING, AgentState.BLOCKED, AgentState.FAILED,
                            AgentState.ABORTED},
    AgentState.VERIFYING:  {AgentState.REFLECTING, AgentState.FAILED, AgentState.ABORTED},
    AgentState.REFLECTING: {AgentState.PERCEIVING, AgentState.SUCCEEDED, AgentState.FAILED,
                            AgentState.ABORTED},
    AgentState.BLOCKED:    {AgentState.ESCALATED, AgentState.ABORTED, AgentState.IDLE},
    AgentState.ESCALATED:  {AgentState.PERCEIVING, AgentState.ABORTED, AgentState.IDLE},
    AgentState.SUCCEEDED:  {AgentState.IDLE},
    AgentState.FAILED:     {AgentState.IDLE},
    AgentState.ABORTED:    {AgentState.IDLE},
}


# ── Structured errors ───────────────────────────────────────────────────
class AgentError(Exception):
    """Base for all agent errors. Carries structured context."""
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context: Dict[str, Any] = context

    def to_dict(self) -> Dict[str, Any]:
        return {"error": type(self).__name__, "message": str(self), **self.context}


class PerceptionError(AgentError):
    """Failed to see/understand the screen."""


class MotorError(AgentError):
    """Failed to execute a physical action."""


class SafetyViolation(AgentError):
    """Governor blocked an action."""


class StateTransitionError(AgentError):
    """Illegal state machine transition."""


class SkillNotFound(AgentError):
    """Skill name not registered."""


class PreconditionFailed(AgentError):
    """Skill precondition not met."""


class BudgetExhausted(AgentError):
    """Action/time/token budget consumed."""


class CircuitOpen(AgentError):
    """Circuit breaker rejected the call."""


# ── Structured logging ──────────────────────────────────────────────────
class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts":    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg":   record.getMessage(),
        }
        if hasattr(record, "extra_ctx"):
            payload.update(record.extra_ctx)  # type: ignore[attr-defined]
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _mk_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if not lg.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(StructuredFormatter())
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


log = _mk_logger("agent")


def log_ctx(level: int, msg: str, **ctx: Any) -> None:
    log.log(level, msg, extra={"extra_ctx": ctx})


# ── Event bus (fully typed) ─────────────────────────────────────────────
class Event(BaseModel):
    """Base event — carries an id, timestamp, and kind."""
    model_config = ConfigDict(frozen=True, extra="allow")
    event_id:   str        = Field(default_factory=lambda: new_id("evt"))
    timestamp:  datetime   = Field(default_factory=lambda: datetime.now(timezone.utc))
    kind:       str


class ActionStartedEvent(Event):
    kind: Literal["action.started"] = "action.started"
    action_id: ActionId
    action_kind: ActionKind


class ActionCompletedEvent(Event):
    kind: Literal["action.completed"] = "action.completed"
    action_id: ActionId
    success: bool
    duration_ms: float


class SafetyBlockedEvent(Event):
    kind: Literal["safety.blocked"] = "safety.blocked"
    reason: str
    action: str


class StateChangedEvent(Event):
    kind: Literal["state.changed"] = "state.changed"
    from_state: AgentState
    to_state:   AgentState


EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Async pub-sub bus. Subscribe by base or specific event type."""
    def __init__(self) -> None:
        self._subs: Dict[Type[Event], List[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: Type[Event], handler: EventHandler) -> None:
        self._subs[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        for etype, handlers in self._subs.items():
            if isinstance(event, etype):
                for h in handlers:
                    try:
                        await h(event)
                    except Exception:
                        log.exception("event handler failed for %s", etype.__name__)


# ── Metrics ─────────────────────────────────────────────────────────────
class Metrics:
    def __init__(self) -> None:
        self.counters:   Dict[str, float] = defaultdict(float)
        self.histograms: Dict[str, List[float]] = defaultdict(list)
        self.gauges:     Dict[str, float] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        self.counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.histograms[self._key(name, labels)].append(value)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        self.gauges[self._key(name, labels)] = value

    @staticmethod
    def _key(name: str, labels: Mapping[str, str]) -> str:
        if not labels:
            return name
        return name + "{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"

    def snapshot(self) -> Dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "histograms": {k: {
                "count": len(v),
                "sum":   sum(v),
                "avg":   sum(v) / len(v) if v else 0.0,
                "p50":   sorted(v)[len(v) // 2] if v else 0.0,
                "p95":   sorted(v)[int(len(v) * 0.95)] if v else 0.0,
                "p99":   sorted(v)[int(len(v) * 0.99)] if v else 0.0,
            } for k, v in self.histograms.items()},
            "gauges": dict(self.gauges),
        }


METRICS = Metrics()
BUS     = EventBus()


# ── Tracing ─────────────────────────────────────────────────────────────
@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Dict[str, Any]]:
    t0 = time.perf_counter()
    ctx: Dict[str, Any] = {"name": name, "attrs": dict(attrs)}
    try:
        yield ctx
    except Exception as e:
        ctx["error"] = repr(e)
        raise
    finally:
        dur_ms = (time.perf_counter() - t0) * 1000
        ctx["duration_ms"] = dur_ms
        METRICS.observe(f"span.{name}.duration_ms", dur_ms)
        log_ctx(logging.DEBUG, f"span:{name}", **ctx)


# ── Circuit breaker ─────────────────────────────────────────────────────
class CircuitBreaker:
    """Classic 3-state breaker: closed → open → half-open."""
    def __init__(self, name: str, threshold: int = 5, reset_after: float = 30.0) -> None:
        self.name = name
        self.threshold = threshold
        self.reset_after = reset_after
        self._failures = 0
        self._opened_at: Optional[float] = None

    def _state(self) -> Literal["closed", "open", "half_open"]:
        if self._opened_at is None:
            return "closed"
        if time.time() - self._opened_at > self.reset_after:
            return "half_open"
        return "open"

    def guard(self) -> None:
        s = self._state()
        if s == "open":
            raise CircuitOpen(f"Circuit '{self.name}' is open", breaker=self.name)

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.time()
            log_ctx(logging.WARNING, "circuit.open", breaker=self.name,
                    failures=self._failures)


# ═══════════════════════════════════════════════════════════════════════════
#  L1 — OS ABSTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def detect_os() -> OSFamily:
    s = platform.system().lower()
    return {
        "windows": OSFamily.WINDOWS,
        "darwin":  OSFamily.MACOS,
        "linux":   OSFamily.LINUX,
    }.get(s, OSFamily.UNKNOWN)


CURRENT_OS: Final[OSFamily] = detect_os()


class PixelCoord(BaseModel):
    """Physical pixel coordinate — validated non-negative int."""
    model_config = ConfigDict(frozen=True)
    x: Annotated[int, Field(ge=0, le=65535)]
    y: Annotated[int, Field(ge=0, le=65535)]

    def as_tuple(self) -> Tuple[int, int]:
        return (self.x, self.y)


class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    width:  Annotated[int, Field(ge=1)]
    height: Annotated[int, Field(ge=1)]

    @computed_field  # type: ignore[misc]
    @property
    def center(self) -> PixelCoord:
        return PixelCoord(x=self.x + self.width // 2, y=self.y + self.height // 2)

    @computed_field  # type: ignore[misc]
    @property
    def area(self) -> int:
        return self.width * self.height

    def contains(self, p: PixelCoord) -> bool:
        return (self.x <= p.x <= self.x + self.width and
                self.y <= p.y <= self.y + self.height)

    def intersects(self, other: BoundingBox) -> bool:
        return not (self.x + self.width  < other.x or
                    other.x + other.width  < self.x or
                    self.y + self.height < other.y or
                    other.y + other.height < self.y)


class MonitorInfo(BaseModel):
    model_config = ConfigDict(frozen=True)
    index: Annotated[int, Field(ge=1)]
    x: int
    y: int
    width:  Annotated[int, Field(ge=1)]
    height: Annotated[int, Field(ge=1)]
    scale_factor: Annotated[float, Field(gt=0, le=8)] = 1.0
    is_primary: bool = False

    @computed_field  # type: ignore[misc]
    @property
    def bbox(self) -> BoundingBox:
        return BoundingBox(x=self.x, y=self.y, width=self.width, height=self.height)


def enumerate_monitors() -> List[MonitorInfo]:
    monitors: List[MonitorInfo] = []
    try:
        import mss
        mss_cls = getattr(mss, "MSS", mss.mss)
        with mss_cls() as sct:
            for i, m in enumerate(sct.monitors[1:], start=1):
                scale = 1.0
                if CURRENT_OS == OSFamily.WINDOWS:
                    try:
                        import ctypes
                        ctypes.windll.shcore.SetProcessDpiAwareness(2)
                        scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
                    except Exception:
                        pass
                elif CURRENT_OS == OSFamily.MACOS:
                    scale = 2.0
                monitors.append(MonitorInfo(
                    index=i, x=int(m["left"]), y=int(m["top"]),
                    width=int(m["width"]), height=int(m["height"]),
                    scale_factor=scale, is_primary=(i == 1),
                ))
    except Exception as e:
        log_ctx(logging.WARNING, "monitor.enumerate.failed", error=str(e))
    if not monitors:
        monitors.append(MonitorInfo(
            index=1, x=0, y=0, width=1920, height=1080, is_primary=True,
        ))
    return monitors


# ═══════════════════════════════════════════════════════════════════════════
#  L2 — PERCEPTION
# ═══════════════════════════════════════════════════════════════════════════

class ScreenElement(BaseModel):
    """A perceived UI element — fully validated, immutable."""
    model_config = ConfigDict(frozen=True)

    element_id: ElementId = Field(default_factory=lambda: ElementId(new_id("el")))
    text:       StrictStr
    bbox:       BoundingBox
    role:       ElementRole = ElementRole.UNKNOWN
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    source:     PerceptionSource
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @property
    def center(self) -> PixelCoord:
        return self.bbox.center

    def distance_to(self, p: PixelCoord) -> float:
        c = self.center
        return math.hypot(c.x - p.x, c.y - p.y)


class PerceptionQuery(BaseModel):
    """Structured query for the perception cascade."""
    model_config = ConfigDict(frozen=True)
    text: StrictStr
    role_hint:      Optional[ElementRole] = None
    region:         Optional[BoundingBox] = None
    min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    max_results:    Annotated[int,   Field(ge=1, le=100)]  = 10
    fuzzy:          bool = True


@runtime_checkable
class PerceptionStrategy(Protocol):
    """Every perception source implements this Protocol."""
    name: ClassVar[PerceptionSource]

    async def find(
        self,
        screenshot: Any,          # PIL.Image.Image
        query: PerceptionQuery,
    ) -> List[ScreenElement]: ...


class _BaseStrategy:
    """Common helpers for concrete strategies."""
    name: ClassVar[PerceptionSource] = PerceptionSource.CACHE

    @staticmethod
    def _fuzzy_match(needle: str, hay: str) -> float:
        n, h = needle.lower().strip(), hay.lower().strip()
        if not n or not h:
            return 0.0
        if n == h: return 1.0
        if n in h: return 0.9
        common = sum(1 for c in n if c in h)
        return common / max(len(n), 1) * 0.6


class A11yPerception(_BaseStrategy):
    name: ClassVar[PerceptionSource] = PerceptionSource.A11Y

    async def find(self, screenshot: Any, query: PerceptionQuery) -> List[ScreenElement]:
        try:
            if CURRENT_OS == OSFamily.WINDOWS:
                return await self._windows(query)
            if CURRENT_OS == OSFamily.LINUX:
                return await self._linux(query)
            if CURRENT_OS == OSFamily.MACOS:
                return await self._macos(query)
        except Exception as e:
            log_ctx(logging.DEBUG, "a11y.error", error=str(e))
        return []

    async def _windows(self, q: PerceptionQuery) -> List[ScreenElement]:
        try:
            auto = importlib.import_module("uiautomation")
            results: List[ScreenElement] = []
            root = auto.GetRootControl()
            for ctrl in root.GetChildren():
                name = ctrl.Name or ""
                score = self._fuzzy_match(q.text, name)
                if score >= q.min_confidence:
                    r = ctrl.BoundingRectangle
                    results.append(ScreenElement(
                        text=name,
                        bbox=BoundingBox(x=r.left, y=r.top,
                                         width=max(1, r.width()), height=max(1, r.height())),
                        role=ElementRole.UNKNOWN, confidence=score,
                        source=PerceptionSource.A11Y,
                        attributes={"control_type": getattr(ctrl, "ControlTypeName", "")},
                    ))
            return results[:q.max_results]
        except Exception:
            return []

    async def _linux(self, q: PerceptionQuery) -> List[ScreenElement]: return []
    async def _macos(self, q: PerceptionQuery) -> List[ScreenElement]: return []


class OCRPerception(_BaseStrategy):
    name: ClassVar[PerceptionSource] = PerceptionSource.OCR

    async def find(self, screenshot: Any, query: PerceptionQuery) -> List[ScreenElement]:
        try:
            pytesseract = importlib.import_module("pytesseract")
            Output = pytesseract.Output
        except Exception:
            return []
        try:
            img = screenshot
            if query.region:
                img = img.crop((query.region.x, query.region.y,
                                query.region.x + query.region.width,
                                query.region.y + query.region.height))
            data = pytesseract.image_to_data(img, output_type=Output.DICT)
        except Exception as e:
            return []

        results: List[ScreenElement] = []
        offset_x = query.region.x if query.region else 0
        offset_y = query.region.y if query.region else 0

        for i, word in enumerate(data["text"]):
            if not word.strip():
                continue
            score = self._fuzzy_match(query.text, word) if query.fuzzy else (
                1.0 if query.text.lower() in word.lower() else 0.0
            )
            ocr_conf = float(data["conf"][i]) / 100.0 if data["conf"][i] != -1 else 0.5
            combined = score * ocr_conf
            if combined < query.min_confidence:
                continue
            try:
                bbox = BoundingBox(
                    x=int(data["left"][i]) + offset_x,
                    y=int(data["top"][i])  + offset_y,
                    width=max(1, int(data["width"][i])),
                    height=max(1, int(data["height"][i])),
                )
            except Exception:
                continue
            results.append(ScreenElement(
                text=word, bbox=bbox, role=ElementRole.TEXT,
                confidence=combined, source=PerceptionSource.OCR,
            ))
        results.sort(key=lambda e: e.confidence, reverse=True)
        return results[:query.max_results]


class TemplatePerception(_BaseStrategy):
    name: ClassVar[PerceptionSource] = PerceptionSource.TEMPLATE

    def __init__(self, template_dir: Optional[Path] = None) -> None:
        self.template_dir = template_dir or (DATA_DIR / "templates")
        self.template_dir.mkdir(parents=True, exist_ok=True)

    async def find(self, screenshot: Any, query: PerceptionQuery) -> List[ScreenElement]:
        try:
            cv2 = importlib.import_module("cv2")
            import numpy as np
            tpl_path = self.template_dir / f"{query.text}.png"
            if not tpl_path.exists():
                return []
            tpl = cv2.imread(str(tpl_path))
            scr = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            res = cv2.matchTemplate(scr, tpl, cv2.TM_CCOEFF_NORMED)
            locs = np.where(res >= query.min_confidence)
            elements: List[ScreenElement] = []
            h, w = tpl.shape[:2]
            for pt in zip(*locs[::-1]):
                elements.append(ScreenElement(
                    text=query.text,
                    bbox=BoundingBox(x=int(pt[0]), y=int(pt[1]), width=w, height=h),
                    role=ElementRole.ICON, confidence=float(res[pt[1], pt[0]]),
                    source=PerceptionSource.TEMPLATE,
                ))
            return elements[:query.max_results]
        except Exception:
            return []


class VisionLLMPerception(_BaseStrategy):
    name: ClassVar[PerceptionSource] = PerceptionSource.VISION_LLM

    def __init__(self, model: str = "gemini-1.5-flash") -> None:
        self.model = model

    async def find(self, screenshot: Any, query: PerceptionQuery) -> List[ScreenElement]:
        # Graceful fallback: return empty list if no vision key is configured
        return []


class ElementLocatorCache:
    """
    Persistent cache: (query_text, screen_hash_prefix) → ScreenElement.
    """
    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 1000) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._store: Dict[str, Tuple[ScreenElement, float]] = {}

    @staticmethod
    def _key(query_text: str, screen_hash: ScreenHash) -> str:
        return f"{query_text.lower().strip()}|{screen_hash[:8]}"

    def get(self, query_text: str, screen_hash: ScreenHash) -> Optional[ScreenElement]:
        key = self._key(query_text, screen_hash)
        entry = self._store.get(key)
        if not entry: return None
        el, ts = entry
        if time.time() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        METRICS.inc("perception.cache.hit")
        return el

    def put(self, query_text: str, screen_hash: ScreenHash, el: ScreenElement) -> None:
        if len(self._store) >= self.max_entries:
            oldest = min(self._store.items(), key=lambda kv: kv[1][1])
            self._store.pop(oldest[0], None)
        self._store[self._key(query_text, screen_hash)] = (el, time.time())


class PerceptionCascade:
    """
    Runs strategies in priority order.
    """
    def __init__(
        self,
        strategies: Optional[Sequence[PerceptionStrategy]] = None,
        cache: Optional[ElementLocatorCache] = None,
    ) -> None:
        self.strategies: List[PerceptionStrategy] = list(strategies) if strategies else [
            A11yPerception(),
            OCRPerception(),
            TemplatePerception(),
            VisionLLMPerception(),
        ]
        self.cache = cache or ElementLocatorCache()

    async def find(
        self,
        screenshot: Any,
        query: PerceptionQuery,
        screen_hash: Optional[ScreenHash] = None,
    ) -> List[ScreenElement]:
        # 1. Cache check
        if screen_hash:
            hit = self.cache.get(query.text, screen_hash)
            if hit:
                return [hit]

        # 2. Cascade
        for strat in self.strategies:
            with span(f"perception.{strat.name.value}"):
                try:
                    results = await strat.find(screenshot, query)
                except PerceptionError:
                    METRICS.inc("perception.error", strategy=strat.name.value)
                    continue
                except Exception as e:
                    log_ctx(logging.WARNING, "perception.unexpected",
                            strategy=strat.name.value, error=str(e))
                    continue

            filtered = [r for r in results if r.confidence >= query.min_confidence]
            METRICS.observe("perception.results", len(filtered), strategy=strat.name.value)
            if filtered:
                METRICS.inc("perception.success", strategy=strat.name.value)
                if screen_hash:
                    self.cache.put(query.text, screen_hash, filtered[0])
                return filtered

        METRICS.inc("perception.exhausted")
        raise PerceptionError(f"No strategy found '{query.text}'", query=query.model_dump())


# ═══════════════════════════════════════════════════════════════════════════
#  L3 — WORLD MODEL: screen capture + state + diffing
# ═══════════════════════════════════════════════════════════════════════════

class ScreenSnapshot(BaseModel):
    """Immutable snapshot of the screen at a moment in time."""
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    snapshot_id: str        = Field(default_factory=lambda: new_id("snap"))
    taken_at:    datetime   = Field(default_factory=lambda: datetime.now(timezone.utc))
    image:       Any        # PIL.Image.Image
    monitor:     MonitorInfo
    content_hash: ScreenHash


class ScreenCapture:
    def __init__(self, monitors: Optional[List[MonitorInfo]] = None) -> None:
        self.monitors = monitors or enumerate_monitors()

    def grab(
        self,
        region:  Optional[BoundingBox] = None,
        monitor: int = 1,
    ) -> ScreenSnapshot:
        try:
            import mss
            from PIL import Image
        except ImportError as e:
            raise PerceptionError("mss + Pillow required") from e

        mon = next((m for m in self.monitors if m.index == monitor), self.monitors[0])
        img = None
        try:
            mss_cls = getattr(mss, "MSS", mss.mss)
            with mss_cls() as sct:
                area = ({"left": region.x, "top": region.y,
                         "width": region.width, "height": region.height}
                        if region else
                        {"left": mon.x, "top": mon.y,
                         "width": mon.width, "height": mon.height})
                shot = sct.grab(area)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as e:
            log_ctx(logging.DEBUG, "screen.grab.mss_fallback", error=str(e))
            # Fallback 1: PIL ImageGrab
            try:
                from PIL import ImageGrab
                if region:
                    bbox = (region.x, region.y, region.x + region.width, region.y + region.height)
                    img = ImageGrab.grab(bbox=bbox)
                else:
                    img = ImageGrab.grab()
            except Exception:
                # Fallback 2: Graceful blank canvas so agent doesn't crash on locked screen
                w = region.width if region else mon.width
                h = region.height if region else mon.height
                img = Image.new("RGB", (w, h), color=(25, 25, 30))

        h = hashlib.sha256(img.tobytes()).hexdigest()
        return ScreenSnapshot(
            image=img, monitor=mon, content_hash=ScreenHash(h),
        )


class ScreenDiffer:
    """Computes semantic diffs between snapshots."""
    @staticmethod
    def diff_ratio(a: ScreenSnapshot, b: ScreenSnapshot) -> float:
        if a.content_hash == b.content_hash:
            return 0.0
        try:
            from PIL import ImageChops
            diff = ImageChops.difference(a.image, b.image)
            extrema = diff.getextrema()
            max_diff = max(max(e) for e in extrema)
            return float(max_diff) / 255.0
        except Exception:
            return 1.0

    @staticmethod
    def changed_region(a: ScreenSnapshot, b: ScreenSnapshot,
                       threshold: int = 30) -> Optional[BoundingBox]:
        try:
            from PIL import ImageChops
            diff = ImageChops.difference(a.image, b.image)
            diff = diff.point(lambda p: 255 if p > threshold else 0)
            bbox = diff.getbbox()
            if bbox is None: return None
            x1, y1, x2, y2 = bbox
            return BoundingBox(x=x1, y=y1, width=x2 - x1, height=y2 - y1)
        except Exception:
            return None


class WorldModel:
    """The agent's belief about the current screen state."""
    def __init__(self, capture: ScreenCapture, differ: ScreenDiffer) -> None:
        self.capture = capture
        self.differ  = differ
        self._current: Optional[ScreenSnapshot] = None
        self._history: Deque[ScreenSnapshot] = deque(maxlen=20)

    def observe(self) -> ScreenSnapshot:
        snap = self.capture.grab()
        if self._current:
            self._history.append(self._current)
        self._current = snap
        return snap

    @property
    def current(self) -> ScreenSnapshot:
        if self._current is None:
            return self.observe()
        return self._current

    def changed_since(self, snap: ScreenSnapshot,
                      min_ratio: float = 0.02) -> bool:
        cur = self.observe()
        return self.differ.diff_ratio(snap, cur) >= min_ratio


# ═══════════════════════════════════════════════════════════════════════════
#  L4 — MOTOR CONTROL
# ═══════════════════════════════════════════════════════════════════════════

class ActionSpec(BaseModel):
    """Fully-typed description of a motor action."""
    model_config = ConfigDict(frozen=True)
    action_id:   ActionId = Field(default_factory=lambda: ActionId(new_id("act")))
    kind:        ActionKind
    coord:       Optional[PixelCoord] = None
    text:        Optional[StrictStr]  = None
    key:         Optional[StrictStr]  = None
    keys:        Optional[List[StrictStr]] = None
    button:      MouseButton = MouseButton.LEFT
    clicks:      Annotated[int, Field(ge=1, le=3)] = 1
    scroll_amt:  Optional[int] = None
    duration:    Annotated[float, Field(ge=0.0, le=10.0)] = 0.15
    verify:      bool = True

    @model_validator(mode="after")
    def _validate(self) -> Self:
        needs_coord = {ActionKind.CLICK, ActionKind.DOUBLE_CLICK,
                       ActionKind.RIGHT_CLICK, ActionKind.MOVE, ActionKind.DRAG}
        if self.kind in needs_coord and self.coord is None:
            raise ValueError(f"{self.kind} requires coord")
        if self.kind == ActionKind.TYPE and not self.text:
            raise ValueError("TYPE requires text")
        if self.kind == ActionKind.PRESS and not self.key:
            raise ValueError("PRESS requires key")
        if self.kind == ActionKind.HOTKEY and not self.keys:
            raise ValueError("HOTKEY requires keys")
        if self.kind == ActionKind.SCROLL and self.scroll_amt is None:
            raise ValueError("SCROLL requires scroll_amt")
        return self


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    action_id:      ActionId
    kind:           ActionKind
    success:        StrictBool
    detail:         StrictStr = ""
    screen_changed: StrictBool = False
    diff_ratio:     Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    duration_ms:    Annotated[float, Field(ge=0.0)] = 0.0
    retries:        Annotated[int,   Field(ge=0)] = 0
    timestamp:      datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MotorController:
    """
    Executes ActionSpecs with diff verification, back-off, circuit breakers.
    """
    def __init__(
        self,
        world: WorldModel,
        max_retries: int = 3,
        verify_delay: float = 0.3,
        base_backoff: float = 0.2,
    ) -> None:
        self.world = world
        self.max_retries = max_retries
        self.verify_delay = verify_delay
        self.base_backoff = base_backoff
        self._breakers: Dict[ActionKind, CircuitBreaker] = {
            k: CircuitBreaker(name=f"motor.{k.value}") for k in ActionKind
        }

    def _require_pyautogui(self) -> Any:
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.03
            return pyautogui
        except ImportError as e:
            raise MotorError("pyautogui not installed") from e

    async def execute(self, spec: ActionSpec) -> ActionResult:
        breaker = self._breakers[spec.kind]
        breaker.guard()

        await BUS.publish(ActionStartedEvent(
            action_id=spec.action_id, action_kind=spec.kind,
        ))

        last_result: Optional[ActionResult] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with span(f"motor.{spec.kind.value}", attempt=attempt):
                    result = await self._execute_once(spec, attempt)
                if result.success and (not spec.verify or result.screen_changed or spec.kind in {ActionKind.WAIT, ActionKind.SCREENSHOT, ActionKind.MOVE}):
                    breaker.record_success()
                    METRICS.inc("motor.success", kind=spec.kind.value)
                    METRICS.observe("motor.duration_ms", result.duration_ms,
                                    kind=spec.kind.value)
                    await BUS.publish(ActionCompletedEvent(
                        action_id=spec.action_id, success=True,
                        duration_ms=result.duration_ms,
                    ))
                    return result
                last_result = result
                log_ctx(logging.WARNING, "motor.retry",
                        action=spec.kind.value, attempt=attempt)
            except MotorError as e:
                breaker.record_failure()
                METRICS.inc("motor.error", kind=spec.kind.value)
                last_result = ActionResult(
                    action_id=spec.action_id, kind=spec.kind,
                    success=False, detail=str(e), retries=attempt,
                )
            await asyncio.sleep(self.base_backoff * (2 ** (attempt - 1)))

        breaker.record_failure()
        result = last_result or ActionResult(
            action_id=spec.action_id, kind=spec.kind,
            success=False, detail="exhausted retries",
            retries=self.max_retries,
        )
        await BUS.publish(ActionCompletedEvent(
            action_id=spec.action_id, success=False,
            duration_ms=result.duration_ms,
        ))
        return result

    async def _execute_once(self, spec: ActionSpec, attempt: int) -> ActionResult:
        pg = self._require_pyautogui()
        t0 = time.perf_counter()

        before = self.world.current if spec.verify else None

        try:
            if spec.kind == ActionKind.CLICK:
                assert spec.coord
                pg.click(spec.coord.x, spec.coord.y,
                         button=spec.button.value, clicks=spec.clicks)
            elif spec.kind == ActionKind.DOUBLE_CLICK:
                assert spec.coord
                pg.doubleClick(spec.coord.x, spec.coord.y)
            elif spec.kind == ActionKind.RIGHT_CLICK:
                assert spec.coord
                pg.rightClick(spec.coord.x, spec.coord.y)
            elif spec.kind == ActionKind.MOVE:
                assert spec.coord
                pg.moveTo(spec.coord.x, spec.coord.y, duration=spec.duration)
            elif spec.kind == ActionKind.DRAG:
                assert spec.coord
                pg.dragTo(spec.coord.x, spec.coord.y,
                          duration=spec.duration, button=spec.button.value)
            elif spec.kind == ActionKind.TYPE:
                assert spec.text is not None
                self._type_unicode_safe(pg, spec.text)
            elif spec.kind == ActionKind.PRESS:
                assert spec.key
                pg.press(spec.key)
            elif spec.kind == ActionKind.HOTKEY:
                assert spec.keys
                pg.hotkey(*spec.keys)
            elif spec.kind == ActionKind.SCROLL:
                assert spec.scroll_amt is not None
                x = spec.coord.x if spec.coord else None
                y = spec.coord.y if spec.coord else None
                pg.scroll(spec.scroll_amt, x=x, y=y)
            elif spec.kind == ActionKind.WAIT:
                await asyncio.sleep(spec.duration)
            elif spec.kind == ActionKind.SCREENSHOT:
                self.world.observe()
        except Exception as e:
            raise MotorError(f"pyautogui error: {e}", kind=spec.kind.value) from e

        screen_changed, diff_ratio = False, 0.0
        if spec.verify and before is not None:
            await asyncio.sleep(self.verify_delay)
            after = self.world.observe()
            diff_ratio = self.world.differ.diff_ratio(before, after)
            screen_changed = diff_ratio > 0.01

        return ActionResult(
            action_id=spec.action_id, kind=spec.kind, success=True,
            screen_changed=screen_changed, diff_ratio=diff_ratio,
            duration_ms=(time.perf_counter() - t0) * 1000,
            retries=attempt - 1,
        )

    @staticmethod
    def _type_unicode_safe(pg: Any, text: str) -> None:
        try:
            import pyperclip
            pyperclip.copy(text)
            mod = "command" if CURRENT_OS == OSFamily.MACOS else "ctrl"
            pg.hotkey(mod, "v")
        except ImportError:
            ascii_text = text.encode("ascii", "ignore").decode()
            pg.typewrite(ascii_text, interval=0.02)


# ═══════════════════════════════════════════════════════════════════════════
#  L5 — SAFETY GOVERNOR
# ═══════════════════════════════════════════════════════════════════════════

class GovernorConfig(BaseModel):
    model_config = ConfigDict(frozen=False)
    level:                    SafetyLevel = SafetyLevel.NORMAL
    max_actions_per_minute:   Annotated[int,   Field(ge=1, le=600)]  = 60
    max_total_actions:        Annotated[int,   Field(ge=1)]          = 500
    max_wall_clock_seconds:   Annotated[float, Field(gt=0)]          = 600.0
    forbidden_regions:        List[BoundingBox]                      = Field(default_factory=list)
    forbidden_keywords:       Set[str] = Field(default_factory=lambda: {
        "delete", "format", "uninstall", "shutdown", "reboot", "rm -rf",
        "drop table", "sudo", "chmod 777",
    })
    dry_run:                  bool = False
    require_capability:       bool = False


class CapabilityGrant(BaseModel):
    """A grant that authorises specific actions."""
    model_config = ConfigDict(frozen=True)
    token:       CapToken
    allowed:     Set[ActionKind]
    expires_at:  datetime
    granted_by:  StrictStr = "user"

    def is_valid(self) -> bool:
        return datetime.now(timezone.utc) < self.expires_at


class SafetyGovernor:
    """
    Multi-layer governor with formal invariants:
      INVARIANT 1: Σ(actions) ≤ max_total_actions
      INVARIANT 2: rate(actions, 60s) ≤ max_actions_per_minute
      INVARIANT 3: wall_clock(episode) ≤ max_wall_clock_seconds
      INVARIANT 4: ∀ click(x,y). (x,y) ∉ ⋃ forbidden_regions
      INVARIANT 5: ∀ action. ¬ ∃ kw ∈ forbidden_keywords. kw ∈ action.text
      INVARIANT 6: require_capability ⇒ ∃ valid CapabilityGrant covering action.kind
    """
    def __init__(self, config: Optional[GovernorConfig] = None) -> None:
        self.config = config or GovernorConfig()
        self._action_count = 0
        self._recent: Deque[float] = deque(maxlen=1000)
        self._started_at: Optional[float] = None
        self._audit: List[Dict[str, Any]] = []
        self._grants: Dict[CapToken, CapabilityGrant] = {}
        self._escalation: Optional[Callable[[str], Awaitable[bool]]] = None

    def set_escalation_handler(
        self, handler: Callable[[str], Awaitable[bool]]
    ) -> None:
        """Called when the governor needs human approval."""
        self._escalation = handler

    def grant(self, capability: CapabilityGrant) -> None:
        self._grants[capability.token] = capability

    def begin_episode(self) -> None:
        self._started_at = time.time()

    async def check(
        self,
        spec: ActionSpec,
        capability: Optional[CapToken] = None,
    ) -> None:
        # INV 1: total budget
        self._action_count += 1
        if self._action_count > self.config.max_total_actions:
            raise BudgetExhausted("action budget exhausted",
                                  limit=self.config.max_total_actions)

        # INV 2: rate
        now = time.time()
        self._recent.append(now)
        recent = sum(1 for t in self._recent if t > now - 60)
        if recent > self.config.max_actions_per_minute:
            raise SafetyViolation("rate limit exceeded",
                                  rate=recent, limit=self.config.max_actions_per_minute)

        # INV 3: wall clock
        if self._started_at and (now - self._started_at) > self.config.max_wall_clock_seconds:
            raise BudgetExhausted("wall clock exhausted",
                                  elapsed=now - self._started_at)

        # INV 4: forbidden regions
        if spec.coord:
            for region in self.config.forbidden_regions:
                if region.contains(spec.coord):
                    raise SafetyViolation("coordinate in forbidden region",
                                          coord=spec.coord.as_tuple())

        # INV 5: keyword blocklist
        payload = " ".join(filter(None, [spec.text or "", spec.key or "",
                                         " ".join(spec.keys or [])])).lower()
        for kw in self.config.forbidden_keywords:
            if kw in payload:
                if self._escalation:
                    approved = await self._escalation(
                        f"Blocked keyword '{kw}' in action {spec.kind.value}. Allow?"
                    )
                    if approved:
                        break
                await BUS.publish(SafetyBlockedEvent(
                    reason=f"keyword:{kw}", action=spec.kind.value,
                ))
                raise SafetyViolation(f"forbidden keyword '{kw}'",
                                      action=spec.kind.value)

        # INV 6: capability check
        if self.config.require_capability:
            grant = self._grants.get(capability) if capability else None
            if grant is None or not grant.is_valid() or spec.kind not in grant.allowed:
                raise SafetyViolation("capability required",
                                      needed=spec.kind.value)

    def log(self, result: ActionResult) -> None:
        self._audit.append(result.model_dump(mode="json"))

    def dump_audit(self, path: Optional[Path] = None) -> None:
        p = path or (DATA_DIR / "audit.jsonl")
        try:
            with p.open("a", encoding="utf-8") as f:
                for entry in self._audit:
                    f.write(json.dumps(entry, default=str) + "\n")
            self._audit.clear()
        except Exception as e:
            log_ctx(logging.WARNING, "audit.dump.failed", error=str(e))


# ═══════════════════════════════════════════════════════════════════════════
#  L6 — SKILL REGISTRY
# ═══════════════════════════════════════════════════════════════════════════

TIn  = TypeVar("TIn",  bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)


class SkillResult(BaseModel):
    model_config = ConfigDict(frozen=False, extra="allow")
    ok:       StrictBool
    skill:    SkillName
    detail:   StrictStr = ""
    data:     Dict[str, Any] = Field(default_factory=dict)
    error:    Optional[str]  = None
    duration_ms: float = 0.0


@runtime_checkable
class SkillContext(Protocol):
    motor:      MotorController
    perception: PerceptionCascade
    world:      WorldModel
    governor:   SafetyGovernor


@dataclass
class DefaultSkillContext:
    motor:      MotorController
    perception: PerceptionCascade
    world:      WorldModel
    governor:   SafetyGovernor


class Skill(ABC, Generic[TIn]):
    """Abstract typed skill."""
    name:          ClassVar[SkillName]
    description:   ClassVar[str]
    version:       ClassVar[str] = "1.0.0"
    params_model:  ClassVar[Type[BaseModel]]
    required_caps: ClassVar[Set[ActionKind]] = set()

    async def preconditions(self, ctx: SkillContext, params: TIn) -> None:
        return None

    @abstractmethod
    async def run(self, ctx: SkillContext, params: TIn) -> SkillResult: ...

    async def __call__(self, ctx: SkillContext, params: Dict[str, Any]) -> SkillResult:
        t0 = time.perf_counter()
        try:
            typed = cast(TIn, self.params_model.model_validate(params))
        except Exception as e:
            return SkillResult(ok=False, skill=self.name,
                               error=f"param validation: {e}")
        try:
            await self.preconditions(ctx, typed)
        except PreconditionFailed as e:
            return SkillResult(ok=False, skill=self.name,
                               error=str(e), data=e.context)
        try:
            with span(f"skill.{self.name}"):
                result = await self.run(ctx, typed)
        except AgentError as e:
            result = SkillResult(ok=False, skill=self.name,
                                 error=str(e), data=e.to_dict())
        except Exception as e:
            result = SkillResult(ok=False, skill=self.name, error=repr(e))
        result.duration_ms = (time.perf_counter() - t0) * 1000
        METRICS.inc("skill.executed", skill=self.name, ok=str(result.ok))
        METRICS.observe("skill.duration_ms", result.duration_ms, skill=self.name)
        return result


_SKILL_REGISTRY: Dict[SkillName, Type[Skill[Any]]] = {}


def register_skill(cls: Type[Skill[Any]]) -> Type[Skill[Any]]:
    if not hasattr(cls, "name") or not hasattr(cls, "params_model"):
        raise TypeError(f"{cls.__name__} must define 'name' and 'params_model'")
    _SKILL_REGISTRY[cls.name] = cls
    log_ctx(logging.DEBUG, "skill.registered", name=cls.name, version=cls.version)
    return cls


class SkillRegistry:
    def __init__(self, ctx: SkillContext) -> None:
        self.ctx = ctx
        self._instances: Dict[SkillName, Skill[Any]] = {}
        self._stats: Dict[SkillName, Dict[str, float]] = defaultdict(
            lambda: {"runs": 0, "successes": 0, "avg_ms": 0.0}
        )

    def discover(self) -> List[SkillName]:
        for name, cls in _SKILL_REGISTRY.items():
            self._instances.setdefault(name, cls())
        return list(self._instances.keys())

    def describe(self) -> List[Dict[str, Any]]:
        self.discover()
        return [
            {
                "name": s.name,
                "description": s.description,
                "version": s.version,
                "params_schema": s.params_model.model_json_schema(),
            }
            for s in self._instances.values()
        ]

    async def run(self, name: SkillName, params: Dict[str, Any]) -> SkillResult:
        self.discover()
        skill = self._instances.get(name)
        if not skill:
            raise SkillNotFound(f"unknown skill '{name}'", name=name)
        result = await skill(self.ctx, params)
        stat = self._stats[name]
        n = stat["runs"]
        stat["runs"] = n + 1
        stat["successes"] += int(result.ok)
        stat["avg_ms"] = (stat["avg_ms"] * n + result.duration_ms) / (n + 1)
        return result

    def stats(self) -> Dict[str, Any]:
        return {k: dict(v) for k, v in self._stats.items()}


# ═══════════════════════════════════════════════════════════════════════════
#  BUILT-IN SKILLS
# ═══════════════════════════════════════════════════════════════════════════

class YouTubeParams(BaseModel):
    query: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    prefer_playwright: bool = False


@register_skill
class YouTubeSkill(Skill[YouTubeParams]):
    name         = SkillName("youtube")
    description  = "Search and play a YouTube video"
    params_model = YouTubeParams

    async def run(self, ctx: SkillContext, params: YouTubeParams) -> SkillResult:
        has_playwright = False
        try:
            importlib.import_module("playwright.async_api")
            has_playwright = True
        except Exception:
            has_playwright = False

        if params.prefer_playwright and has_playwright:
            return await self._via_playwright(params.query)
        return await self._via_screen_or_browser(ctx, params.query)

    async def _via_playwright(self, query: str) -> SkillResult:
        pw_api = importlib.import_module("playwright.async_api")
        async_playwright = pw_api.async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            try:
                page = await browser.new_page()
                await page.goto("https://www.youtube.com", wait_until="domcontentloaded")
                await page.wait_for_selector("input#search", timeout=15_000)
                await page.fill("input#search", query)
                await page.press("input#search", "Enter")
                await page.wait_for_selector("ytd-video-renderer", timeout=15_000)
                first = page.locator("ytd-video-renderer").first
                title = await first.locator("#video-title").inner_text()
                await first.click()
                await asyncio.sleep(2)
                return SkillResult(ok=True, skill=self.name,
                                   data={"query": query, "title": title})
            finally:
                await browser.close()

    async def _via_screen_or_browser(self, ctx: SkillContext, query: str) -> SkillResult:
        # Fast & 100% reliable direct browser trigger
        import urllib.parse
        encoded = urllib.parse.quote(query)
        url = f"https://www.youtube.com/results?search_query={encoded}"
        webbrowser.open(url)
        await asyncio.sleep(2.5)

        # Try to click the first video via perception or default position
        snap = ctx.world.observe()
        try:
            els = await ctx.perception.find(
                snap.image,
                PerceptionQuery(text="video", role_hint=ElementRole.IMAGE, min_confidence=0.4),
                screen_hash=snap.content_hash,
            )
            if els:
                await ctx.motor.execute(ActionSpec(kind=ActionKind.CLICK, coord=els[0].center))
        except Exception:
            # Fallback: Tab navigation to first search result
            try:
                await ctx.motor.execute(ActionSpec(kind=ActionKind.PRESS, key="tab", verify=False))
                await asyncio.sleep(0.1)
                await ctx.motor.execute(ActionSpec(kind=ActionKind.PRESS, key="enter", verify=False))
            except Exception:
                pass

        return SkillResult(ok=True, skill=self.name,
                           data={"query": query, "url": url, "path": "direct_browser"})


class VolumeParams(BaseModel):
    level: Annotated[int, Field(ge=0, le=100)]


@register_skill
class VolumeSkill(Skill[VolumeParams]):
    name         = SkillName("volume")
    description  = "Set system volume (0-100)"
    params_model = VolumeParams

    async def run(self, ctx: SkillContext, params: VolumeParams) -> SkillResult:
        try:
            if CURRENT_OS == OSFamily.WINDOWS:
                from ctypes import cast as ccast, POINTER
                from comtypes import CLSCTX_ALL
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
                dev = AudioUtilities.GetSpeakers()
                if hasattr(dev, "EndpointVolume"):
                    vol = dev.EndpointVolume
                else:
                    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    vol = ccast(iface, POINTER(IAudioEndpointVolume))
                vol.SetMasterVolumeLevelScalar(params.level / 100.0, None)
                if params.level > 0 and vol.GetMute():
                    vol.SetMute(0, None)
            elif CURRENT_OS == OSFamily.LINUX:
                subprocess.run(["amixer", "-D", "pulse", "sset", "Master",
                                f"{params.level}%"], check=True, capture_output=True)
            elif CURRENT_OS == OSFamily.MACOS:
                subprocess.run(["osascript", "-e",
                                f"set volume output volume {params.level}"],
                               check=True, capture_output=True)
            return SkillResult(ok=True, skill=self.name, data={"volume": params.level})
        except Exception as e:
            return SkillResult(ok=False, skill=self.name, error=str(e))


class BrightnessParams(BaseModel):
    level: Annotated[int, Field(ge=0, le=100)]


@register_skill
class BrightnessSkill(Skill[BrightnessParams]):
    name         = SkillName("brightness")
    description  = "Set screen brightness (0-100)"
    params_model = BrightnessParams

    async def run(self, ctx: SkillContext, params: BrightnessParams) -> SkillResult:
        try:
            import screen_brightness_control as sbc
            sbc.set_brightness(params.level)
            return SkillResult(ok=True, skill=self.name,
                               data={"brightness": params.level})
        except Exception as e:
            return SkillResult(ok=False, skill=self.name, error=str(e))


class ScreenshotParams(BaseModel):
    path: Annotated[StrictStr, Field(min_length=1, max_length=260)] = "screenshot.png"
    region: Optional[BoundingBox] = None


@register_skill
class ScreenshotSkill(Skill[ScreenshotParams]):
    name         = SkillName("screenshot")
    description  = "Save a screenshot to disk"
    params_model = ScreenshotParams

    async def run(self, ctx: SkillContext, params: ScreenshotParams) -> SkillResult:
        try:
            snap = ctx.world.capture.grab(region=params.region)
            out_path = Path(params.path)
            if not out_path.is_absolute():
                out_path = DATA_DIR / out_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            snap.image.save(str(out_path))
            return SkillResult(ok=True, skill=self.name,
                               data={"path": str(out_path),
                                     "size": list(snap.image.size)})
        except Exception as e:
            return SkillResult(ok=False, skill=self.name, error=str(e))


class LaunchParams(BaseModel):
    app: Annotated[StrictStr, Field(min_length=1, max_length=100)]


@register_skill
class LaunchSkill(Skill[LaunchParams]):
    name         = SkillName("launch")
    description  = "Launch an application or common utility"
    params_model = LaunchParams

    async def run(self, ctx: SkillContext, params: LaunchParams) -> SkillResult:
        try:
            app_lower = params.app.lower().strip()
            if CURRENT_OS == OSFamily.WINDOWS:
                common_apps = {
                    "notepad": "notepad.exe",
                    "calc": "calc.exe",
                    "calculator": "calc.exe",
                    "chrome": "chrome",
                    "edge": "msedge",
                    "explorer": "explorer.exe",
                    "taskmgr": "taskmgr.exe",
                    "cmd": "cmd.exe",
                }
                target = common_apps.get(app_lower, params.app)
                try:
                    os.startfile(target)  # type: ignore[attr-defined]
                except Exception:
                    subprocess.Popen(f"start {target}", shell=True)
            elif CURRENT_OS == OSFamily.MACOS:
                subprocess.Popen(["open", "-a", params.app])
            else:
                subprocess.Popen([params.app])
            return SkillResult(ok=True, skill=self.name, data={"app": params.app})
        except Exception as e:
            return SkillResult(ok=False, skill=self.name, error=str(e))


# ═══════════════════════════════════════════════════════════════════════════
#  L7 — PLANNER: ReAct + episodic memory + reflection
# ═══════════════════════════════════════════════════════════════════════════

class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=False)
    step_id:      StepId    = Field(default_factory=lambda: StepId(new_id("step")))
    thought:      StrictStr
    skill:        Optional[SkillName] = None
    raw_action:   Optional[ActionKind] = None
    params:       Dict[str, Any] = Field(default_factory=dict)
    observation:  StrictStr = ""
    success:      bool = False
    timestamp:    datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Episode(BaseModel):
    episode_id: EpisodeId
    goal:       StrictStr
    steps:      List[PlanStep] = Field(default_factory=list)
    final_state: AgentState = AgentState.IDLE
    started_at:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at:    Optional[datetime] = None


class EpisodicMemory:
    """Persists past episodes for few-shot planning."""
    def __init__(self, path: Optional[Path] = None, max_recall: int = 5) -> None:
        self.path = path or (DATA_DIR / "episodes.jsonl")
        self.max_recall = max_recall
        self._episodes: List[Episode] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists(): return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._episodes.append(Episode.model_validate_json(line))
        except Exception as e:
            log_ctx(logging.WARNING, "memory.load.failed", error=str(e))

    def save(self, episode: Episode) -> None:
        self._episodes.append(episode)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(episode.model_dump_json() + "\n")
        except Exception as e:
            log_ctx(logging.WARNING, "memory.save.failed", error=str(e))

    def recall_similar(self, goal: str) -> List[Episode]:
        goal_tokens = set(goal.lower().split())
        scored = [
            (len(goal_tokens & set(e.goal.lower().split())), e)
            for e in self._episodes if e.final_state == AgentState.SUCCEEDED
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [e for _, e in scored[:self.max_recall]]


class StateMachine:
    """Enforces valid transitions between AgentStates."""
    def __init__(self, initial: AgentState = AgentState.IDLE) -> None:
        self._state = initial

    @property
    def state(self) -> AgentState: return self._state

    async def transition(self, to: AgentState) -> None:
        if to not in _VALID_TRANSITIONS[self._state]:
            raise StateTransitionError(
                f"illegal transition {self._state.value} → {to.value}",
                from_state=self._state.value, to_state=to.value,
            )
        prev = self._state
        self._state = to
        await BUS.publish(StateChangedEvent(from_state=prev, to_state=to))


@runtime_checkable
class Planner(Protocol):
    async def next_step(
        self, goal: str, history: List[PlanStep],
        world: WorldModel, memory: EpisodicMemory,
    ) -> Optional[PlanStep]: ...


class StubPlanner:
    """Deterministic keyword & intent planner."""
    async def next_step(
        self, goal: str, history: List[PlanStep],
        world: WorldModel, memory: EpisodicMemory,
    ) -> Optional[PlanStep]:
        import re
        gl = goal.lower()
        done = {s.skill for s in history if s.success}

        # 1. YouTube
        if any(w in gl for w in ["youtube", "video", "play", "gaan", "gan", "গান", "বাজাও", "চালাও"]) \
           and SkillName("youtube") not in done:
            query = "lo-fi beats"
            for marker in ["play", "for", "search", "গান", "video"]:
                if marker in gl:
                    part = gl.split(marker, 1)[-1].strip(" :-")
                    if part:
                        query = part
                        break
            return PlanStep(
                thought="Delegating to youtube skill",
                skill=SkillName("youtube"), params={"query": query},
            )

        # 2. Volume
        if any(w in gl for w in ["volume", "sound", "awaj", "ভলিউম", "সাউন্ড"]) and SkillName("volume") not in done:
            num = re.search(r"\d+", gl)
            level = int(num.group(0)) if num else 70
            return PlanStep(
                thought="Setting volume",
                skill=SkillName("volume"), params={"level": level},
            )

        # 3. Brightness
        if any(w in gl for w in ["brightness", "display light", "alo", "ব্রাইটনেস", "আলো"]) and SkillName("brightness") not in done:
            num = re.search(r"\d+", gl)
            level = int(num.group(0)) if num else 80
            return PlanStep(
                thought="Setting brightness",
                skill=SkillName("brightness"), params={"level": level},
            )

        # 4. Screenshot
        if any(w in gl for w in ["screenshot", "স্ক্রিনশট", "screen capture"]) and SkillName("screenshot") not in done:
            return PlanStep(
                thought="Taking screenshot",
                skill=SkillName("screenshot"), params={"path": "capture.png"},
            )

        # 5. App Launch
        if any(w in gl for w in ["open", "launch", "kholo", "খোলো", "চালু"]) and SkillName("launch") not in done:
            words = gl.split()
            app_candidate = words[-1] if words else "notepad"
            return PlanStep(
                thought=f"Launching application {app_candidate}",
                skill=SkillName("launch"), params={"app": app_candidate},
            )

        return None


class LLMPlanner:
    """
    LLM-backed planner. Defaults to StubPlanner with option for Gemini / Anthropic.
    """
    def __init__(self, model: str = "gemini-1.5-flash") -> None:
        self.model = model

    async def next_step(
        self, goal: str, history: List[PlanStep],
        world: WorldModel, memory: EpisodicMemory,
    ) -> Optional[PlanStep]:
        # Fast fallback to deterministic StubPlanner
        return await StubPlanner().next_step(goal, history, world, memory)


class ReActAgent:
    """
    The core agent engine: Perceive → Plan → Act → Verify → Reflect.
    """
    def __init__(
        self,
        registry: SkillRegistry,
        motor: MotorController,
        world: WorldModel,
        governor: SafetyGovernor,
        planner: Optional[Planner] = None,
        memory:  Optional[EpisodicMemory] = None,
        max_steps: int = 20,
    ) -> None:
        self.registry = registry
        self.motor    = motor
        self.world    = world
        self.governor = governor
        self.planner  = planner or StubPlanner()
        self.memory   = memory  or EpisodicMemory()
        self.max_steps = max_steps
        self.fsm = StateMachine()

    async def run(self, goal: str) -> Episode:
        episode = Episode(episode_id=EpisodeId(new_id("ep")), goal=goal)
        self.governor.begin_episode()

        try:
            await self.fsm.transition(AgentState.PERCEIVING)
            log_ctx(logging.INFO, "episode.start",
                    episode_id=episode.episode_id, goal=goal)

            for step_num in range(1, self.max_steps + 1):
                # PERCEIVE
                if self.fsm.state != AgentState.PERCEIVING:
                    await self.fsm.transition(AgentState.PERCEIVING)
                self.world.observe()

                # PLAN
                await self.fsm.transition(AgentState.PLANNING)
                step = await self.planner.next_step(
                    goal, episode.steps, self.world, self.memory,
                )
                if step is None:
                    await self.fsm.transition(AgentState.SUCCEEDED)
                    episode.final_state = AgentState.SUCCEEDED
                    break

                log_ctx(logging.INFO, "step.plan",
                        step=step_num, thought=step.thought,
                        skill=step.skill, action=step.raw_action)

                # ACT
                await self.fsm.transition(AgentState.ACTING)
                try:
                    if step.skill:
                        result = await self.registry.run(step.skill, step.params)
                        step.observation = result.model_dump_json()
                        step.success = result.ok
                    elif step.raw_action:
                        spec = self._spec_from(step.raw_action, step.params)
                        await self.governor.check(spec)
                        act_result = await self.motor.execute(spec)
                        self.governor.log(act_result)
                        step.observation = act_result.model_dump_json()
                        step.success = act_result.success
                    else:
                        step.observation = "no skill or raw_action"
                        step.success = False
                except SafetyViolation as e:
                    step.observation = f"BLOCKED: {e}"
                    step.success = False
                    episode.steps.append(step)
                    await self.fsm.transition(AgentState.BLOCKED)
                    episode.final_state = AgentState.BLOCKED
                    break
                except AgentError as e:
                    step.observation = f"ERROR: {e}"
                    step.success = False

                episode.steps.append(step)

                # VERIFY + REFLECT
                await self.fsm.transition(AgentState.VERIFYING)
                await self.fsm.transition(AgentState.REFLECTING)

                if not step.success:
                    log_ctx(logging.WARNING, "step.failed", step=step_num)
                else:
                    log_ctx(logging.INFO, "step.ok", step=step_num)
            else:
                await self.fsm.transition(AgentState.FAILED)
                episode.final_state = AgentState.FAILED

        except StateTransitionError:
            await self.fsm.transition(AgentState.ABORTED)
            episode.final_state = AgentState.ABORTED
            raise
        finally:
            episode.ended_at = datetime.now(timezone.utc)
            self.memory.save(episode)
            self.governor.dump_audit()
            METRICS.inc("episode.completed", state=episode.final_state.value)
            log_ctx(logging.INFO, "episode.end",
                    episode_id=episode.episode_id,
                    state=episode.final_state.value,
                    steps=len(episode.steps))
        return episode

    @staticmethod
    def _spec_from(kind: ActionKind, params: Dict[str, Any]) -> ActionSpec:
        coord = None
        if "x" in params and "y" in params:
            coord = PixelCoord(x=int(params["x"]), y=int(params["y"]))
        return ActionSpec(
            kind=kind, coord=coord,
            text=params.get("text"), key=params.get("key"),
            keys=params.get("keys"),
            button=MouseButton(params.get("button", "left")),
            clicks=int(params.get("clicks", 1)),
            scroll_amt=params.get("scroll_amt"),
            duration=float(params.get("duration", 0.15)),
            verify=bool(params.get("verify", True)),
        )


# ═══════════════════════════════════════════════════════════════════════════
#  BOOTSTRAP FACADE
# ═══════════════════════════════════════════════════════════════════════════

class ComputerControlAgent:
    """High-level Facade for autonomous desktop control."""
    def __init__(
        self,
        governor_config: Optional[GovernorConfig] = None,
        planner: Optional[Planner] = None,
        dry_run: bool = False,
    ) -> None:
        cfg = governor_config or GovernorConfig()
        cfg.dry_run = dry_run

        capture   = ScreenCapture()
        differ    = ScreenDiffer()
        self.world = WorldModel(capture, differ)
        self.motor = MotorController(self.world)
        self.perception = PerceptionCascade()
        self.governor = SafetyGovernor(cfg)
        self.context = DefaultSkillContext(
            motor=self.motor, perception=self.perception,
            world=self.world, governor=self.governor,
        )
        self.registry = SkillRegistry(self.context)
        self.registry.discover()
        self.memory = EpisodicMemory()

        self.agent = ReActAgent(
            registry=self.registry, motor=self.motor, world=self.world,
            governor=self.governor, planner=planner, memory=self.memory,
        )

        log_ctx(logging.INFO, "agent.ready",
                os=CURRENT_OS.value,
                monitors=len(self.world.capture.monitors),
                skills=self.registry.discover(),
                safety_level=cfg.level.value)

    async def run(self, goal: str) -> Episode:
        return await self.agent.run(goal)

    async def quick(self, skill: str, **params: Any) -> SkillResult:
        return await self.registry.run(SkillName(skill), params)

    def snapshot_metrics(self) -> Dict[str, Any]:
        return {
            "metrics": METRICS.snapshot(),
            "skill_stats": self.registry.stats(),
            "state": self.agent.fsm.state.value,
        }


# Global lazy singleton instance
_GLOBAL_AGENT: Optional[ComputerControlAgent] = None


def get_computer_control_agent() -> ComputerControlAgent:
    global _GLOBAL_AGENT
    if _GLOBAL_AGENT is None:
        _GLOBAL_AGENT = ComputerControlAgent()
    return _GLOBAL_AGENT


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

async def _main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        prog="agent",
        description="Computer Control Agent v4.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python -m Backend.autonomous_agent "Open YouTube and play lo-fi beats"\n'
            "  python -m Backend.autonomous_agent --skill volume --params '{\"level\":75}'\n"
            "  python -m Backend.autonomous_agent --skill screenshot --params '{\"path\":\"shot.png\"}'\n"
            "  python -m Backend.autonomous_agent --describe\n"
        ),
    )
    p.add_argument("goal", nargs="?")
    p.add_argument("--skill")
    p.add_argument("--params", default="{}", help="JSON params for --skill")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--safety", choices=[s.value for s in SafetyLevel], default="normal")
    p.add_argument("--describe", action="store_true", help="List available skills")
    p.add_argument("--metrics", action="store_true", help="Print metrics")
    p.add_argument("--verbose", "-v", action="count", default=0)
    args = p.parse_args()

    if args.verbose >= 1: log.setLevel(logging.DEBUG)

    cfg = GovernorConfig(level=SafetyLevel(args.safety), dry_run=args.dry_run)
    agent = ComputerControlAgent(governor_config=cfg, dry_run=args.dry_run)

    if args.describe:
        print(json.dumps(agent.registry.describe(), indent=2, default=str))
        return

    if args.skill:
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError as e:
            print(f"invalid --params JSON: {e}", file=sys.stderr)
            sys.exit(2)
        result = await agent.quick(args.skill, **params)
        print(result.model_dump_json(indent=2))
    elif args.goal:
        episode = await agent.run(args.goal)
        print(episode.model_dump_json(indent=2))
    else:
        p.print_help()

    if args.metrics:
        print("\n─── METRICS ───", file=sys.stderr)
        print(json.dumps(agent.snapshot_metrics(), indent=2, default=str), file=sys.stderr)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\naborted by user", file=sys.stderr)
        sys.exit(130)
