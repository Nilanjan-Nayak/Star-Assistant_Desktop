"""In-memory capture backend for tests — never touches the real screen."""

from __future__ import annotations

from agent.geometry.bbox import BoundingBox
from agent.geometry.monitor import MonitorInfo
from agent.geometry.raster import Raster, solid
from agent.world.snapshot import ScreenSnapshot


class FakeCapture:
    def __init__(
        self,
        raster: Raster | None = None,
        monitor: MonitorInfo | None = None,
    ) -> None:
        self.raster = raster or solid(64, 48, (32, 32, 32))
        self.monitor = monitor or MonitorInfo(
            index=1, x=0, y=0, width=64, height=48, is_primary=True, name="fake"
        )
        self.monitors: list[MonitorInfo] = [self.monitor]
        self.grabs: int = 0

    def grab(self, region: BoundingBox | None = None, monitor: int = 1) -> ScreenSnapshot:
        _ = monitor
        self.grabs += 1
        raster = self.raster.crop(region) if region is not None else self.raster
        return ScreenSnapshot(raster=raster, monitor=self.monitor, content_hash=raster.sha256())

    def set_raster(self, raster: Raster) -> None:
        self.raster = raster
