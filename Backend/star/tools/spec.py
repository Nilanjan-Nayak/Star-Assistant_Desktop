"""Typed tool specifications — the contract between the brain and the OS.

Blueprint §7 Phase 4: "Build a typed tool registry with permissions, risk levels
and audit trails. Wrap existing tools; do not duplicate subsystems."

A :class:`ToolSpec` is *metadata + a handler*. Metadata (category, owning agent,
risk, JSON-schema parameters, reversibility, timeout) is what lets the planner,
the permission engine, the audit log and the UI all reason about a tool without
ever importing its implementation.
"""

from __future__ import annotations

import enum
import inspect
import typing
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from Backend.star.brain.schemas import AgentName
from Backend.star.tools.risk import classify_risk

__all__ = [
    "PREFIX_AGENT",
    "TOOL_AGENT",
    "TOOL_CATEGORY",
    "ToolCategory",
    "RESERVED_RESULT_KEYS",
    "ToolResult",
    "ToolSpec",
    "agent_for_tool",
    "category_for_tool",
    "spec_from_function",
    "validate_arguments",
]


@enum.unique
class ToolCategory(str, enum.Enum):
    SYSTEM = "system"
    MEDIA = "media"
    VISION = "vision"
    FILES = "files"
    WEB = "web"
    BROWSER = "browser"
    CODE = "code"
    MEMORY = "memory"
    LEARNING = "learning"
    META = "meta"
    OTHER = "other"


#: ``namespace.`` prefix → owning agent (STAR 2.0 tools follow this convention).
PREFIX_AGENT: tuple[tuple[str, AgentName], ...] = (
    ("browser.", AgentName.BROWSER),
    ("web.", AgentName.BROWSER),
    ("nav.", AgentName.BROWSER),
    ("file.", AgentName.FILESYSTEM),
    ("fs.", AgentName.FILESYSTEM),
    ("computer.", AgentName.COMPUTER),
    ("screen.", AgentName.COMPUTER),
    ("input.", AgentName.COMPUTER),
    ("system.", AgentName.SYSTEM),
    ("media.", AgentName.SYSTEM),
    ("app.", AgentName.SYSTEM),
    ("code.", AgentName.CODING),
    ("repo.", AgentName.CODING),
    ("git.", AgentName.CODING),
    ("research.", AgentName.RESEARCH),
    ("chat.", AgentName.CONVERSATION),
    ("orchestrator.", AgentName.SYSTEM),
    ("plan.", AgentName.SYSTEM),
    ("security.", AgentName.SYSTEM),
)

#: exact legacy tool names (``Backend/tools`` auto-discovery) → owning agent.
TOOL_AGENT: dict[str, AgentName] = {
    "adjust_volume": AgentName.SYSTEM,
    "set_volume": AgentName.SYSTEM,
    "get_volume": AgentName.SYSTEM,
    "set_mute": AgentName.SYSTEM,
    "adjust_brightness": AgentName.SYSTEM,
    "set_brightness": AgentName.SYSTEM,
    "get_brightness": AgentName.SYSTEM,
    "launch_application": AgentName.SYSTEM,
    "close_application": AgentName.SYSTEM,
    "open_special_folder": AgentName.SYSTEM,
    "lock_workstation": AgentName.SYSTEM,
    "get_system_status": AgentName.SYSTEM,
    "get_agent_metrics": AgentName.SYSTEM,
    "calculate_math": AgentName.SYSTEM,
    "execute_autonomous_goal": AgentName.SYSTEM,
    "youtube_play_last": AgentName.SYSTEM,
    "youtube_play_from_history": AgentName.SYSTEM,
    "youtube_get_history": AgentName.SYSTEM,
    "youtube_clear_local_history": AgentName.SYSTEM,
    "take_screenshot": AgentName.COMPUTER,
    "see_screen": AgentName.COMPUTER,
    "execute_computer_skill": AgentName.COMPUTER,
    "search_web": AgentName.RESEARCH,
    "web_search": AgentName.RESEARCH,
    "google_search": AgentName.RESEARCH,
    "wikipedia_search": AgentName.RESEARCH,
    "web_quick_answer": AgentName.RESEARCH,
    "youtube_open_history_page": AgentName.BROWSER,
    "remember_fact": AgentName.CONVERSATION,
    "recall_memory": AgentName.CONVERSATION,
}

#: exact legacy tool names → category.
TOOL_CATEGORY: dict[str, ToolCategory] = {
    "adjust_volume": ToolCategory.MEDIA,
    "set_volume": ToolCategory.MEDIA,
    "get_volume": ToolCategory.MEDIA,
    "set_mute": ToolCategory.MEDIA,
    "adjust_brightness": ToolCategory.SYSTEM,
    "set_brightness": ToolCategory.SYSTEM,
    "get_brightness": ToolCategory.SYSTEM,
    "launch_application": ToolCategory.SYSTEM,
    "close_application": ToolCategory.SYSTEM,
    "open_special_folder": ToolCategory.FILES,
    "lock_workstation": ToolCategory.SYSTEM,
    "get_system_status": ToolCategory.META,
    "get_agent_metrics": ToolCategory.META,
    "calculate_math": ToolCategory.OTHER,
    "execute_autonomous_goal": ToolCategory.META,
    "take_screenshot": ToolCategory.VISION,
    "see_screen": ToolCategory.VISION,
    "execute_computer_skill": ToolCategory.VISION,
    "search_web": ToolCategory.WEB,
    "web_search": ToolCategory.WEB,
    "google_search": ToolCategory.WEB,
    "wikipedia_search": ToolCategory.WEB,
    "web_quick_answer": ToolCategory.WEB,
    "remember_fact": ToolCategory.MEMORY,
    "recall_memory": ToolCategory.MEMORY,
}

_AGENT_KEYWORDS: tuple[tuple[str, AgentName], ...] = (
    ("browser", AgentName.BROWSER),
    ("youtube", AgentName.BROWSER),
    ("screen", AgentName.COMPUTER),
    ("click", AgentName.COMPUTER),
    ("type", AgentName.COMPUTER),
    ("mouse", AgentName.COMPUTER),
    ("keyboard", AgentName.COMPUTER),
    ("file", AgentName.FILESYSTEM),
    ("folder", AgentName.FILESYSTEM),
    ("code", AgentName.CODING),
    ("python", AgentName.CODING),
    ("search", AgentName.RESEARCH),
    ("web", AgentName.RESEARCH),
    ("volume", AgentName.SYSTEM),
    ("brightness", AgentName.SYSTEM),
    ("app", AgentName.SYSTEM),
    ("music", AgentName.SYSTEM),
    ("play", AgentName.SYSTEM),
)

_CATEGORY_KEYWORDS: tuple[tuple[str, ToolCategory], ...] = (
    ("youtube", ToolCategory.MEDIA),
    ("music", ToolCategory.MEDIA),
    ("play", ToolCategory.MEDIA),
    ("volume", ToolCategory.MEDIA),
    ("mute", ToolCategory.MEDIA),
    ("screenshot", ToolCategory.VISION),
    ("screen", ToolCategory.VISION),
    ("see", ToolCategory.VISION),
    ("ocr", ToolCategory.VISION),
    ("file", ToolCategory.FILES),
    ("folder", ToolCategory.FILES),
    ("path", ToolCategory.FILES),
    ("search", ToolCategory.WEB),
    ("web", ToolCategory.WEB),
    ("wikipedia", ToolCategory.WEB),
    ("browser", ToolCategory.BROWSER),
    ("navigate", ToolCategory.BROWSER),
    ("code", ToolCategory.CODE),
    ("python", ToolCategory.CODE),
    ("git", ToolCategory.CODE),
    ("memory", ToolCategory.MEMORY),
    ("remember", ToolCategory.MEMORY),
    ("recall", ToolCategory.MEMORY),
    ("metrics", ToolCategory.META),
    ("status", ToolCategory.META),
    ("brightness", ToolCategory.SYSTEM),
    ("app", ToolCategory.SYSTEM),
    ("lock", ToolCategory.SYSTEM),
    ("shutdown", ToolCategory.SYSTEM),
)


def agent_for_tool(tool: str) -> AgentName:
    """Which agent should own this tool (prefix → exact name → keyword → system)."""
    name = (tool or "").strip().lower()
    for prefix, agent in PREFIX_AGENT:
        if name.startswith(prefix):
            return agent
    if name in TOOL_AGENT:
        return TOOL_AGENT[name]
    bare = name.rsplit(".", 1)[-1]
    if bare in TOOL_AGENT:
        return TOOL_AGENT[bare]
    for keyword, agent in _AGENT_KEYWORDS:
        if keyword in name:
            return agent
    return AgentName.SYSTEM


def category_for_tool(tool: str) -> ToolCategory:
    """Which capability bucket this tool belongs to (for UI grouping + policy)."""
    name = (tool or "").strip().lower()
    if name in TOOL_CATEGORY:
        return TOOL_CATEGORY[name]
    bare = name.rsplit(".", 1)[-1]
    if bare in TOOL_CATEGORY:
        return TOOL_CATEGORY[bare]
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in name:
            return category
    return ToolCategory.OTHER


class ToolSpec(BaseModel):
    """Everything the platform needs to know about one capability."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    description: str = ""
    category: ToolCategory = ToolCategory.OTHER
    agent: AgentName = AgentName.SYSTEM
    risk: str = "medium"
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}, "required": []})
    handler: Callable[..., Any] | None = None
    timeout_s: float = Field(default=20.0, gt=0)
    idempotent: bool = False
    reversible: bool = True
    dry_run_safe: bool = True             # can be simulated without touching the OS
    confirm_above: str | None = None      # per-tool override of the global threshold
    origin: str = "star2"                 # star2 | legacy | agent
    tags: tuple[str, ...] = ()
    module: str = ""

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        from Backend.star.brain.reasoning import TOOL_NAME_RE

        cleaned = str(value).strip()
        if not TOOL_NAME_RE.match(cleaned):
            raise ValueError(f"invalid tool name: {value!r}")
        return cleaned

    @field_validator("risk", "confirm_above")
    @classmethod
    def _valid_risk(cls, value: str | None) -> str | None:
        from Backend.star.tools.risk import RISK_ORDER

        if value is None:
            return None
        cleaned = str(value).lower()
        if cleaned not in RISK_ORDER:
            raise ValueError(f"invalid risk tier: {value!r}")
        return cleaned

    @property
    def required(self) -> list[str]:
        return [str(name) for name in (self.parameters or {}).get("required", [])]

    @property
    def properties(self) -> dict[str, Any]:
        return dict((self.parameters or {}).get("properties", {}))

    @property
    def callable(self) -> bool:
        return callable(self.handler)

    def public(self) -> dict[str, Any]:
        """JSON-safe description for the gateway, console and audit log."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "agent": self.agent.value,
            "risk": self.risk,
            "parameters": self.parameters,
            "required": self.required,
            "timeout_s": self.timeout_s,
            "idempotent": self.idempotent,
            "reversible": self.reversible,
            "dry_run_safe": self.dry_run_safe,
            "confirm_above": self.confirm_above,
            "origin": self.origin,
            "tags": list(self.tags),
            "module": self.module,
            "callable": self.callable,
        }

    def schema(self) -> dict[str, Any]:
        """OpenAI/Ollama function-calling shape (fed to the LLM providers)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolResult(BaseModel):
    """What came back from one tool call — including *why* it did not run."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = False
    tool: str = ""
    call_id: str = ""
    decision: str = "executed"   # executed | simulated | denied | needs_confirmation | blocked
                                 # | invalid_args | unknown_tool | timeout | error
    risk: str = "unknown"
    data: dict[str, Any] = Field(default_factory=dict)
    output: str = ""
    error: str | None = None
    dry_run: bool = False
    duration_ms: float = Field(default=0.0, ge=0.0)
    audited: bool = False
    confirmation_id: str | None = None
    notes: list[str] = Field(default_factory=list)

    def as_call_result(self) -> dict[str, Any]:
        """Shape stored on :class:`~Backend.star.brain.schemas.ToolCall`.

        Handler data is merged at the top level so the legacy result shape
        (``{"success": ..., "result": ...}``) survives unchanged for the HUD.
        """
        payload: dict[str, Any] = {
            "success": self.ok,
            "decision": self.decision,
            "dry_run": self.dry_run,
            "duration_ms": self.duration_ms,
        }
        for key, value in (self.data or {}).items():
            if key not in RESERVED_RESULT_KEYS:
                payload[key] = value
        if self.output:
            payload["output"] = self.output[:500]
        if self.error:
            payload["error"] = self.error[:500]
        if self.confirmation_id:
            payload["confirmation_id"] = self.confirmation_id
        if self.notes:
            payload["notes"] = self.notes[:8]
        return payload


#: keys the executor owns — handler data must not shadow them
RESERVED_RESULT_KEYS: tuple[str, ...] = ("success", "decision", "dry_run", "duration_ms")

_COERCERS: dict[str, Callable[[Any], Any]] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": lambda value: value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on", "হ্যাঁ"},
}


def validate_arguments(
    spec: ToolSpec, arguments: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Coerce/validate arguments against the spec's JSON-schema-lite parameters.

    Returns ``(arguments, errors, notes)``. Unknown arguments are kept (legacy
    tools accept ``**kwargs``) but reported in ``notes``.
    """
    args = dict(arguments or {})
    properties = spec.properties
    required = spec.required
    errors: list[str] = []
    notes: list[str] = []

    for name in required:
        if name not in args or args[name] in (None, ""):
            errors.append(f"missing required argument '{name}'")

    for name, value in list(args.items()):
        schema = properties.get(name)
        if schema is None:
            notes.append(f"undeclared argument '{name}'")
            continue
        expected = str(schema.get("type") or "string").lower()
        coercer = _COERCERS.get(expected)
        if coercer is None:
            continue                                   # object/array: pass through
        if expected == "boolean" and isinstance(value, bool):
            continue
        if expected in ("number", "integer") and isinstance(value, (int, float)) and not isinstance(value, bool):
            if expected == "integer" and isinstance(value, float) and not value.is_integer():
                errors.append(f"argument '{name}' must be an integer")
            continue
        try:
            coerced = coercer(value)
        except (TypeError, ValueError):
            errors.append(f"argument '{name}' is not a valid {expected}")
            continue
        if expected == "integer":
            try:
                coerced = int(float(coerced))
            except (TypeError, ValueError):
                errors.append(f"argument '{name}' is not a valid integer")
                continue
        if coerced != value:
            notes.append(f"coerced '{name}' to {expected}")
        args[name] = coerced

    return args, errors, notes


_ANNOTATION_NAMES = {"int": "integer", "float": "number", "bool": "boolean", "str": "string"}


def _json_kind(annotation: Any) -> str:
    """Map a (possibly stringified) annotation onto a JSON-schema type."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return "string"
    if isinstance(annotation, str):
        text = annotation.strip().lower()
        if text in _ANNOTATION_NAMES:
            return _ANNOTATION_NAMES[text]
        for name, kind in _ANNOTATION_NAMES.items():      # e.g. "int | None"
            if text.startswith(name) or f"| {name}" in text or f"{name} |" in text:
                return kind
        return "string"
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    origin = getattr(annotation, "__origin__", None)
    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"
    return "string"


def spec_from_function(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str = "",
    category: ToolCategory | None = None,
    agent: AgentName | None = None,
    risk: str | None = None,
    origin: str = "star2",
    timeout_s: float = 20.0,
    **extra: Any,
) -> ToolSpec:
    """Build a :class:`ToolSpec` from a plain Python callable (signature → schema)."""
    tool_name = name or func.__name__
    signature = inspect.signature(func)
    try:                                       # PEP 563: annotations may be strings
        hints = typing.get_type_hints(func)
    except Exception:                          # noqa: BLE001 — unresolvable forward refs
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter_name, parameter in signature.parameters.items():
        if parameter_name in ("self", "cls"):
            continue
        annotation = hints.get(parameter_name, parameter.annotation)
        kind = _json_kind(annotation)
        properties[parameter_name] = {"type": kind, "description": f"Parameter {parameter_name}"}
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter_name)
    doc = description or (inspect.getdoc(func) or "").strip()
    return ToolSpec(
        name=tool_name,
        description=doc.splitlines()[0] if doc else "",
        category=category or category_for_tool(tool_name),
        agent=agent or agent_for_tool(tool_name),
        risk=(risk or classify_risk(tool_name)).lower(),
        parameters={"type": "object", "properties": properties, "required": required},
        handler=func,
        timeout_s=timeout_s,
        origin=origin,
        module=getattr(func, "__module__", "") or "",
        **extra,
    )
