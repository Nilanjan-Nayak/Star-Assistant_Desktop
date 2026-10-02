"""Prediction — "what will the user probably want next?" (suggestions only).

Blueprint §7 Phase 3: "Add simple next-action prediction from history" and
Phase 9: "Learn only safe patterns. Predictions never bypass confirmation."

Hard rules enforced here:
  * a :class:`Prediction` is **never** executed — ``executed`` is always ``False``
    and the orchestrator refuses to run one without turning it into a normal task
    that passes policy (Phase 10/11);
  * only *safe* tools (risk below the confirmation threshold) may be predicted;
  * patterns are learned from what Star actually did, stored as append-only JSONL
    at ``settings.paths.patterns_path`` (``data/patterns.jsonl``).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from Backend.star.brain.schemas import Context, Plan, Prediction, UserRequest
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger

__all__ = ["HistoryPredictor", "NullPredictor", "PatternStore", "Predictor", "SAFE_SEED_PATTERNS"]

_log = star_logger("prediction")

#: Conservative seed patterns (all low/medium risk, all reversible).
SAFE_SEED_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("play_music", "set_volume", "after starting music you often adjust the volume"),
    ("youtube_play_last", "set_volume", "after starting a video you often adjust the volume"),
    ("launch_application", "see_screen", "after opening an app Star usually checks what is on screen"),
    ("take_screenshot", "see_screen", "a screenshot is usually followed by reading it"),
    ("set_brightness", "get_brightness", "brightness changes are usually confirmed"),
    ("set_volume", "get_volume", "volume changes are usually confirmed"),
    ("remember_fact", "recall_memory", "what Star remembers is often recalled later"),
)


class PatternStore:
    """Append-only ``tool_a → tool_b`` counter persisted as JSONL."""

    def __init__(self, path: Path | str, *, max_pairs: int = 4000) -> None:
        self.path = Path(path)
        self.max_pairs = max_pairs
        self._counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                first, second = str(entry.get("a") or ""), str(entry.get("b") or "")
                count = int(entry.get("count") or 1)
                if first and second:
                    self._counts[first][second] += count
        except OSError as exc:
            _log.warning("could not read pattern store %s: %s", self.path, exc)

    def successors(self, tool: str) -> dict[str, int]:
        self._load()
        return dict(self._counts.get(tool, {}))

    def record(self, sequence: Iterable[str]) -> int:
        """Learn from an ordered tool sequence; returns how many pairs were stored."""
        self._load()
        tools = [str(t) for t in sequence if t]
        added = 0
        lines: list[str] = []
        for first, second in zip(tools, tools[1:]):
            if first == second:
                continue
            self._counts[first][second] += 1
            added += 1
            lines.append(
                json.dumps(
                    {
                        "a": first,
                        "b": second,
                        "count": 1,
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                )
            )
        if lines:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
            except OSError as exc:
                _log.warning("could not persist patterns: %s", exc)
        return added

    def total(self) -> int:
        self._load()
        return sum(len(successors) for successors in self._counts.values())

    def snapshot(self) -> dict[str, dict[str, int]]:
        self._load()
        return {tool: dict(successors) for tool, successors in self._counts.items()}


@runtime_checkable
class Predictor(Protocol):
    name: str

    def available(self) -> bool: ...

    async def predict(self, request: UserRequest, context: Context, plan: Plan) -> list[Prediction]: ...

    def learn(self, plan: Plan) -> int: ...

    def learn_bridge(self, previous_tool: str, next_tool: str) -> int: ...


class NullPredictor:
    name = "null"

    def available(self) -> bool:
        return True

    async def predict(self, request: UserRequest, context: Context, plan: Plan) -> list[Prediction]:
        return []

    def learn(self, plan: Plan) -> int:
        return 0

    def learn_bridge(self, previous_tool: str, next_tool: str) -> int:
        return 0


class HistoryPredictor:
    """Seed patterns + learned co-occurrence, filtered to safe tools only."""

    name = "history"

    def __init__(
        self,
        settings: Settings,
        *,
        store: PatternStore | None = None,
        bus: StarEventBus | None = None,
        risk_of=None,
        limit: int = 3,
        min_confidence: float = 0.25,
    ) -> None:
        self.settings = settings
        self.store = store if store is not None else PatternStore(settings.paths.patterns_path)
        self.bus = bus or StarEventBus()
        self._risk_of = risk_of
        self.limit = limit
        self.min_confidence = min_confidence
        self.stats = {"predictions": 0, "learned_pairs": 0, "unsafe_filtered": 0}

    # ── safety gate ───────────────────────────────────────────────────────
    def _risk(self, tool: str) -> str:
        if self._risk_of is None:
            from Backend.star.brain.planning import default_risk

            self._risk_of = default_risk
        try:
            return str(self._risk_of(tool, {}))
        except Exception:  # noqa: BLE001
            return "high"

    def _is_safe(self, tool: str) -> bool:
        from Backend.star.brain.planning import risk_at_least

        risk = self._risk(tool)
        if risk_at_least(risk, self.settings.security.confirm_above_risk):
            return False
        if risk_at_least(risk, self.settings.security.deny_risk):
            return False
        deny = {name.lower() for name in self.settings.security.tool_denylist}
        return tool.lower() not in deny

    # ── prediction ────────────────────────────────────────────────────────
    async def predict(self, request: UserRequest, context: Context, plan: Plan) -> list[Prediction]:
        if not self.settings.brain.prediction_enabled:
            return []
        last_tools = [call.tool for task in plan.tasks for call in task.steps]
        if not last_tools:
            return []
        scored: dict[str, tuple[float, str, str]] = {}
        for tool in last_tools:
            for seed_from, seed_to, reason in SAFE_SEED_PATTERNS:
                if seed_from == tool and self._is_safe(seed_to):
                    current = scored.get(seed_to)
                    if current is None or current[0] < 0.35:
                        scored[seed_to] = (0.35, reason, "seed")
            for successor, count in self.store.successors(tool).items():
                if not self._is_safe(successor):
                    self.stats["unsafe_filtered"] += 1
                    continue
                confidence = round(count / (count + 2.0), 3)      # Laplace-smoothed
                if confidence < self.min_confidence:
                    continue
                current = scored.get(successor)
                if current is None or current[0] < confidence:
                    scored[successor] = (confidence, f"seen together {count}×", "history")

        ordered = sorted(scored.items(), key=lambda item: item[1][0], reverse=True)[: self.limit]
        predictions = [
            Prediction(
                tool=tool,
                arguments={},
                confidence=round(min(0.95, confidence), 3),
                reason=reason,
                source=source,
                executed=False,
            )
            for tool, (confidence, reason, source) in ordered
        ]
        self.stats["predictions"] += len(predictions)
        if predictions:
            self.bus.emit(
                "prediction.proposed",
                phase=EventPhase.BRAIN,
                request_id=request.request_id,
                session_id=request.session_id,
                predictions=[p.model_dump(mode="json") for p in predictions],
                note="suggestions only — never executed without policy",
            )
        return predictions

    # ── learning ──────────────────────────────────────────────────────────
    def learn(self, plan: Plan) -> int:
        """Record which tools actually ran together in a finished plan."""
        if not self.settings.brain.prediction_enabled:
            return 0
        sequence = [
            call.tool
            for task in plan.tasks
            for call in task.steps
            if call.state.value in ("done", "skipped")
        ]
        added = self.store.record(sequence)
        self.stats["learned_pairs"] += added
        return added

    def learn_bridge(self, previous_tool: str, next_tool: str) -> int:
        """Learn the *cross-turn* pair: last tool of turn N → first tool of turn N+1.

        Only called for turns that actually succeeded, so Star never copies its own
        mistakes (blueprint Phase 9: learn safe patterns only).
        """
        if not self.settings.brain.prediction_enabled:
            return 0
        if not previous_tool or not next_tool or previous_tool == next_tool:
            return 0
        added = self.store.record([previous_tool, next_tool])
        self.stats["learned_pairs"] += added
        return added

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.brain.prediction_enabled,
            "patterns": self.store.total(),
            "limit": self.limit,
            "min_confidence": self.min_confidence,
            **self.stats,
        }
