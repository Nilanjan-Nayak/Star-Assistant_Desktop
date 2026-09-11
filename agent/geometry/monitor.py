"""Monitor enumeration with DPI awareness."""

from __future__ import annotations

import logging
import platform

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Annotated

from agent.core.enums import OSFamily
from agent.core.logging import get_logger, log_ctx
from agent.geometry.bbox import BoundingBox
from agent.geometry.coord import LogicalCoord, PixelCoord

_log = get_logger("agent.monitor")


def detect_os() -> OSFamily:
    mapping: dict[str, OSFamily] = {
        "windows": OSFamily.WINDOWS,
        "darwin": OSFamily.MACOS,
        "linux": OSFamily.LINUX,
    }
    return mapping.get(platform.system().lower(), OSFamily.UNKNOWN)


CURRENT_OS: OSFamily = detect_os()


class MonitorInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    index: Annotated[int, Field(ge=1)]
    x: int
    y: int
    width: Annotated[int, Field(ge=1)]
    height: Annotated[int, Field(ge=1)]
    scale_factor: Annotated[float, Field(gt=0, le=8)] = 1.0
    is_primary: bool = False
    name: str = ""

    @property
    def bbox(self) -> BoundingBox:
        return BoundingBox(x=self.x, y=self.y, width=self.width, height=self.height)

    def contains(self, point: PixelCoord) -> bool:
        return self.bbox.contains(point)

    def to_physical(self, coord: LogicalCoord) -> PixelCoord:
        return PixelCoord(
            x=self.x + int(coord.x * self.scale_factor),
            y=self.y + int(coord.y * self.scale_factor),
        )

    def to_logical(self, coord: PixelCoord) -> LogicalCoord:
        return LogicalCoord(
            x=int((coord.x - self.x) / self.scale_factor),
            y=int((coord.y - self.y) / self.scale_factor),
        )


def enumerate_monitors() -> list[MonitorInfo]:
    monitors: list[MonitorInfo] = []
    try:
        import mss

        with mss.mss() as sct:
            for i, mon in enumerate(sct.monitors[1:], start=1):
                scale = _detect_scale()
                monitors.append(
                    MonitorInfo(
                        index=i,
                        x=int(mon["left"]),
                        y=int(mon["top"]),
                        width=int(mon["width"]),
                        height=int(mon["height"]),
                        scale_factor=scale,
                        is_primary=(i == 1),
                        name=str(mon.get("name", f"monitor-{i}")),
                    )
                )
    except Exception as exc:
        log_ctx(_log, logging.WARNING, "monitor.enumerate.failed", error=str(exc))

    if not monitors:
        monitors.append(
            MonitorInfo(index=1, x=0, y=0, width=1920, height=1080, is_primary=True, name="virtual")
        )
    return monitors


def monitor_containing(
    point: PixelCoord, monitors: list[MonitorInfo] | None = None
) -> MonitorInfo | None:
    pool = monitors if monitors is not None else enumerate_monitors()
    for mon in pool:
        if mon.contains(point):
            return mon
    return None


def _detect_scale() -> float:
    if CURRENT_OS is OSFamily.WINDOWS:
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
            return float(ctypes.windll.shcore.GetScaleFactorForDevice(0)) / 100.0  # type: ignore[attr-defined]
        except Exception:
            return 1.0
    if CURRENT_OS is OSFamily.MACOS:
        return 2.0
    return 1.0
