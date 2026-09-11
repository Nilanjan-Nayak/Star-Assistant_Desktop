from __future__ import annotations

from hypothesis import given, settings, strategies as st

from agent.geometry.bbox import BoundingBox
from agent.geometry.coord import PixelCoord
from agent.geometry.raster import solid


@given(
    x=st.integers(min_value=0, max_value=200),
    y=st.integers(min_value=0, max_value=200),
    w=st.integers(min_value=1, max_value=80),
    h=st.integers(min_value=1, max_value=80),
)
@settings(max_examples=40)
def test_bbox_contains_its_center(x: int, y: int, w: int, h: int) -> None:
    box = BoundingBox(x=x, y=y, width=w, height=h)
    assert box.contains(box.center)


@given(
    x=st.integers(min_value=-100, max_value=100),
    y=st.integers(min_value=-100, max_value=100),
    dx=st.integers(min_value=-50, max_value=50),
    dy=st.integers(min_value=-50, max_value=50),
)
@settings(max_examples=40)
def test_offset_is_additive(x: int, y: int, dx: int, dy: int) -> None:
    p = PixelCoord(x=x, y=y)
    q = p.offset(dx, dy)
    assert q.x == x + dx
    assert q.y == y + dy


@given(w=st.integers(min_value=1, max_value=12), h=st.integers(min_value=1, max_value=12))
@settings(max_examples=20)
def test_solid_hash_depends_on_size(w: int, h: int) -> None:
    a = solid(w, h, (1, 2, 3))
    assert len(a.rgb) == w * h * 3
    assert a.diff_ratio(a) == 0.0
