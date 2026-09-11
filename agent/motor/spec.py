"""Discriminated ``ActionSpec`` union — each kind has exactly the fields it needs.

A single fat model with optional ``coord`` / ``text`` / ``key`` lets invalid
combinations type-check. Here every variant is its own frozen model, tagged
with a ``Literal`` ``kind``, and ``TypeAdapter`` reconstructs the union from
JSON. Exhaustive ``match`` on the union is a type error if a variant is missed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from typing_extensions import Self

from agent.core.enums import ActionKind, MouseButton, ScrollDirection
from agent.core.ids import ActionId, new_action_id
from agent.geometry.coord import PixelCoord

type Duration = Annotated[float, Field(ge=0.0, le=60.0)]
type ClickCount = Annotated[int, Field(ge=1, le=3)]
type PayloadText = Annotated[str, Field(min_length=1, max_length=10_000)]
type KeyName = Annotated[str, Field(min_length=1, max_length=40)]


class ActionBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action_id: ActionId = Field(default_factory=new_action_id)
    duration: Duration = 0.15
    verify: bool = True
    timeout: Annotated[float, Field(gt=0.0, le=120.0)] = 30.0


class ClickAction(ActionBase):
    kind: Literal[ActionKind.CLICK] = ActionKind.CLICK
    coord: PixelCoord
    button: MouseButton = MouseButton.LEFT
    clicks: ClickCount = 1


class DoubleClickAction(ActionBase):
    kind: Literal[ActionKind.DOUBLE_CLICK] = ActionKind.DOUBLE_CLICK
    coord: PixelCoord


class RightClickAction(ActionBase):
    kind: Literal[ActionKind.RIGHT_CLICK] = ActionKind.RIGHT_CLICK
    coord: PixelCoord


class MoveAction(ActionBase):
    kind: Literal[ActionKind.MOVE] = ActionKind.MOVE
    coord: PixelCoord
    verify: bool = False


class DragAction(ActionBase):
    kind: Literal[ActionKind.DRAG] = ActionKind.DRAG
    coord: PixelCoord
    button: MouseButton = MouseButton.LEFT


class TypeAction(ActionBase):
    kind: Literal[ActionKind.TYPE] = ActionKind.TYPE
    text: PayloadText


class PressAction(ActionBase):
    kind: Literal[ActionKind.PRESS] = ActionKind.PRESS
    key: KeyName


class HotkeyAction(ActionBase):
    kind: Literal[ActionKind.HOTKEY] = ActionKind.HOTKEY
    keys: Annotated[list[KeyName], Field(min_length=1, max_length=6)]


class ScrollAction(ActionBase):
    kind: Literal[ActionKind.SCROLL] = ActionKind.SCROLL
    amount: int
    direction: ScrollDirection = ScrollDirection.DOWN
    coord: PixelCoord | None = None


class WaitAction(ActionBase):
    kind: Literal[ActionKind.WAIT] = ActionKind.WAIT
    duration: Duration = 0.5
    verify: bool = False


class ScreenshotAction(ActionBase):
    kind: Literal[ActionKind.SCREENSHOT] = ActionKind.SCREENSHOT
    verify: bool = False


ActionSpec = Annotated[
    ClickAction
    | DoubleClickAction
    | RightClickAction
    | MoveAction
    | DragAction
    | TypeAction
    | PressAction
    | HotkeyAction
    | ScrollAction
    | WaitAction
    | ScreenshotAction,
    Field(discriminator="kind"),
]

ACTION_ADAPTER: TypeAdapter[ActionSpec] = TypeAdapter(ActionSpec)


def parse_action(data: object) -> ActionSpec:
    return ACTION_ADAPTER.validate_python(data)


def action_coord(spec: ActionSpec) -> PixelCoord | None:
    if isinstance(
        spec,
        (ClickAction, DoubleClickAction, RightClickAction, MoveAction, DragAction),
    ):
        return spec.coord
    if isinstance(spec, ScrollAction):
        return spec.coord
    return None


def action_payload_text(spec: ActionSpec) -> str:
    if isinstance(spec, TypeAction):
        return spec.text
    if isinstance(spec, PressAction):
        return spec.key
    if isinstance(spec, HotkeyAction):
        return " ".join(spec.keys)
    return ""


# Ergonomic constructors — preferred over spelling out ``kind=`` at call sites.


def click(x: int, y: int, *, button: MouseButton = MouseButton.LEFT, clicks: int = 1) -> ClickAction:
    return ClickAction(coord=PixelCoord(x=x, y=y), button=button, clicks=clicks)


def double_click(x: int, y: int) -> DoubleClickAction:
    return DoubleClickAction(coord=PixelCoord(x=x, y=y))


def right_click(x: int, y: int) -> RightClickAction:
    return RightClickAction(coord=PixelCoord(x=x, y=y))


def move_to(x: int, y: int, *, duration: float = 0.15) -> MoveAction:
    return MoveAction(coord=PixelCoord(x=x, y=y), duration=duration)


def drag_to(x: int, y: int, *, duration: float = 0.3) -> DragAction:
    return DragAction(coord=PixelCoord(x=x, y=y), duration=duration)


def type_text(text: str) -> TypeAction:
    return TypeAction(text=text)


def press(key: str) -> PressAction:
    return PressAction(key=key)


def hotkey(*keys: str) -> HotkeyAction:
    return HotkeyAction(keys=list(keys))


def scroll(
    amount: int,
    *,
    direction: ScrollDirection = ScrollDirection.DOWN,
    x: int | None = None,
    y: int | None = None,
) -> ScrollAction:
    coord = PixelCoord(x=x, y=y) if x is not None and y is not None else None
    return ScrollAction(amount=amount, direction=direction, coord=coord)


def wait(seconds: float) -> WaitAction:
    return WaitAction(duration=seconds)


def screenshot() -> ScreenshotAction:
    return ScreenshotAction()


def spec_from_kind(kind: ActionKind, params: dict[str, object]) -> ActionSpec:
    """Build a spec from a loosely-typed planner dict (JSON / LLM output)."""
    payload: dict[str, object] = {"kind": kind.value, **params}
    if "x" in params and "y" in params and "coord" not in params:
        payload["coord"] = {"x": int(params["x"]), "y": int(params["y"])}  # type: ignore[arg-type]
        payload.pop("x", None)
        payload.pop("y", None)
    if kind is ActionKind.SCROLL and "scroll_amt" in params and "amount" not in params:
        payload["amount"] = params["scroll_amt"]
        payload.pop("scroll_amt", None)
    return parse_action(payload)


# Satisfy "used" for Self in case a future validator needs it.
_ = Self
