from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent.core.enums import ActionKind
from agent.core.ids import SkillName, StepId, new_step_id


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: StepId = Field(default_factory=new_step_id)
    thought: StrictStr
    skill: SkillName | None = None
    raw_action: ActionKind | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    observation: StrictStr = ""
    success: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
