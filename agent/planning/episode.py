from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictStr, computed_field

from agent.core.enums import AgentState
from agent.core.ids import EpisodeId
from agent.planning.step import PlanStep


class Episode(BaseModel):
    model_config = ConfigDict(extra="ignore")
    episode_id: EpisodeId
    goal: StrictStr
    steps: list[PlanStep] = Field(default_factory=list)
    final_state: AgentState = AgentState.IDLE
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def step_count(self) -> int:
        return len(self.steps)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def succeeded(self) -> bool:
        return self.final_state is AgentState.SUCCEEDED
