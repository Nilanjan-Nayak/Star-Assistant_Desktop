"""Append-only action journal for audit + deterministic replay."""

from __future__ import annotations

import json
from pathlib import Path

from agent.motor.result import ActionResult
from agent.motor.spec import ActionSpec, parse_action


class ActionJournal:
    def __init__(self, path: Path = Path("journal.jsonl")) -> None:
        self.path = path
        self._entries: list[tuple[ActionSpec, ActionResult]] = []

    def record(self, spec: ActionSpec, result: ActionResult) -> None:
        self._entries.append((spec, result))
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "spec": spec.model_dump(mode="json"),
                            "result": result.model_dump(mode="json"),
                        },
                        default=str,
                    )
                    + "\n"
                )
        except OSError:
            return

    def load(self) -> list[tuple[ActionSpec, ActionResult]]:
        if not self.path.exists():
            return []
        loaded: list[tuple[ActionSpec, ActionResult]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            loaded.append(
                (parse_action(payload["spec"]), ActionResult.model_validate(payload["result"]))
            )
        return loaded

    @property
    def entries(self) -> tuple[tuple[ActionSpec, ActionResult], ...]:
        return tuple(self._entries)
