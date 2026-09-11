"""L1 spatial types: coordinates, boxes, rasters, monitors."""

from __future__ import annotations

from agent.geometry.bbox import BoundingBox
from agent.geometry.coord import IntPoint, LogicalCoord, PixelCoord
from agent.geometry.monitor import (
    CURRENT_OS,
    MonitorInfo,
    detect_os,
    enumerate_monitors,
    monitor_containing,
)
from agent.geometry.raster import RGB, Raster, solid

__all__ = [
    "CURRENT_OS",
    "RGB",
    "BoundingBox",
    "IntPoint",
    "LogicalCoord",
    "MonitorInfo",
    "PixelCoord",
    "Raster",
    "detect_os",
    "enumerate_monitors",
    "monitor_containing",
    "solid",
]
