"""Strong ID types — prevent accidental cross-use of different string IDs.

Each ``NewType`` is nominally distinct at the type level: mypy will refuse
to pass an ``ActionId`` where a ``StepId`` is expected. Runtime helpers
validate the compact ``'<prefix>_<12 hex chars>'`` shape so untrusted
input (JSON, CLI, logs) cannot be silently coerced.
"""

from __future__ import annotations

import re
import uuid
from typing import Final, NewType

ActionId = NewType("ActionId", str)
StepId = NewType("StepId", str)
EpisodeId = NewType("EpisodeId", str)
ElementId = NewType("ElementId", str)
SnapshotId = NewType("SnapshotId", str)
ScreenHash = NewType("ScreenHash", str)
CapToken = NewType("CapToken", str)
SkillName = NewType("SkillName", str)
BreakerName = NewType("BreakerName", str)
CorrelationId = NewType("CorrelationId", str)
SpanId = NewType("SpanId", str)
EventId = NewType("EventId", str)
MemoryId = NewType("MemoryId", int)

_ID_LEN: Final[int] = 12
_HEX: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{12}$")
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SKILL: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def new_id(prefix: str) -> str:
    """Generate a compact unique id: ``'<prefix>_<12 hex chars>'``."""
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex[:_ID_LEN]}"


def _parse(raw: str, prefix: str) -> str:
    expected = prefix + "_"
    if not raw.startswith(expected):
        raise ValueError(f"id {raw!r} does not start with {expected!r}")
    rest = raw[len(expected) :]
    if not _HEX.fullmatch(rest):
        raise ValueError(f"id {raw!r} has invalid suffix")
    return raw


def new_action_id() -> ActionId:
    return ActionId(new_id("act"))


def new_step_id() -> StepId:
    return StepId(new_id("step"))


def new_episode_id() -> EpisodeId:
    return EpisodeId(new_id("ep"))


def new_element_id() -> ElementId:
    return ElementId(new_id("el"))


def new_snapshot_id() -> SnapshotId:
    return SnapshotId(new_id("snap"))


def new_event_id() -> EventId:
    return EventId(new_id("evt"))


def new_correlation_id() -> CorrelationId:
    return CorrelationId(new_id("corr"))


def new_span_id() -> SpanId:
    return SpanId(new_id("span"))


def new_cap_token() -> CapToken:
    return CapToken(new_id("cap"))


def parse_action_id(raw: str) -> ActionId:
    return ActionId(_parse(raw, "act"))


def parse_step_id(raw: str) -> StepId:
    return StepId(_parse(raw, "step"))


def parse_episode_id(raw: str) -> EpisodeId:
    return EpisodeId(_parse(raw, "ep"))


def parse_element_id(raw: str) -> ElementId:
    return ElementId(_parse(raw, "el"))


def parse_snapshot_id(raw: str) -> SnapshotId:
    return SnapshotId(_parse(raw, "snap"))


def parse_event_id(raw: str) -> EventId:
    return EventId(_parse(raw, "evt"))


def parse_correlation_id(raw: str) -> CorrelationId:
    return CorrelationId(_parse(raw, "corr"))


def parse_cap_token(raw: str) -> CapToken:
    return CapToken(_parse(raw, "cap"))


def parse_screen_hash(raw: str) -> ScreenHash:
    lowered = raw.lower()
    if not _SHA256.fullmatch(lowered):
        raise ValueError(f"not a sha256 hex digest: {raw!r}")
    return ScreenHash(lowered)


def parse_skill_name(raw: str) -> SkillName:
    if not _SKILL.fullmatch(raw):
        raise ValueError(f"invalid skill name: {raw!r}")
    return SkillName(raw)


def parse_breaker_name(raw: str) -> BreakerName:
    if not raw or len(raw) > 80:
        raise ValueError(f"invalid breaker name: {raw!r}")
    return BreakerName(raw)
