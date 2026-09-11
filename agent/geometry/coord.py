"""Coordinate value objects — physical vs logical distinction.

Multi-monitor layouts routinely place secondary displays at negative origins,
so pixel coordinates are signed int16-range rather than non-negative.
``PixelCoord`` and ``LogicalCoord`` are distinct Pydantic models so a
DPI-scaled point cannot be passed where a physical pixel is required.
"""

from __future__ import annotations

import math
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Annotated

type CoordInt = Annotated[int, Field(ge=-32768, le=65535)]


class PixelCoord(BaseModel):
    """Physical pixel coordinate on the virtual desktop."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    x: CoordInt
    y: CoordInt

    def as_tuple(self) -> tuple[int, int]:
        return (self.x, self.y)

    def distance_to(self, other: PixelCoord) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def offset(self, dx: int, dy: int) -> PixelCoord:
        return PixelCoord(x=self.x + dx, y=self.y + dy)

    def clamp(self, x_min: int, y_min: int, x_max: int, y_max: int) -> PixelCoord:
        return PixelCoord(
            x=min(max(self.x, x_min), x_max),
            y=min(max(self.y, y_min), y_max),
        )


class LogicalCoord(BaseModel):
    """Logical (DPI-scaled) coordinate. Distinct type prevents mix-up."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    x: CoordInt
    y: CoordInt

    def as_tuple(self) -> tuple[int, int]:
        return (self.x, self.y)


class IntPoint(NamedTuple):
    """Tiny unvalidated tuple used at FFI boundaries (mss, pyautogui)."""

    x: int
    y: int
