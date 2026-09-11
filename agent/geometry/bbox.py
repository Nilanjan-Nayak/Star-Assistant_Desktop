"""Axis-aligned bounding box. Half-open on the far edges: ``[x, x+width)``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Annotated, Self

from agent.geometry.coord import PixelCoord


class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    x: Annotated[int, Field(ge=-32768, le=65535)]
    y: Annotated[int, Field(ge=-32768, le=65535)]
    width: Annotated[int, Field(ge=1, le=65535)]
    height: Annotated[int, Field(ge=1, le=65535)]

    @model_validator(mode="after")
    def _finite_extent(self) -> Self:
        # Guard against overflow when adding origin + size.
        if self.x + self.width > 131071 or self.y + self.height > 131071:
            raise ValueError("bounding box extent overflows the virtual desktop")
        return self

    @property
    def center(self) -> PixelCoord:
        return PixelCoord(x=self.x + self.width // 2, y=self.y + self.height // 2)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, point: PixelCoord) -> bool:
        """Half-open containment: the right and bottom edges are exclusive."""
        return self.x <= point.x < self.right and self.y <= point.y < self.bottom

    def intersects(self, other: BoundingBox) -> bool:
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )

    def intersection(self, other: BoundingBox) -> BoundingBox | None:
        if not self.intersects(other):
            return None
        x1 = max(self.x, other.x)
        y1 = max(self.y, other.y)
        x2 = min(self.right, other.right)
        y2 = min(self.bottom, other.bottom)
        return BoundingBox(x=x1, y=y1, width=x2 - x1, height=y2 - y1)

    def iou(self, other: BoundingBox) -> float:
        """Intersection-over-Union score for element deduplication."""
        overlap = self.intersection(other)
        if overlap is None:
            return 0.0
        union = self.area + other.area - overlap.area
        return overlap.area / union if union > 0 else 0.0

    def translate(self, dx: int, dy: int) -> BoundingBox:
        return BoundingBox(x=self.x + dx, y=self.y + dy, width=self.width, height=self.height)

    def as_tuple(self) -> tuple[int, int, int, int]:
        """``(x, y, right, bottom)`` crop box, PIL-style."""
        return (self.x, self.y, self.right, self.bottom)
