"""Phase 9 feedback log — the explicit, human half of the learning loop.

Blueprint §9 puts *feedback* between observation and the candidate pattern:
``interaction → observation → **feedback** → candidate pattern → validation → …``.
Implicit evidence (did the run succeed?) is counted on the candidate itself; this
log is the user *telling* Star something — "yes, keep doing that", "no, stop",
"actually, do this instead". It is the only signal that can approve a
:class:`~Backend.star.learning.schemas.CandidateKind` ``preference``, because a
preference is by definition a **user-approved habit**, never an inference.

Append-only JSONL at ``settings.paths.feedback_path``
(``data/learning/feedback.jsonl``), bounded in memory to the most recent
``max_records``. Like the candidate store it is a log, not a memory store: it
records what the user said so the loop — and the audit trail — can show why a
preference was or was not learned.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Backend.star.learning.schemas import Feedback, FeedbackSignal, new_feedback_id
from Backend.star.observability.logging import redact, star_logger

__all__ = ["FeedbackLog"]

_log = star_logger("star2.learning.feedback")


class FeedbackLog:
    """Append-only log of explicit feedback, newest-last in memory."""

    def __init__(self, path: Path | str, *, max_records: int = 500) -> None:
        self.path = Path(path)
        self.max_records = int(max_records)
        self._records: deque[Feedback] = deque(maxlen=self.max_records)
        self._loaded = False
        self.stats = {"recorded": 0, "positive": 0, "negative": 0, "correction": 0, "persist_failures": 0}

    # ── persistence ───────────────────────────────────────────────────────
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
                    record = Feedback.model_validate(json.loads(line))
                except Exception:  # noqa: BLE001 — skip a corrupt line
                    continue
                self._records.append(record)
        except OSError as exc:
            _log.warning("could not read feedback log %s: %s", self.path, exc)

    def _persist(self, record: Feedback) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
        except OSError as exc:
            self.stats["persist_failures"] += 1
            _log.warning("could not persist feedback: %s", exc)

    # ── write ─────────────────────────────────────────────────────────────
    def record(
        self,
        signal: FeedbackSignal | str,
        *,
        subject: str = "",
        candidate_id: str = "",
        note: str = "",
        session_id: str = "",
    ) -> Feedback:
        """Append one feedback record. Free-text ``note``/``subject`` are redacted."""
        self._load()
        sig = signal if isinstance(signal, FeedbackSignal) else FeedbackSignal(str(signal).lower())
        record = Feedback(
            feedback_id=new_feedback_id(),
            signal=sig,
            subject=redact(str(subject or ""))[:200],
            candidate_id=str(candidate_id or ""),
            note=redact(str(note or ""))[:400],
            session_id=str(session_id or ""),
            ts=datetime.now(timezone.utc),
        )
        self._records.append(record)
        self._persist(record)
        self.stats["recorded"] += 1
        self.stats[{
            FeedbackSignal.POSITIVE: "positive",
            FeedbackSignal.NEGATIVE: "negative",
            FeedbackSignal.CORRECTION: "correction",
        }[sig]] += 1
        return record

    # ── reads ─────────────────────────────────────────────────────────────
    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._load()
        items = list(self._records)[-limit:]
        return [r.model_dump(mode="json") for r in reversed(items)]

    def for_candidate(self, candidate_id: str) -> list[Feedback]:
        self._load()
        return [r for r in self._records if r.candidate_id == candidate_id]

    def count(self) -> int:
        self._load()
        return len(self._records)

    def snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        self._load()
        return {"count": len(self._records), "recent": self.recent(limit=limit), **self.stats}
