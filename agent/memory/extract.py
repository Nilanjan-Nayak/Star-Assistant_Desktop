"""Pull structured preferences out of free-text goals and skill results."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.core.enums import MemoryKind
from agent.core.ids import SkillName

_VOLUME = re.compile(
    r"\bvolume(?:\s+(?:to|at|of)|(?:\s*[:=]))?\s*(\d{1,3})\b",
    re.IGNORECASE,
)
_BRIGHT = re.compile(
    r"\bbrightness(?:\s+(?:to|at|of)|(?:\s*[:=]))?\s*(\d{1,3})\b",
    re.IGNORECASE,
)
@dataclass(frozen=True, slots=True)
class ExtractedPref:
    key: str
    value: str
    text: str
    kind: MemoryKind = MemoryKind.PREFERENCE


def _clamp_percent(raw: str) -> str | None:
    level = int(raw)
    if 0 <= level <= 100:
        return str(level)
    return None


def extract_from_goal(goal: str) -> list[ExtractedPref]:
    found: list[ExtractedPref] = []
    vol = _VOLUME.search(goal)
    if vol is not None:
        value = _clamp_percent(vol.group(1))
        if value is not None:
            found.append(
                ExtractedPref(
                    key="volume.level",
                    value=value,
                    text=f"Preferred volume is {value}",
                )
            )
    bright = _BRIGHT.search(goal)
    if bright is not None:
        value = _clamp_percent(bright.group(1))
        if value is not None:
            found.append(
                ExtractedPref(
                    key="brightness.level",
                    value=value,
                    text=f"Preferred brightness is {value}",
                )
            )
    return found


def extract_from_skill(
    skill: SkillName,
    params: dict[str, object],
) -> list[ExtractedPref]:
    name = str(skill)
    found: list[ExtractedPref] = []
    if name == "volume" and "level" in params:
        value = _clamp_percent(str(params["level"]))
        if value is not None:
            found.append(
                ExtractedPref(
                    key="volume.level",
                    value=value,
                    text=f"Preferred volume is {value}",
                )
            )
    elif name == "brightness" and "level" in params:
        value = _clamp_percent(str(params["level"]))
        if value is not None:
            found.append(
                ExtractedPref(
                    key="brightness.level",
                    value=value,
                    text=f"Preferred brightness is {value}",
                )
            )
    elif name == "youtube" and "query" in params:
        query = str(params["query"]).strip()
        if query:
            found.append(
                ExtractedPref(
                    key="youtube.query",
                    value=query,
                    text=f"Preferred YouTube query is {query!r}",
                )
            )
    elif name == "launch" and "app" in params:
        app = str(params["app"]).strip()
        if app:
            found.append(
                ExtractedPref(
                    key="launch.app",
                    value=app,
                    text=f"Preferred app to launch is {app!r}",
                )
            )
    return found
