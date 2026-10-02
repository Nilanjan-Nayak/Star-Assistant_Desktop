"""Phase 9 candidate store — the loop's working set of *potential* patterns.

This is **not** a second memory store. It is the learning loop's own scratch
ledger: the place where raw observations accumulate into :class:`Candidate`
records that the validator has not yet cleared. Only when a candidate is
*promoted* does anything reach a real store (the brain's ``PatternStore`` or the
shared ``MemoryStore``), so this file is a staging area, not long-term memory.

Persistence is append-only JSONL at ``settings.paths.candidates_path``
(``data/learning/candidates.jsonl``), bounded by ``max_candidates``. Each line is
a full candidate snapshot; on load the newest snapshot per ``candidate_id`` wins
(the same last-write-wins replay ``PatternStore`` uses), so the file can be
tailed, audited and rebuilt without a database.

The store is deliberately dumb about *safety* — it only counts evidence. Deciding
whether a candidate may be learned is :class:`~Backend.star.learning.validator.
CandidateValidator`'s job, and writing it anywhere real is the loop's. Keeping
those three apart is what makes the "no uncontrolled self-modification" rule
auditable: you can read the ledger, read the gate, and read the promotion target
independently.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from Backend.star.learning.schemas import (
    Candidate,
    CandidateKind,
    CandidateState,
    new_candidate_id,
)
from Backend.star.observability.logging import star_logger

__all__ = ["CandidateStore", "pattern_signature", "preference_signature"]

_log = star_logger("star2.learning.patterns")


def pattern_signature(before: str, after: str) -> str:
    return f"{before}\u2192{after}"


def preference_signature(key: str, value: str) -> str:
    return f"{key}={value}"


class CandidateStore:
    """Accumulates observations into bounded, persisted candidates."""

    def __init__(self, path: Path | str, *, max_candidates: int = 500, max_evidence: int = 6) -> None:
        self.path = Path(path)
        self.max_candidates = int(max_candidates)
        self.max_evidence = int(max_evidence)
        self._candidates: dict[str, Candidate] = {}
        self._by_signature: dict[str, str] = {}        # signature → candidate_id
        self._loaded = False
        self.stats = {"observed": 0, "created": 0, "evicted": 0, "persist_failures": 0}

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
                    data = json.loads(line)
                    candidate = Candidate.model_validate(data)
                except Exception:  # noqa: BLE001 — a corrupt line is skipped, not fatal
                    continue
                self._candidates[candidate.candidate_id] = candidate
                if candidate.signature:
                    self._by_signature[candidate.signature] = candidate.candidate_id
        except OSError as exc:
            _log.warning("could not read candidate store %s: %s", self.path, exc)
        self._evict()

    def _persist(self, candidate: Candidate) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False) + "\n")
        except OSError as exc:
            self.stats["persist_failures"] += 1
            _log.warning("could not persist candidate: %s", exc)

    def _evict(self) -> None:
        """Keep the working set bounded: drop the oldest terminal candidates first."""
        if len(self._candidates) <= self.max_candidates:
            return
        terminal = [c for c in self._candidates.values() if c.is_terminal]
        terminal.sort(key=lambda c: c.last_seen)
        for candidate in terminal[: len(self._candidates) - self.max_candidates]:
            self._drop(candidate.candidate_id)
        # if still over (nothing terminal), drop the least-recently-seen
        while len(self._candidates) > self.max_candidates:
            oldest = min(self._candidates.values(), key=lambda c: c.last_seen)
            self._drop(oldest.candidate_id)
            self.stats["evicted"] += 1

    def _drop(self, candidate_id: str) -> None:
        candidate = self._candidates.pop(candidate_id, None)
        if candidate is not None and candidate.signature:
            if self._by_signature.get(candidate.signature) == candidate_id:
                self._by_signature.pop(candidate.signature, None)
        self.stats["evicted"] += 1

    # ── observation ───────────────────────────────────────────────────────
    def observe_pattern(
        self,
        before: str,
        after: str,
        *,
        succeeded: bool | None = True,
        risk: str = "unknown",
        source: str = "run",
        session_id: str = "",
        evidence: str = "",
    ) -> Candidate | None:
        """Record one ``before → after`` occurrence; create the candidate on first sight.

        ``succeeded=None`` records the *sighting* without an outcome signal — used for
        dry-run rehearsals, which prove intent but not that the workflow really works,
        so they must never push a candidate over the success-rate gate on their own.
        A terminal candidate (promoted / rejected) keeps its state: it is not reopened
        by a later sighting, so a validated pattern is promoted once and a pattern the
        user rejected stays rejected.
        """
        self._load()
        before, after = str(before or "").strip(), str(after or "").strip()
        if not before or not after or before == after:
            return None
        signature = pattern_signature(before, after)
        candidate = self._get_by_signature(signature)
        now = datetime.now(timezone.utc)
        if candidate is None:
            candidate = Candidate(
                candidate_id=new_candidate_id(),
                kind=CandidateKind.PATTERN,
                signature=signature,
                before=before,
                after=after,
                risk=risk,
                source=source,
                session_id=session_id,
                first_seen=now,
                last_seen=now,
            )
            self._candidates[candidate.candidate_id] = candidate
            self._by_signature[signature] = candidate.candidate_id
            self.stats["created"] += 1
        candidate.observations += 1
        if succeeded is True:
            candidate.successes += 1
        elif succeeded is False:
            candidate.failures += 1
        # succeeded is None ⇒ observation only, no outcome recorded
        candidate.risk = risk if risk != "unknown" else candidate.risk
        candidate.last_seen = now
        if session_id:
            candidate.session_id = session_id
        if evidence:
            candidate.evidence = (candidate.evidence + [evidence[:160]])[-self.max_evidence :]
        self.stats["observed"] += 1
        self._persist(candidate)
        self._evict()
        return candidate

    def observe_preference(
        self,
        key: str,
        value: str,
        *,
        source: str = "feedback",
        session_id: str = "",
        evidence: str = "",
    ) -> Candidate | None:
        """Record a candidate ``key = value`` habit. Approvals are added by feedback."""
        self._load()
        key, value = str(key or "").strip(), str(value or "").strip()
        if not key or not value:
            return None
        signature = preference_signature(key, value)
        candidate = self._get_by_signature(signature)
        now = datetime.now(timezone.utc)
        if candidate is None:
            candidate = Candidate(
                candidate_id=new_candidate_id(),
                kind=CandidateKind.PREFERENCE,
                signature=signature,
                key=key[:80],
                value=value[:400],
                risk="medium",
                source=source,
                session_id=session_id,
                first_seen=now,
                last_seen=now,
            )
            self._candidates[candidate.candidate_id] = candidate
            self._by_signature[signature] = candidate.candidate_id
            self.stats["created"] += 1
        candidate.observations += 1
        candidate.last_seen = now
        if evidence:
            candidate.evidence = (candidate.evidence + [evidence[:160]])[-self.max_evidence :]
        self.stats["observed"] += 1
        self._persist(candidate)
        self._evict()
        return candidate

    # ── feedback effects ──────────────────────────────────────────────────
    def apply_approval(self, candidate_id: str) -> Candidate | None:
        return self._bump(candidate_id, "approvals")

    def apply_rejection(self, candidate_id: str) -> Candidate | None:
        candidate = self._bump(candidate_id, "rejections")
        if candidate is not None:
            candidate.state = CandidateState.REJECTED
            candidate.reasons = [*(r for r in candidate.reasons if "rejected by user" not in r)]
            candidate.reasons.append("rejected by user")
            self._persist(candidate)
        return candidate

    def _bump(self, candidate_id: str, field: str) -> Candidate | None:
        self._load()
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            return None
        setattr(candidate, field, getattr(candidate, field) + 1)
        candidate.last_seen = datetime.now(timezone.utc)
        self._persist(candidate)
        return candidate

    # ── state transitions ─────────────────────────────────────────────────
    def set_state(self, candidate_id: str, state: CandidateState, *, reasons: list[str] | None = None) -> Candidate | None:
        self._load()
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            return None
        candidate.state = state
        if reasons is not None:
            candidate.reasons = list(reasons)
        candidate.last_seen = datetime.now(timezone.utc)
        self._persist(candidate)
        return candidate

    def mark_promoted(self, candidate_id: str, *, promoted_to: str) -> Candidate | None:
        self._load()
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            return None
        candidate.state = CandidateState.PROMOTED
        candidate.promoted_at = datetime.now(timezone.utc)
        candidate.promoted_to = promoted_to
        candidate.last_seen = candidate.promoted_at
        self._persist(candidate)
        return candidate

    # ── reads ─────────────────────────────────────────────────────────────
    def _get_by_signature(self, signature: str) -> Candidate | None:
        candidate_id = self._by_signature.get(signature)
        return self._candidates.get(candidate_id) if candidate_id else None

    def get(self, candidate_id: str) -> Candidate | None:
        self._load()
        return self._candidates.get(candidate_id)

    def find_by_signature(self, signature: str) -> Candidate | None:
        self._load()
        return self._get_by_signature(signature)

    def all(self, *, kind: CandidateKind | str | None = None, state: CandidateState | str | None = None) -> list[Candidate]:
        self._load()
        items: Iterable[Candidate] = self._candidates.values()
        if kind is not None:
            kind_value = kind.value if isinstance(kind, CandidateKind) else str(kind)
            items = [c for c in items if c.kind.value == kind_value]
        if state is not None:
            state_value = state.value if isinstance(state, CandidateState) else str(state)
            items = [c for c in items if c.state.value == state_value]
        return sorted(items, key=lambda c: c.last_seen, reverse=True)

    def observing(self) -> list[Candidate]:
        return self.all(state=CandidateState.OBSERVING)

    def count(self) -> int:
        self._load()
        return len(self._candidates)

    def counts_by_state(self) -> dict[str, int]:
        self._load()
        out = {state.value: 0 for state in CandidateState}
        for candidate in self._candidates.values():
            out[candidate.state.value] += 1
        return out

    def snapshot(self, *, limit: int = 50) -> dict[str, Any]:
        self._load()
        return {
            "count": len(self._candidates),
            "by_state": self.counts_by_state(),
            "max_candidates": self.max_candidates,
            "candidates": [c.public() for c in self.all()[:limit]],
            **self.stats,
        }
