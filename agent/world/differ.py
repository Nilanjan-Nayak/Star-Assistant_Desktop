"""Screen diffing over ``Raster`` buffers — no PIL required."""

from __future__ import annotations

from agent.geometry.bbox import BoundingBox
from agent.world.snapshot import ScreenSnapshot


class ScreenDiffer:
    @staticmethod
    def diff_ratio(a: ScreenSnapshot, b: ScreenSnapshot) -> float:
        if a.content_hash == b.content_hash:
            return 0.0
        return a.raster.diff_ratio(b.raster)

    @staticmethod
    def changed_region(
        a: ScreenSnapshot,
        b: ScreenSnapshot,
        threshold: int = 30,
    ) -> BoundingBox | None:
        return a.raster.changed_bbox(b.raster, threshold=threshold)
