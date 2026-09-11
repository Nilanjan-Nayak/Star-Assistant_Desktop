from __future__ import annotations

import pytest

from agent.core.clock import FrozenClock
from agent.geometry.monitor import MonitorInfo
from agent.geometry.raster import solid
from agent.motor.backends.null_backend import NullBackend
from agent.world.differ import ScreenDiffer
from agent.world.fake import FakeCapture
from agent.world.model import WorldModel


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def null_backend() -> NullBackend:
    return NullBackend()


@pytest.fixture
def fake_world() -> WorldModel:
    capture = FakeCapture(
        raster=solid(64, 48, (10, 20, 30)),
        monitor=MonitorInfo(index=1, x=0, y=0, width=64, height=48, is_primary=True, name="test"),
    )
    return WorldModel(capture, ScreenDiffer())
