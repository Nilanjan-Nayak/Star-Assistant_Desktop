"""PIL-free RGB raster. Keeps the typed core independent of imaging libraries.

Perception strategies that need PIL convert at the boundary via
:meth:`Raster.to_pil` / :meth:`Raster.from_pil`. Tests mint rasters with
:func:`solid` and never import Pillow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from agent.core.errors import ValidationFailed
from agent.core.ids import ScreenHash
from agent.geometry.bbox import BoundingBox
from agent.geometry.coord import PixelCoord

type RGB = tuple[int, int, int]


def _clamp_channel(value: int) -> int:
    if not 0 <= value <= 255:
        raise ValidationFailed("RGB channel out of range", value=value)
    return value


@dataclass(frozen=True, slots=True)
class Raster:
    width: int
    height: int
    rgb: bytes
    mode: Literal["RGB"] = "RGB"

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValidationFailed("raster dimensions must be >= 1")
        expected = self.width * self.height * 3
        if len(self.rgb) != expected:
            raise ValidationFailed(
                "rgb buffer length mismatch",
                expected=expected,
                actual=len(self.rgb),
            )

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def sha256(self) -> ScreenHash:
        digest = hashlib.sha256(self.rgb).hexdigest()
        return ScreenHash(digest)

    def pixel(self, point: PixelCoord) -> RGB:
        if not (0 <= point.x < self.width and 0 <= point.y < self.height):
            raise ValidationFailed("pixel out of raster bounds", coord=point.as_tuple())
        index = (point.y * self.width + point.x) * 3
        buf = self.rgb
        return (buf[index], buf[index + 1], buf[index + 2])

    def crop(self, box: BoundingBox) -> Raster:
        x0 = max(0, box.x)
        y0 = max(0, box.y)
        x1 = min(self.width, box.right)
        y1 = min(self.height, box.bottom)
        if x1 <= x0 or y1 <= y0:
            raise ValidationFailed("crop box does not intersect raster")
        width = x1 - x0
        height = y1 - y0
        rows: list[bytes] = []
        src = self.rgb
        for y in range(y0, y1):
            start = (y * self.width + x0) * 3
            rows.append(src[start : start + width * 3])
        return Raster(width=width, height=height, rgb=b"".join(rows))

    def save(self, path: str | Path) -> None:
        """Write PNG/JPEG via Pillow when available."""
        image = self.to_pil()
        saver = getattr(image, "save", None)
        if saver is None:
            raise ValidationFailed("raster could not be saved (Pillow missing?)")
        saver(str(path))

    def to_pil(self) -> object:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ImportError("Pillow is required to convert Raster → PIL") from exc
        return Image.frombytes("RGB", (self.width, self.height), self.rgb)

    @classmethod
    def from_pil(cls, image: object) -> Raster:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise ImportError("Pillow is required to convert PIL → Raster") from exc
        if not isinstance(image, Image.Image):
            raise ValidationFailed("expected a PIL.Image.Image", got=type(image).__name__)
        rgb = image.convert("RGB")
        return cls(width=rgb.width, height=rgb.height, rgb=rgb.tobytes())

    def diff_ratio(self, other: Raster) -> float:
        """Mean channel-wise absolute difference in ``[0, 1]``."""
        if self.width != other.width or self.height != other.height:
            return 1.0
        if self.rgb == other.rgb:
            return 0.0
        total = 0
        a = self.rgb
        b = other.rgb
        for i in range(len(a)):
            total += abs(a[i] - b[i])
        return total / (len(a) * 255.0)

    def changed_bbox(self, other: Raster, *, threshold: int = 30) -> BoundingBox | None:
        if self.width != other.width or self.height != other.height:
            return BoundingBox(x=0, y=0, width=self.width, height=self.height)
        min_x = self.width
        min_y = self.height
        max_x = -1
        max_y = -1
        a = self.rgb
        b = other.rgb
        width = self.width
        for y in range(self.height):
            row = y * width * 3
            for x in range(width):
                i = row + x * 3
                if (
                    abs(a[i] - b[i]) > threshold
                    or abs(a[i + 1] - b[i + 1]) > threshold
                    or abs(a[i + 2] - b[i + 2]) > threshold
                ):
                    if x < min_x:
                        min_x = x
                    if y < min_y:
                        min_y = y
                    if x > max_x:
                        max_x = x
                    if y > max_y:
                        max_y = y
        if max_x < 0:
            return None
        return BoundingBox(x=min_x, y=min_y, width=max_x - min_x + 1, height=max_y - min_y + 1)


def solid(width: int, height: int, color: RGB = (0, 0, 0)) -> Raster:
    r, g, b = (_clamp_channel(c) for c in color)
    return Raster(width=width, height=height, rgb=bytes((r, g, b)) * (width * height))
