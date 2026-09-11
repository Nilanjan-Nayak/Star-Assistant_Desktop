from __future__ import annotations

import pytest

from agent.core.ids import new_action_id, parse_action_id, parse_screen_hash, parse_skill_name


def test_action_id_roundtrip() -> None:
    aid = new_action_id()
    assert str(aid).startswith("act_")
    assert parse_action_id(str(aid)) == aid


def test_parse_action_id_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_action_id("nope")
    with pytest.raises(ValueError):
        parse_action_id("act_zzzz")


def test_skill_name() -> None:
    assert parse_skill_name("volume") == "volume"
    with pytest.raises(ValueError):
        parse_skill_name("Volume")
    with pytest.raises(ValueError):
        parse_skill_name("rm -rf")


def test_screen_hash() -> None:
    digest = "a" * 64
    assert parse_screen_hash(digest) == digest
    with pytest.raises(ValueError):
        parse_screen_hash("abc")
