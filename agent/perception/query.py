"""Structured perception query."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from typing_extensions import Annotated

from agent.core.enums import ElementRole
from agent.geometry.bbox import BoundingBox


class PerceptionQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: Annotated[StrictStr, Field(min_length=1, max_length=500)]
    role_hint: ElementRole | None = None
    region: BoundingBox | None = None
    min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    max_results: Annotated[int, Field(ge=1, le=100)] = 10
    fuzzy: bool = True
    dedupe: bool = True
