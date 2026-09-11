"""SkillResult — frozen outcome of a skill invocation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from typing_extensions import Annotated

from agent.core.ids import SkillName


class SkillResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: StrictBool
    skill: SkillName
    detail: StrictStr = ""
    data: dict[str, object] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: Annotated[float, Field(ge=0.0)] = 0.0

    def with_duration(self, duration_ms: float) -> SkillResult:
        return self.model_copy(update={"duration_ms": duration_ms})
