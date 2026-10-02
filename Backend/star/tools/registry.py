"""The typed tool registry — one place that knows every capability Star has.

Blueprint §7 Phase 4: "Build a typed tool registry with permissions, risk levels
and audit trails. Wrap existing tools; do not duplicate subsystems."

The 30 legacy tools registered by ``Backend/tools`` (auto-discovered, some skipped
when optional deps such as ``pyautogui`` are missing) are imported **as specs** —
their implementations are reused untouched, they just gain metadata: category,
owning agent, risk tier, JSON-schema parameters, timeout, reversibility.
"""

from __future__ import annotations

from typing import Any, Callable

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.tools.risk import RISK_ORDER, classify_risk, highest
from Backend.star.tools.spec import (
    ToolCategory,
    ToolSpec,
    agent_for_tool,
    category_for_tool,
    spec_from_function,
)

__all__ = ["StarToolRegistry", "build_tool_registry"]

_log = star_logger("tools")


def _first_line(text: Any) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[0] if lines else ""


class StarToolRegistry:
    """Typed, inspectable catalogue of tools (legacy + STAR 2.0 native)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: StarEventBus | None = None,
        import_legacy: bool = True,
    ) -> None:
        self.settings = settings
        self.bus = bus or StarEventBus()
        self._specs: dict[str, ToolSpec] = {}
        self.stats: dict[str, Any] = {"registered": 0, "replaced": 0, "legacy_imported": 0, "lookups": 0, "misses": 0}
        if import_legacy:
            self.import_legacy()

    # ── registration ──────────────────────────────────────────────────────
    def register(self, spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
        existing = self._specs.get(spec.name)
        if existing is not None and not replace:
            self.stats["replaced"] += 0
            raise ValueError(f"tool '{spec.name}' is already registered (pass replace=True)")
        if existing is not None:
            self.stats["replaced"] += 1
        self._specs[spec.name] = spec
        self.stats["registered"] += 1
        self.bus.emit(
            "tool.registered",
            phase=EventPhase.TOOL,
            tool=spec.name,
            category=spec.category.value,
            agent=spec.agent.value,
            risk=spec.risk,
            origin=spec.origin,
            callable=spec.callable,
        )
        return spec

    def register_function(
        self, func: Callable[..., Any] | None = None, **kwargs: Any
    ) -> Callable[..., Any] | ToolSpec:
        """Decorator: ``@registry.register_function(risk="low")``."""

        def wrap(target: Callable[..., Any]) -> Callable[..., Any]:
            self.register(spec_from_function(target, **kwargs), replace=bool(kwargs.get("replace", False)))
            return target

        if func is not None:
            return wrap(func)
        return wrap

    def unregister(self, name: str) -> bool:
        return self._specs.pop(name, None) is not None

    def import_legacy(self) -> int:
        """Wrap ``Backend.tools.registry`` entries as typed specs (no re-implementation)."""
        try:
            from Backend.tools.registry import get_all_tools

            legacy = get_all_tools()
        except Exception as exc:  # noqa: BLE001 — optional deps may be missing
            _log.warning("legacy tool registry unavailable: %s", exc)
            return 0

        imported = 0
        for name, entry in dict(legacy).items():
            if name in self._specs:
                continue
            handler = entry.get("func")
            if not callable(handler):
                continue
            spec = ToolSpec(
                name=str(name),
                description=_first_line(entry.get("description")),
                category=category_for_tool(str(name)),
                agent=agent_for_tool(str(name)),
                risk=classify_risk(str(name)),
                parameters=dict(entry.get("parameters") or {"type": "object", "properties": {}, "required": []}),
                handler=handler,
                origin="legacy",
                module=getattr(handler, "__module__", "") or "",
                reversible=str(name) not in {"lock_workstation", "execute_autonomous_goal"},
                idempotent=str(name).startswith(("get_", "see_", "recall_", "calculate")),
            )
            self._specs[spec.name] = spec
            imported += 1
        self.stats["legacy_imported"] += imported
        self.stats["registered"] += imported
        if imported:
            self.bus.emit(
                "tool.registered",
                phase=EventPhase.TOOL,
                tool="*",
                origin="legacy",
                count=imported,
                note="legacy Backend.tools registry wrapped as typed specs",
            )
            _log.info("imported %d legacy tools as typed specs", imported)
        return imported

    # ── lookup ────────────────────────────────────────────────────────────
    def get(self, name: str) -> ToolSpec | None:
        self.stats["lookups"] += 1
        spec = self._specs.get(str(name or "").strip())
        if spec is None:
            self.stats["misses"] += 1
        return spec

    def has(self, name: str) -> bool:
        return str(name or "").strip() in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def risk_of(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        """Risk for the planner: the registered spec wins, else the keyword estimate."""
        spec = self._specs.get(str(tool or "").strip())
        if spec is not None:
            return spec.risk
        return classify_risk(tool, arguments)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> list[dict[str, Any]]:
        """Public tool list — this is what ``GET /api/v1/tools`` serves."""
        return [spec.public() for spec in sorted(self._specs.values(), key=lambda item: item.name)]

    def list(
        self,
        *,
        category: str | ToolCategory | None = None,
        agent: str | None = None,
        risk: str | None = None,
        origin: str | None = None,
    ) -> list[dict[str, Any]]:
        wanted_category = category.value if isinstance(category, ToolCategory) else category
        out: list[dict[str, Any]] = []
        for spec in sorted(self._specs.values(), key=lambda item: item.name):
            if wanted_category and spec.category.value != wanted_category:
                continue
            if agent and spec.agent.value != agent:
                continue
            if risk and spec.risk != risk:
                continue
            if origin and spec.origin != origin:
                continue
            out.append(spec.public())
        return out

    def schemas(self) -> list[dict[str, Any]]:
        """Function-calling schemas for the LLM providers."""
        return [spec.schema() for spec in sorted(self._specs.values(), key=lambda item: item.name)]

    def by_category(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for spec in self._specs.values():
            grouped.setdefault(spec.category.value, []).append(spec.name)
        return {key: sorted(value) for key, value in sorted(grouped.items())}

    def summary(self) -> dict[str, Any]:
        risks = [spec.risk for spec in self._specs.values()]
        return {
            "tools": len(self._specs),
            "categories": {key: len(value) for key, value in self.by_category().items()},
            "agents": sorted({spec.agent.value for spec in self._specs.values()}),
            "risks": {tier: risks.count(tier) for tier in RISK_ORDER if risks.count(tier)},
            "highest_risk": highest(risks, default="none"),
            "origins": {
                origin: len([spec for spec in self._specs.values() if spec.origin == origin])
                for origin in sorted({spec.origin for spec in self._specs.values()})
            },
            "without_handler": sorted(spec.name for spec in self._specs.values() if not spec.callable),
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        missing = [spec.name for spec in self._specs.values() if not spec.callable]
        status = "ok" if self._specs and not missing else "degraded" if self._specs else "pending"
        return {
            "status": status,
            "detail": {
                "tools": len(self._specs),
                "legacy_imported": self.stats["legacy_imported"],
                "missing_handlers": missing[:10],
            },
        }


def build_tool_registry(settings: Settings | None = None, *, bus: StarEventBus | None = None, **kwargs: Any) -> StarToolRegistry:
    """Factory used by the composition root."""
    return StarToolRegistry(settings, bus=bus, **kwargs)
