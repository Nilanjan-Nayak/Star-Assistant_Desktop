"""Immutable ``ScreenElement`` value object."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from typing_extensions import Annotated

from agent.core.enums import ElementRole, PerceptionSource
from agent.core.ids import ElementId, new_element_id
from agent.geometry.bbox import BoundingBox
from agent.geometry.coord import PixelCoord


class ScreenElement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    element_id: ElementId = Field(default_factory=new_element_id)
    text: StrictStr
    bbox: BoundingBox
    role: ElementRole = ElementRole.UNKNOWN
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    source: PerceptionSource
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > 2000:
            return stripped[:2000]
        return stripped

    @property
    def center(self) -> PixelCoord:
        return self.bbox.center

    def distance_to(self, point: PixelCoord) -> float:
        return self.center.distance_to(point)

    def is_duplicate_of(self, other: ScreenElement, *, iou_threshold: float = 0.7) -> bool:
        return (
            self.role == other.role
            and self.text.lower() == other.text.lower()
            and self.bbox.iou(other.bbox) >= iou_threshold
        )
