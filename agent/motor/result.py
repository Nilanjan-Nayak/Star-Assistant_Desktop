"""``ActionResult`` — immutable outcome of a single motor action."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from typing_extensions import Annotated

from agent.core.enums import ActionKind
from agent.core.ids import ActionId


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action_id: ActionId
    kind: ActionKind
    success: StrictBool
    detail: StrictStr = ""
    screen_changed: StrictBool = False
    diff_ratio: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    duration_ms: Annotated[float, Field(ge=0.0)] = 0.0
    retries: Annotated[int, Field(ge=0)] = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
