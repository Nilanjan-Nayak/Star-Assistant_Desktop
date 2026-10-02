"""Phase 9 — the safe learning loop.

Blueprint §9: ``interaction → observation → feedback → candidate pattern →
validation → memory update → future retrieval``, with *"Do not permit
uncontrolled self-modification of executable code or model weights"* and *"A
prediction must still pass the normal policy/tool boundary."*

:class:`LearningLoop` is the conductor. It owns no long-term memory of its own —
it keeps a **candidate ledger** (staging) and a **feedback log**, and the only
two places it can ever *write* on promotion are stores that already exist:

* a validated **pattern** reinforces the brain's :class:`~Backend.star.brain.prediction.PatternStore`
  (the procedural store the Phase 8 layer reads), and
* a validated **preference** is written through the shared
  :class:`~Backend.star.memory.manager.MemoryManager` into the ``MemoryStore``.

There is no code path from here to a ``.py`` file, a shell, or a model weight, so
"no self-modification" is structural, and the validator's ``no_code`` /
``no_secret`` gates reject any candidate that even tries to smuggle one through a
tool name or a preference value.

Relationship to Phase 3: the brain's :class:`~Backend.star.brain.prediction.HistoryPredictor`
records *raw* co-occurrence every turn (the prediction substrate). This loop is
the *validated* path on top — it only promotes a pattern once it clears frequency,
success-rate, risk, content and (for preferences) explicit-approval gates, and it
is the only thing that learns **preferences from feedback**. Promotion is a
deliberate, gate-cleared reinforcement, recorded once per candidate.

Everything here is **synchronous** and bounded: the candidate ledger and
``PatternStore`` are sync, and preference writes go through the manager's
``set_preference_now`` twin. Callers in the event loop (``main.py``) invoke these
via :func:`asyncio.to_thread`; the Phase 4 executor already threads tool handlers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterable, Sequence

from Backend.star.config.settings import Settings
from Backend.star.learning.feedback import FeedbackLog
from Backend.star.learning.patterns import CandidateStore
from Backend.star.learning.schemas import (
    Candidate,
    CandidateKind,
    CandidateState,
    FeedbackSignal,
    Promotion,
    ValidationResult,
)
from Backend.star.learning.validator import CandidateValidator
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger
from Backend.star.tools.risk import classify_risk, highest

__all__ = ["LearningLoop", "build_learning"]

_log = star_logger("star2.learning.loop")


class LearningLoop:
    """Observes finished work, validates candidate patterns/preferences, promotes the safe ones."""

    name = "learning"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: StarEventBus | None = None,
        candidates: CandidateStore | None = None,
        feedback: FeedbackLog | None = None,
        validator: CandidateValidator | None = None,
        patterns: Any = None,
        memory: Any = None,
        risk_of: Callable[[str, dict[str, Any]], str] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.bus = bus or StarEventBus()
        cfg = self.settings.learning
        self.enabled = bool(cfg.enabled)
        self._risk_of = risk_of or classify_risk

        #: a disabled loop touches no disk and learns nothing — that is what the flag means
        self.candidates = candidates if candidates is not None else (
            CandidateStore(self.settings.paths.candidates_path, max_candidates=cfg.max_candidates)
            if self.enabled else None
        )
        self.feedback_log = feedback if feedback is not None else (
            FeedbackLog(self.settings.paths.feedback_path) if self.enabled else None
        )
        self.validator = validator or CandidateValidator(self.settings, risk_of=self._risk_of)

        #: promotion targets, attached once the brain/memory exist (see build_application)
        self.patterns = patterns
        self.memory = memory

        self.auto_promote = bool(cfg.auto_promote)
        self.max_promotions_per_cycle = int(cfg.max_promotions_per_cycle)
        self.stats: dict[str, int] = {
            "observed": 0,
            "patterns_observed": 0,
            "preferences_observed": 0,
            "feedback": 0,
            "validated": 0,
            "rejected": 0,
            "promoted": 0,
            "promote_failures": 0,
            "cycles": 0,
        }
        self.last_cycle: dict[str, Any] = {}
        self.last_promotion: Promotion | None = None
        self._started = False
        self._closed = False

    # ── late attachment: promote into the stores the app already built ──────
    def attach_patterns(self, patterns: Any) -> bool:
        """Borrow the brain's live ``PatternStore`` — never open a second one."""
        if patterns is None:
            return False
        self.patterns = patterns
        return True

    def attach_memory(self, memory: Any) -> bool:
        """Borrow the shared ``MemoryManager`` so preferences land in the one store."""
        if memory is None:
            return False
        self.memory = memory
        return True

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def startup(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        _log.info(
            "star2.learning.ready enabled=%s auto_promote=%s candidates=%s patterns=%s memory=%s",
            self.enabled, self.auto_promote,
            self.candidates is not None, self.patterns is not None, self.memory is not None,
        )
        self._emit(
            "learning.observed",
            stage="startup",
            enabled=self.enabled,
            auto_promote=self.auto_promote,
            pattern_store=self.patterns is not None,
            memory_manager=self.memory is not None,
        )

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        """Nothing to release: the ledger and log are append-only files, the stores are borrowed."""
        self._closed = True

    # ── risk helper ─────────────────────────────────────────────────────────
    def _risk(self, tool: str) -> str:
        if not tool:
            return "low"
        try:
            return str(self._risk_of(tool, {})).lower()
        except Exception:  # noqa: BLE001
            return "high"

    # ── observation (interaction → observation → candidate) ─────────────────
    def observe_sequence(
        self,
        tools: Sequence[str],
        *,
        succeeded: bool | None,
        session_id: str = "",
        source: str = "run",
        dry_run: bool = False,
        evidence: str = "",
    ) -> list[Candidate]:
        """Record every adjacent ``before → after`` pair in a finished tool sequence.

        ``succeeded=None`` (a dry-run rehearsal) records the sighting without an
        outcome, so a simulation can never push a pattern over the success gate.
        """
        if not self.enabled or self.candidates is None:
            return []
        clean = [str(t).strip() for t in tools if str(t).strip()]
        if len(clean) < 2:
            return []
        # a dry-run never claims success: withhold the outcome signal entirely
        outcome: bool | None = None if dry_run else succeeded
        touched: list[Candidate] = []
        for before, after in zip(clean, clean[1:]):
            if before == after:
                continue
            risk = highest([self._risk(before), self._risk(after)], default="low")
            candidate = self.candidates.observe_pattern(
                before, after,
                succeeded=outcome, risk=risk, source=source,
                session_id=session_id, evidence=evidence,
            )
            if candidate is not None:
                touched.append(candidate)
        if touched:
            self.stats["observed"] += 1
            self.stats["patterns_observed"] += len(touched)
            self._emit(
                "learning.observed",
                session_id=session_id,
                stage="observation",
                pairs=[c.signature for c in touched],
                succeeded=succeeded,
                dry_run=dry_run,
                source=source,
            )
            if self.auto_promote:
                self._validate_and_promote(touched, session_id=session_id)
        return touched

    def observe_run(self, run: Any, *, session_id: str = "", agent: str = "") -> list[Candidate]:
        """Observe a finished browser/computer ``AgentRun`` (the same steps memory records)."""
        if not self.enabled or run is None:
            return []
        tools = [
            str(getattr(step, "tool", "") or getattr(step, "action", "") or "")
            for step in getattr(run, "steps", [])
            if not getattr(step, "skipped", False)
        ]
        return self.observe_sequence(
            tools,
            succeeded=bool(getattr(run, "succeeded", False)),
            session_id=session_id or str(getattr(run, "session_id", "") or ""),
            source=f"{agent or 'agent'}_run",
            dry_run=bool(getattr(run, "dry_run", False)),
            evidence=str(getattr(run, "goal", "") or "")[:160],
        )

    def observe_chat(self, result: dict[str, Any], *, session_id: str = "") -> list[Candidate]:
        """Observe a finished brain turn from its result dict (tasks → tool sequence)."""
        if not self.enabled or not isinstance(result, dict):
            return []
        tools: list[str] = []
        for task in result.get("tasks") or (result.get("plan") or {}).get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for call in task.get("steps") or []:
                if isinstance(call, dict) and call.get("tool"):
                    tools.append(str(call["tool"]))
        if len(tools) < 2:
            return []
        reflection = result.get("reflection") if isinstance(result.get("reflection"), dict) else {}
        verdict = str(reflection.get("verdict") or "")
        succeeded = bool(result.get("ok", True)) and verdict not in ("failed", "blocked")
        return self.observe_sequence(
            tools,
            succeeded=succeeded,
            session_id=session_id or str(result.get("session_id") or ""),
            source="chat",
            dry_run=bool(result.get("dry_run", False)) or bool(result.get("simulated", False)),
            evidence=str(result.get("intent") or "")[:160],
        )

    # ── feedback (the human half) ───────────────────────────────────────────
    def record_feedback(
        self,
        signal: FeedbackSignal | str,
        *,
        subject: str = "",
        candidate_id: str = "",
        note: str = "",
        session_id: str = "",
        key: str = "",
        value: str = "",
    ) -> dict[str, Any]:
        """Record explicit feedback; a positive preference statement seeds a candidate.

        ``key``/``value`` let the user *state* a habit ("reply_language = bn"); a
        positive signal on it is the explicit approval the validator requires before
        a preference may be promoted.
        """
        if not self.enabled or self.feedback_log is None or self.candidates is None:
            return {"ok": False, "error": "learning is disabled (STAR_LEARNING_ENABLED=false)"}
        try:
            sig = signal if isinstance(signal, FeedbackSignal) else FeedbackSignal(str(signal).lower())
        except ValueError:
            return {"ok": False, "error": f"unknown feedback signal {signal!r}"}

        target: Candidate | None = None
        if candidate_id:
            target = self.candidates.get(candidate_id)
        elif subject:
            target = self.candidates.find_by_signature(subject)

        # a stated preference becomes (or updates) a preference candidate
        pref: Candidate | None = None
        if key and value and sig in (FeedbackSignal.POSITIVE, FeedbackSignal.CORRECTION):
            pref = self.candidates.observe_preference(
                key, value, source="feedback", session_id=session_id, evidence=note or subject
            )
            if pref is not None:
                self.stats["preferences_observed"] += 1
                target = target or pref

        record = self.feedback_log.record(
            sig,
            subject=subject or (target.signature if target else ""),
            candidate_id=target.candidate_id if target else "",
            note=note,
            session_id=session_id,
        )
        self.stats["feedback"] += 1

        # apply the signal once per distinct candidate (``target`` may *be* ``pref``)
        approved_ids: set[str] = set()
        if target is not None:
            if sig is FeedbackSignal.POSITIVE:
                approved_ids.add(target.candidate_id)
                if pref is not None:
                    approved_ids.add(pref.candidate_id)
            elif sig is FeedbackSignal.NEGATIVE:
                self.candidates.apply_rejection(target.candidate_id)
            elif sig is FeedbackSignal.CORRECTION:
                approved_ids.add(target.candidate_id)
                if pref is not None:
                    approved_ids.add(pref.candidate_id)
        for cid in approved_ids:
            self.candidates.apply_approval(cid)

        self._emit(
            "learning.feedback",
            session_id=session_id,
            stage="feedback",
            signal=sig.value,
            candidate_id=target.candidate_id if target else "",
            subject=record.subject,
        )
        result: dict[str, Any] = {
            "ok": True,
            "feedback_id": record.feedback_id,
            "signal": sig.value,
            "candidate_id": target.candidate_id if target else "",
            "preference": pref.public() if pref is not None else None,
        }
        if self.auto_promote and target is not None:
            result["cycle"] = self._validate_and_promote([target], session_id=session_id)
        return result

    # ── validation + promotion (candidate → validation → memory update) ─────
    def validate_candidate(self, candidate: Candidate) -> ValidationResult:
        """Run the gates over one candidate and update its state (no promotion here)."""
        result = self.validator.validate(candidate)
        if result.ok:
            self.candidates.set_state(
                candidate.candidate_id, CandidateState.VALIDATED, reasons=result.reasons or ["cleared every gate"]
            )
            candidate.state = CandidateState.VALIDATED
        else:
            # a hard content/risk/approval failure is terminal; mere low evidence is not
            hard = any(not result.gates.get(gate, True) for gate in ("no_code", "no_secret", "risk", "approval"))
            if hard:
                self.candidates.set_state(candidate.candidate_id, CandidateState.REJECTED, reasons=result.reasons)
                candidate.state = CandidateState.REJECTED
                self.stats["rejected"] += 1
                self._emit("learning.rejected", stage="validation",
                           candidate_id=candidate.candidate_id, signature=candidate.signature,
                           reasons=result.reasons)
            else:
                # not enough evidence yet — stay observing, remember why
                self.candidates.set_state(candidate.candidate_id, CandidateState.OBSERVING, reasons=result.reasons)
                candidate.reasons = result.reasons
        return result

    def promote(self, candidate: Candidate, *, session_id: str = "") -> Promotion:
        """Write a *validated* candidate into the one real store it belongs to."""
        if candidate.state is not CandidateState.VALIDATED:
            return Promotion(
                candidate_id=candidate.candidate_id, kind=candidate.kind, signature=candidate.signature,
                ok=False, detail=f"candidate is {candidate.state.value}, not validated",
            )
        if candidate.kind is CandidateKind.PATTERN:
            promotion = self._promote_pattern(candidate)
        else:
            promotion = self._promote_preference(candidate, session_id=session_id)
        if promotion.ok:
            self.candidates.mark_promoted(candidate.candidate_id, promoted_to=promotion.promoted_to)
            candidate.state = CandidateState.PROMOTED
            candidate.promoted_to = promotion.promoted_to
            self.stats["promoted"] += 1
            self.last_promotion = promotion
            self._emit(
                "learning.promoted",
                session_id=session_id or candidate.session_id,
                stage="memory_update",
                candidate_id=candidate.candidate_id,
                candidate_kind=candidate.kind.value,
                signature=candidate.signature,
                promoted_to=promotion.promoted_to,
            )
            if candidate.kind is CandidateKind.PATTERN:
                # the procedural store's own vocabulary event (declared since Phase 3)
                self._emit("pattern.learned", session_id=session_id or candidate.session_id,
                           stage="memory_update", before=candidate.before, after=candidate.after,
                           signature=candidate.signature, source="learning_loop")
        else:
            self.stats["promote_failures"] += 1
            self._emit("learning.rejected", session_id=session_id or candidate.session_id,
                       stage="promotion", candidate_id=candidate.candidate_id,
                       signature=candidate.signature, reasons=[promotion.detail])
        return promotion

    def _promote_pattern(self, candidate: Candidate) -> Promotion:
        if self.patterns is None:
            return Promotion(candidate_id=candidate.candidate_id, kind=candidate.kind,
                             signature=candidate.signature, ok=False,
                             detail="no pattern store attached (brain not wired)")
        try:
            self.patterns.record([candidate.before, candidate.after])
        except Exception as exc:  # noqa: BLE001
            return Promotion(candidate_id=candidate.candidate_id, kind=candidate.kind,
                             signature=candidate.signature, ok=False, detail=f"pattern record failed: {exc}"[:200])
        return Promotion(candidate_id=candidate.candidate_id, kind=candidate.kind,
                         signature=candidate.signature, ok=True, promoted_to="pattern_store",
                         detail=f"reinforced {candidate.signature} in the procedural store")

    def _promote_preference(self, candidate: Candidate, *, session_id: str = "") -> Promotion:
        if self.memory is None:
            return Promotion(candidate_id=candidate.candidate_id, kind=candidate.kind,
                             signature=candidate.signature, ok=False,
                             detail="no memory manager attached (memory not wired)")
        try:
            outcome = self.memory.set_preference_now(
                candidate.key, candidate.value, session_id=session_id or candidate.session_id
            )
        except Exception as exc:  # noqa: BLE001
            return Promotion(candidate_id=candidate.candidate_id, kind=candidate.kind,
                             signature=candidate.signature, ok=False, detail=f"preference write failed: {exc}"[:200])
        if not isinstance(outcome, dict) or not outcome.get("ok"):
            detail = str((outcome or {}).get("error") or "preference write refused")[:200]
            return Promotion(candidate_id=candidate.candidate_id, kind=candidate.kind,
                             signature=candidate.signature, ok=False, detail=detail)
        return Promotion(candidate_id=candidate.candidate_id, kind=candidate.kind,
                         signature=candidate.signature, ok=True, promoted_to="preference_layer",
                         detail=f"stored preference {candidate.signature}")

    def _validate_and_promote(self, candidates: Iterable[Candidate], *, session_id: str = "") -> dict[str, Any]:
        """Validate the given candidates and promote the ones that clear every gate."""
        validated = rejected = promoted = 0
        budget = self.max_promotions_per_cycle
        for candidate in list(candidates):
            if candidate.state is CandidateState.PROMOTED:
                continue
            result = self.validate_candidate(candidate)
            if result.ok:
                validated += 1
                if budget > 0:
                    promotion = self.promote(candidate, session_id=session_id)
                    if promotion.ok:
                        promoted += 1
                        budget -= 1
            elif candidate.state is CandidateState.REJECTED:
                rejected += 1
        summary = {"validated": validated, "rejected": rejected, "promoted": promoted}
        self.last_cycle = summary
        return summary

    def cycle(self, *, promote: bool | None = None, limit: int | None = None) -> dict[str, Any]:
        """Full sweep: validate every observing candidate, promote the validated ones.

        Called on startup, by the ``learning_cycle`` tool/route, and after a batch of
        observations when auto-promote is on. Bounded by ``max_promotions_per_cycle``.
        """
        if not self.enabled or self.candidates is None:
            return {"ok": False, "error": "learning is disabled", **self.stats}
        started = time.perf_counter()
        do_promote = self.auto_promote if promote is None else bool(promote)
        observing = self.candidates.observing()
        if limit is not None:
            observing = observing[: max(0, int(limit))]
        validated = rejected = promoted = 0
        budget = self.max_promotions_per_cycle
        promotions: list[dict[str, Any]] = []
        for candidate in observing:
            result = self.validate_candidate(candidate)
            if result.ok:
                validated += 1
                if do_promote and budget > 0:
                    promotion = self.promote(candidate)
                    if promotion.ok:
                        promoted += 1
                        budget -= 1
                        promotions.append(promotion.model_dump(mode="json"))
            elif candidate.state is CandidateState.REJECTED:
                rejected += 1
        self.stats["cycles"] += 1
        summary = {
            "ok": True,
            "considered": len(observing),
            "validated": validated,
            "rejected": rejected,
            "promoted": promoted,
            "auto_promote": do_promote,
            "promotions": promotions,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }
        self.last_cycle = summary
        self._emit("learning.validated", stage="validation", considered=len(observing),
                   validated=validated, rejected=rejected, promoted=promoted)
        return summary

    # ── views ───────────────────────────────────────────────────────────────
    def snapshot(self, *, limit: int = 50) -> dict[str, Any]:
        """Full, honest view for the gateway/console: what Star is learning and why."""
        if not self.enabled or self.candidates is None:
            return {
                "ok": True, "enabled": False,
                "detail": "learning is disabled (STAR_LEARNING_ENABLED=false) — nothing is observed, "
                          "validated or promoted, and no learning files are touched",
                "gates": self.validator.describe(),
                "stats": dict(self.stats),
            }
        candidates = self.candidates.snapshot(limit=limit)
        feedback = self.feedback_log.snapshot(limit=10) if self.feedback_log is not None else {"count": 0, "recent": []}
        return {
            "ok": True,
            "enabled": True,
            "auto_promote": self.auto_promote,
            "gates": self.validator.describe(),
            "targets": {
                "pattern_store": self.patterns is not None,
                "memory_manager": self.memory is not None,
            },
            "candidates": candidates,
            "feedback": feedback,
            "last_cycle": self.last_cycle,
            "last_promotion": self.last_promotion.model_dump(mode="json") if self.last_promotion else None,
            "stats": dict(self.stats),
            "no_self_modification": True,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_promote": self.auto_promote,
            "candidates": self.candidates.count() if self.candidates is not None else 0,
            "by_state": self.candidates.counts_by_state() if self.candidates is not None else {},
            "pattern_store": self.patterns is not None,
            "memory_manager": self.memory is not None,
            "gates": self.validator.describe(),
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "off", "detail": "learning disabled (STAR_LEARNING_ENABLED=false)"}
        if self.candidates is None:
            return {"status": "degraded", "detail": "candidate ledger unavailable"}
        detail = {
            "candidates": self.candidates.count(),
            "by_state": self.candidates.counts_by_state(),
            "pattern_store": self.patterns is not None,
            "memory_manager": self.memory is not None,
            "auto_promote": self.auto_promote,
            **self.stats,
        }
        # degraded when neither promotion target is attached: it can observe but not learn
        status = "ok" if (self.patterns is not None or self.memory is not None) else "degraded"
        return {"status": status, "detail": detail}

    def available(self) -> bool:
        return self.enabled and self.candidates is not None

    # ── events ──────────────────────────────────────────────────────────────
    def _emit(self, kind: str, *, stage: str, session_id: str = "", **payload: Any) -> None:
        """Publish on the shared bus. Payload keys never shadow event fields."""
        try:
            self.bus.emit(
                kind,
                phase=EventPhase.LEARNING,
                session_id=str(session_id or self.settings.session_id or ""),
                stage=stage,
                **{key: value for key, value in payload.items() if value is not None},
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must never break learning
            _log.warning("learning event %s failed: %s", kind, exc)


def build_learning(
    settings: Settings | None = None,
    *,
    bus: StarEventBus | None = None,
    patterns: Any = None,
    memory: Any = None,
    candidates: CandidateStore | None = None,
    feedback: FeedbackLog | None = None,
    risk_of: Callable[[str, dict[str, Any]], str] | None = None,
) -> LearningLoop:
    """Compose the loop. Pass the stores the app already built — it must not reopen them.

    ``patterns`` is the brain's live :class:`~Backend.star.brain.prediction.PatternStore`
    and ``memory`` the shared :class:`~Backend.star.memory.manager.MemoryManager`; both
    are attached in :func:`Backend.star.main.build_application` once they exist.
    """
    cfg = settings or Settings()
    loop = LearningLoop(
        cfg, bus=bus, candidates=candidates, feedback=feedback,
        patterns=patterns, memory=memory, risk_of=risk_of,
    )
    _log.info(
        "star2.learning.built enabled=%s auto_promote=%s patterns=%s memory=%s",
        loop.enabled, loop.auto_promote, loop.patterns is not None, loop.memory is not None,
    )
    return loop
