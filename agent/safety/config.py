"""Governor configuration with safety-level presets."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Annotated, Self

from agent.core.enums import SafetyLevel
from agent.geometry.bbox import BoundingBox

_DEFAULT_FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "delete",
        "format",
        "uninstall",
        "shutdown",
        "reboot",
        "rm -rf",
        "drop table",
        "sudo",
        "chmod 777",
        "mkfs",
        "diskpart",
        ":(){",
        "reg delete",
        "remove-item",
        "invoke-expression",
    }
)

_LEVEL_PRESETS: dict[SafetyLevel, dict[str, object]] = {
    SafetyLevel.PERMISSIVE: {
        "max_actions_per_minute": 300,
        "max_total_actions": 5000,
        "max_wall_clock_seconds": 3600.0,
        "require_capability": False,
    },
    SafetyLevel.NORMAL: {
        "max_actions_per_minute": 60,
        "max_total_actions": 500,
        "max_wall_clock_seconds": 600.0,
        "require_capability": False,
    },
    SafetyLevel.STRICT: {
        "max_actions_per_minute": 30,
        "max_total_actions": 200,
        "max_wall_clock_seconds": 300.0,
        "require_capability": False,
    },
    SafetyLevel.PARANOID: {
        "max_actions_per_minute": 10,
        "max_total_actions": 50,
        "max_wall_clock_seconds": 120.0,
        "require_capability": True,
    },
}


class GovernorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: SafetyLevel = SafetyLevel.NORMAL
    max_actions_per_minute: Annotated[int, Field(ge=1, le=600)] = 60
    max_total_actions: Annotated[int, Field(ge=1)] = 500
    max_wall_clock_seconds: Annotated[float, Field(gt=0)] = 600.0
    forbidden_regions: list[BoundingBox] = Field(default_factory=list)
    forbidden_keywords: frozenset[str] = Field(
        default_factory=lambda: _DEFAULT_FORBIDDEN_KEYWORDS
    )
    dry_run: bool = False
    require_capability: bool = False
    audit_path: str = "audit.jsonl"

    @classmethod
    def for_level(cls, level: SafetyLevel, **overrides: object) -> Self:
        preset = dict(_LEVEL_PRESETS[level])
        preset.update(overrides)
        return cls(level=level, **preset)  # type: ignore[arg-type]
