from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.core.enums import ActionKind
from agent.motor.backends.null_backend import NullBackend
from agent.motor.controller import MotorController
from agent.motor.spec import (
    ClickAction,
    TypeAction,
    WaitAction,
    click,
    parse_action,
    spec_from_kind,
    type_text,
    wait,
)


def test_click_factory_sets_kind() -> None:
    spec = click(100, 200)
    assert spec.kind is ActionKind.CLICK
    assert spec.coord.x == 100
    assert spec.coord.y == 200


def test_type_requires_text() -> None:
    with pytest.raises(ValidationError):
        TypeAction(text="")  # type: ignore[call-arg]


def test_click_cannot_carry_text_field() -> None:
    with pytest.raises(ValidationError):
        ClickAction.model_validate({"kind": "click", "coord": {"x": 1, "y": 1}, "text": "nope"})


def test_parse_action_roundtrip() -> None:
    spec = click(3, 4)
    restored = parse_action(spec.model_dump(mode="json"))
    assert isinstance(restored, ClickAction)
    assert restored.coord.as_tuple() == (3, 4)


def test_spec_from_kind_maps_xy() -> None:
    spec = spec_from_kind(ActionKind.CLICK, {"x": 9, "y": 8})
    assert isinstance(spec, ClickAction)
    assert spec.coord.x == 9


@pytest.mark.asyncio
async def test_null_backend_records(fake_world: object) -> None:
    from agent.world.model import WorldModel

    assert isinstance(fake_world, WorldModel)
    backend = NullBackend()
    motor = MotorController(backend, fake_world, verify_delay=0.0)
    result = await motor.execute(wait(0.0))
    assert result.success
    assert len(backend.executed) == 1
    assert isinstance(backend.executed[0], WaitAction)


@pytest.mark.asyncio
async def test_wait_does_not_require_screen_change(fake_world: object) -> None:
    from agent.world.model import WorldModel

    assert isinstance(fake_world, WorldModel)
    motor = MotorController(NullBackend(), fake_world, verify_delay=0.0)
    result = await motor.execute(wait(0.0))
    assert result.success
    assert result.screen_changed is False


def test_type_text_factory() -> None:
    spec = type_text("hello")
    assert spec.text == "hello"
    assert spec.kind is ActionKind.TYPE
