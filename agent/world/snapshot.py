"""Content-addressed screen snapshot. The raster is the source of truth."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import ScreenHash, SnapshotId, new_snapshot_id
from agent.geometry.monitor import MonitorInfo
from agent.geometry.raster import Raster


class ScreenSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True, extra="forbid")
    snapshot_id: SnapshotId = Field(default_factory=new_snapshot_id)
    taken_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raster: Raster
    monitor: MonitorInfo
    content_hash: ScreenHash

    @property
    def width(self) -> int:
        return self.raster.width

    @property
    def height(self) -> int:
        return self.raster.height
