from __future__ import annotations

import pytest

from agent.core.errors import MotorError
from agent.core.result import Err, Ok, collect, is_err, is_ok


def test_ok_unwrap() -> None:
    r = Ok(42)
    assert is_ok(r)
    assert r.unwrap() == 42
    assert r.map(lambda n: n + 1).unwrap() == 43


def test_err_unwrap_raises() -> None:
    r: Err[MotorError] = Err(MotorError("boom"))
    assert is_err(r)
    with pytest.raises(MotorError):
        r.unwrap()
    assert r.unwrap_or(7) == 7


def test_and_then_short_circuits() -> None:
    start: Ok[int] = Ok(2)
    chained = start.and_then(lambda n: Ok(n * 3))
    assert chained.unwrap() == 6
    failed = start.and_then(lambda n: Err(MotorError("no")))
    assert is_err(failed)


def test_collect() -> None:
    ok_all = collect([Ok(1), Ok(2), Ok(3)])
    assert ok_all.unwrap() == [1, 2, 3]
    mixed = collect([Ok(1), Err(MotorError("x")), Ok(3)])
    assert is_err(mixed)
