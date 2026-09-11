"""Frozen memory record — one row in the local store."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from typing_extensions import Annotated

from agent.core.enums import MemoryKind
from agent.core.ids import EpisodeId, MemoryId
from agent.memory.embed import Embedding


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    memory_id: MemoryId
    text: Annotated[StrictStr, Field(min_length=1, max_length=4000)]
    kind: MemoryKind
    timestamp: datetime
    key: StrictStr | None = None
    value: StrictStr | None = None
    source_episode: EpisodeId | None = None
    score: Annotated[float, Field(ge=-1.0, le=1.0)] | None = None
    embedding: Embedding | None = None
