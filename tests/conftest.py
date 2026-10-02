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

import inspect
import asyncio

@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        testfunction = pyfuncitem.obj
        funcargs = {
            arg: pyfuncitem.funcargs[arg]
            for arg in pyfuncitem._fixtureinfo.argnames
            if arg in pyfuncitem.funcargs
        }

        async def runner():
            async_gens = []
            try:
                for k, v in list(funcargs.items()):
                    if inspect.isasyncgen(v):
                        async_gens.append(v)
                        funcargs[k] = await v.__anext__()
                await testfunction(**funcargs)
            finally:
                for gen in reversed(async_gens):
                    try:
                        await gen.__anext__()
                    except (StopAsyncIteration, GeneratorExit, Exception):
                        pass

        asyncio.run(runner())
        return True
    return None
