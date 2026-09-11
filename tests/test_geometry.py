from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.geometry.bbox import BoundingBox
from agent.geometry.coord import PixelCoord
from agent.geometry.raster import Raster, solid


def test_pixelcoord_allows_negative_origin() -> None:
    p = PixelCoord(x=-1920, y=0)
    assert p.x == -1920


def test_pixelcoord_rejects_overflow() -> None:
    with pytest.raises(ValidationError):
        PixelCoord(x=100_000, y=0)


def test_bbox_center_is_computed() -> None:
    box = BoundingBox(x=10, y=20, width=100, height=50)
    assert box.center.x == 60
    assert box.center.y == 45


def test_bbox_contains_is_half_open() -> None:
    box = BoundingBox(x=0, y=0, width=100, height=100)
    assert box.contains(PixelCoord(x=0, y=0))
    assert box.contains(PixelCoord(x=99, y=99))
    assert not box.contains(PixelCoord(x=100, y=50))
    assert not box.contains(PixelCoord(x=200, y=200))


def test_bbox_iou_identical() -> None:
    box = BoundingBox(x=0, y=0, width=10, height=10)
    assert box.iou(box) == pytest.approx(1.0)


def test_bbox_iou_disjoint() -> None:
    a = BoundingBox(x=0, y=0, width=10, height=10)
    b = BoundingBox(x=20, y=20, width=10, height=10)
    assert a.iou(b) == 0.0


def test_raster_sha256_stable() -> None:
    a = solid(8, 8, (1, 2, 3))
    b = solid(8, 8, (1, 2, 3))
    assert a.sha256() == b.sha256()


def test_raster_rejects_bad_buffer() -> None:
    with pytest.raises(Exception):
        Raster(width=2, height=2, rgb=b"short")


def test_raster_diff_identical_is_zero() -> None:
    a = solid(4, 4, (9, 9, 9))
    assert a.diff_ratio(a) == 0.0


def test_raster_crop() -> None:
    src = solid(10, 10, (255, 0, 0))
    cropped = src.crop(BoundingBox(x=2, y=2, width=3, height=3))
    assert cropped.size == (3, 3)
    assert cropped.pixel(PixelCoord(x=0, y=0)) == (255, 0, 0)
