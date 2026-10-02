"""Checkpoint persistence for Phase 10 orchestration.

Stores snapshots of plan states into an append-only JSONL ledger and
in-memory index, enabling pause, resume, and rollback capabilities.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.orchestrator.schemas import CheckpointStatus, PlanCheckpoint

_log = star_logger("orchestrator.checkpoint")


class CheckpointStore:
    """Append-only JSONL checkpoint store with in-memory fast indexing."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_records: int = 200,
        bus: StarEventBus | None = None,
    ) -> None:
        self.path = Path(path)
        self.max_records = max(10, max_records)
        self.bus = bus
        self._by_id: dict[str, PlanCheckpoint] = {}
        self._by_plan: dict[str, list[PlanCheckpoint]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                        chk = PlanCheckpoint.model_validate(data)
                        self._index(chk)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning("star2.orchestrator.checkpoint.corrupt_line error=%s", exc)
        except Exception as exc:  # noqa: BLE001
            _log.warning("star2.orchestrator.checkpoint.load_failed path=%s error=%s", self.path, exc)

    def _index(self, checkpoint: PlanCheckpoint) -> None:
        self._by_id[checkpoint.checkpoint_id] = checkpoint
        plan_list = self._by_plan.setdefault(checkpoint.plan_id, [])
        # keep chronological
        plan_list.append(checkpoint)

    def save(self, checkpoint: PlanCheckpoint) -> PlanCheckpoint:
        """Persist a checkpoint and index it."""
        self._index(checkpoint)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(checkpoint.model_dump(mode="json")) + "\n")
        except Exception as exc:  # noqa: BLE001
            _log.error("star2.orchestrator.checkpoint.save_failed error=%s", exc)

        if self.bus is not None:
            self.bus.emit(
                "plan.checkpoint",
                phase=EventPhase.PLAN,
                plan_id=checkpoint.plan_id,
                checkpoint_id=checkpoint.checkpoint_id,
                status=checkpoint.status.value,
                completed=len(checkpoint.completed_tasks),
                pending=len(checkpoint.pending_tasks),
            )
        return checkpoint

    def get(self, checkpoint_id: str) -> PlanCheckpoint | None:
        return self._by_id.get(checkpoint_id)

    def latest(self, plan_id: str) -> PlanCheckpoint | None:
        plan_list = self._by_plan.get(plan_id)
        return plan_list[-1] if plan_list else None

    def list_for_plan(self, plan_id: str) -> list[PlanCheckpoint]:
        return list(self._by_plan.get(plan_id, []))

    def list_all(self, limit: int = 50) -> list[PlanCheckpoint]:
        all_chks = list(self._by_id.values())
        return all_chks[-limit:]

    def count(self) -> int:
        return len(self._by_id)

    def prune(self, max_records: int | None = None) -> int:
        """Keep only the most recent N checkpoints."""
        limit = max_records or self.max_records
        if len(self._by_id) <= limit:
            return 0
        all_chks = list(self._by_id.values())
        keep = all_chks[-limit:]
        removed = len(all_chks) - len(keep)
        self._by_id = {c.checkpoint_id: c for c in keep}
        self._by_plan.clear()
        for c in keep:
            self._by_plan.setdefault(c.plan_id, []).append(c)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                for c in keep:
                    fh.write(json.dumps(c.model_dump(mode="json")) + "\n")
        except Exception as exc:  # noqa: BLE001
            _log.warning("star2.orchestrator.checkpoint.prune_failed error=%s", exc)
        return removed

    def close(self) -> None:
        """Flushes and cleans up store."""
        pass
