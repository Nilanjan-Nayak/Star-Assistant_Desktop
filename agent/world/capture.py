"""Screen capture with a cascade of backends (mss → PIL ImageGrab → OS tools)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent.core.errors import BackendUnavailable
from agent.geometry.bbox import BoundingBox
from agent.geometry.monitor import MonitorInfo, enumerate_monitors
from agent.geometry.raster import Raster
from agent.world.snapshot import ScreenSnapshot


@runtime_checkable
class CaptureBackend(Protocol):
    monitors: list[MonitorInfo]

    def grab(self, region: BoundingBox | None = None, monitor: int = 1) -> ScreenSnapshot: ...


def capture_available() -> bool:
    """True if at least one grabber looks importable / on PATH. Not a display check."""
    try:
        import mss  # noqa: F401

        return True
    except ImportError:
        pass
    try:
        from PIL import ImageGrab  # noqa: F401

        return True
    except ImportError:
        pass
    return any(shutil.which(name) for name in ("gnome-screenshot", "grim", "scrot", "import"))


class ScreenCapture:
    def __init__(self, monitors: list[MonitorInfo] | None = None) -> None:
        self.monitors: list[MonitorInfo] = monitors or enumerate_monitors()

    def grab(self, region: BoundingBox | None = None, monitor: int = 1) -> ScreenSnapshot:
        mon = next((m for m in self.monitors if m.index == monitor), self.monitors[0])
        errors: list[str] = []
        for grabber in (_grab_mss, _grab_imagegrab, _grab_cli):
            try:
                raster = grabber(region, mon)
            except Exception as exc:
                errors.append(f"{grabber.__name__}: {exc}")
                continue
            return ScreenSnapshot(raster=raster, monitor=mon, content_hash=raster.sha256())
        raise BackendUnavailable(
            "no screen-capture backend worked (install mss+Pillow, or use a desktop session)",
            errors=errors,
        )


def _grab_mss(region: BoundingBox | None, mon: MonitorInfo) -> Raster:
    import mss
    from PIL import Image

    with mss.mss() as sct:
        area = (
            {
                "left": region.x,
                "top": region.y,
                "width": region.width,
                "height": region.height,
            }
            if region is not None
            else {
                "left": mon.x,
                "top": mon.y,
                "width": mon.width,
                "height": mon.height,
            }
        )
        shot = sct.grab(area)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return Raster.from_pil(image)


def _grab_imagegrab(region: BoundingBox | None, mon: MonitorInfo) -> Raster:
    from PIL import ImageGrab

    if region is not None:
        bbox = (region.x, region.y, region.right, region.bottom)
    else:
        bbox = (mon.x, mon.y, mon.x + mon.width, mon.y + mon.height)
    try:
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
    except TypeError:
        image = ImageGrab.grab(bbox=bbox)
    return Raster.from_pil(image)


def _grab_cli(region: BoundingBox | None, mon: MonitorInfo) -> Raster:
    """Last-resort: gnome-screenshot / grim / scrot / ImageMagick import."""
    _ = (region, mon)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "shot.png"
        commands: list[list[str]] = []
        if shutil.which("gnome-screenshot"):
            commands.append(["gnome-screenshot", "-f", str(dest)])
        if shutil.which("grim"):
            commands.append(["grim", str(dest)])
        if shutil.which("scrot"):
            commands.append(["scrot", "-o", str(dest)])
        if shutil.which("import"):
            commands.append(["import", "-window", "root", str(dest)])
        if not commands:
            raise RuntimeError("no screenshot CLI on PATH")
        last_err = "no command ran"
        for argv in commands:
            try:
                subprocess.run(argv, check=True, capture_output=True, timeout=8)
            except Exception as exc:
                last_err = str(exc)
                continue
            if dest.exists():
                from PIL import Image

                return Raster.from_pil(Image.open(dest))
        raise RuntimeError(last_err)
