"""Learning tools — the agent-facing surface of the Phase 9 safe learning loop.

Four tools, registered in the Phase 4 registry, so they inherit parameter
validation, the permission ladder, the audit trail and dry-run simulation. They
add only what nothing else offers:

* visibility into **what Star is learning and why** (``learning_status``,
  ``learning_list_candidates``) — every candidate carries its evidence, its gate
  results and its state, so learning is never a black box;
* a way to give **explicit feedback** (``learning_feedback``) — the human half of
  the loop, and the only thing that can approve a preference;
* a way to run **one validation + promotion sweep** on demand (``learning_cycle``).

What these tools deliberately **cannot** do: force an unvalidated candidate into a
store. ``learning_cycle`` runs the same gates as the automatic loop and only
promotes what clears every one, so there is no tool-shaped hole in the "learn only
safe patterns" rule. And nothing here can write code or a model weight — the loop
promotes data into two existing stores and nowhere else (blueprint §9: "Do not
permit uncontrolled self-modification of executable code or model weights").

Risk ladder (``highest(spec.risk, classify_risk(name, args))`` decides the tier):

* **low** — ``learning_status`` / ``learning_list_candidates``. Their names carry
  ``status`` / ``list_`` so the keyword classifier agrees they change nothing.
* **medium** — ``learning_feedback`` (records a signal, may seed a preference) and
  ``learning_cycle`` (writes *validated* data into the pattern/preference stores).
  Neither reaches the OS, so neither needs a confirmation; both are audited.

Handlers are **sync**: the Phase 4 executor runs them with ``asyncio.to_thread``,
and every :class:`~Backend.star.learning.loop.LearningLoop` method is sync and
bounded (the candidate ledger and ``PatternStore`` are sync; preference writes go
through the manager's ``set_preference_now`` twin).
"""

from __future__ import annotations

from typing import Any

from Backend.star.brain.schemas import AgentName
from Backend.star.config.settings import Settings
from Backend.star.learning.loop import LearningLoop
from Backend.star.learning.schemas import CandidateKind, CandidateState, FeedbackSignal
from Backend.star.observability.logging import star_logger
from Backend.star.tools.spec import ToolCategory, ToolSpec

__all__ = ["LearningToolkit", "register_learning_tools"]

_log = star_logger("star2.learning.tools")

_TAGS: tuple[str, ...] = ("learning", "phase9", "safe-loop")
_TIMEOUT = 20.0


class LearningToolkit:
    """Thin, honest wrapper the tool handlers share.

    Every method returns a plain dict with ``ok``, a one-line ``output`` the brain
    can say out loud, and structured data. Nothing raises: a missing loop or a dead
    store becomes ``ok=False`` with the reason, because a learning problem must
    never take down a reply.
    """

    def __init__(self, loop: LearningLoop | None, settings: Settings | None = None) -> None:
        self.loop = loop
        self.settings = settings or (loop.settings if loop is not None else Settings())
        self.stats: dict[str, int] = {"calls": 0, "ok": 0, "refused": 0, "no_loop": 0, "empty": 0}

    # ── helpers ───────────────────────────────────────────────────────────
    def _missing(self, tool: str) -> dict[str, Any]:
        self.stats["calls"] += 1
        self.stats["no_loop"] += 1
        return {
            "ok": False,
            "tool": tool,
            "error": "the safe learning loop is not wired in this build",
            "output": "Learning is not available right now.",
        }

    def _done(self, tool: str, payload: dict[str, Any], output: str) -> dict[str, Any]:
        self.stats["calls"] += 1
        ok = bool(payload.get("ok", True))
        self.stats["ok" if ok else "refused"] += 1
        return {"ok": ok, "tool": tool, "output": output, **payload}

    @staticmethod
    def _bound(limit: Any, default: int, ceiling: int) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, ceiling))

    # ── reads ─────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        if self.loop is None:
            return self._missing("learning_status")
        snap = self.loop.snapshot(limit=0)
        gates = snap.get("gates", {})
        targets = snap.get("targets", {})
        by_state = (snap.get("candidates") or {}).get("by_state", {})
        payload = {
            "enabled": snap.get("enabled", False),
            "auto_promote": snap.get("auto_promote", False),
            "gates": gates,
            "promotion_targets": targets,
            "candidates_by_state": by_state,
            "stats": snap.get("stats", {}),
            "last_cycle": snap.get("last_cycle", {}),
            "last_promotion": snap.get("last_promotion"),
            "no_self_modification": snap.get("no_self_modification", True),
            "note": (
                "learning only ever writes validated data into the existing pattern/preference "
                "stores — it can never write executable code or model weights"
            ),
        }
        if not snap.get("enabled", False):
            return self._done("learning_status", payload, "Learning is disabled (STAR_LEARNING_ENABLED=false).")
        promoted = by_state.get("promoted", 0)
        observing = by_state.get("observing", 0)
        return self._done(
            "learning_status",
            payload,
            f"Learning is on: {observing} candidate(s) observing, {promoted} promoted. "
            f"Gates: frequency≥{gates.get('min_frequency')}, success≥{gates.get('min_success_rate')}, "
            f"risk≤{gates.get('max_risk')}, no code, no secrets.",
        )

    def list_candidates(self, kind: str = "", state: str = "", limit: int = 30) -> dict[str, Any]:
        if self.loop is None:
            return self._missing("learning_list_candidates")
        if not self.loop.available():
            return self._done(
                "learning_list_candidates",
                {"count": 0, "candidates": [], "enabled": False},
                "Learning is disabled — there are no candidates.",
            )
        top = self._bound(limit, 30, 200)
        kind_filter: Any = None
        if str(kind or "").strip():
            try:
                kind_filter = CandidateKind(str(kind).strip().lower())
            except ValueError:
                kind_filter = str(kind).strip().lower()
        state_filter: Any = None
        if str(state or "").strip():
            try:
                state_filter = CandidateState(str(state).strip().lower())
            except ValueError:
                state_filter = str(state).strip().lower()
        items = self.loop.candidates.all(kind=kind_filter, state=state_filter)[:top]
        rows = [c.public() for c in items]
        if not rows:
            self.stats["empty"] += 1
        return self._done(
            "learning_list_candidates",
            {
                "count": len(rows),
                "candidates": rows,
                "total": self.loop.candidates.count(),
                "by_state": self.loop.candidates.counts_by_state(),
                "note": "a candidate is inert until it clears every gate; promoted ones are in a real store",
            },
            f"{len(rows)} candidate(s)"
            + (f" of {self.loop.candidates.count()} total" if self.loop.candidates.count() != len(rows) else "")
            + ("." if rows else " yet — Star learns from finished runs and your feedback."),
        )

    # ── writes ────────────────────────────────────────────────────────────
    def feedback(
        self,
        signal: str,
        subject: str = "",
        candidate_id: str = "",
        key: str = "",
        value: str = "",
        note: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        if self.loop is None:
            return self._missing("learning_feedback")
        sig = str(signal or "").strip().lower()
        if sig not in {s.value for s in FeedbackSignal}:
            self.stats["calls"] += 1
            self.stats["refused"] += 1
            return {
                "ok": False,
                "tool": "learning_feedback",
                "error": f"signal must be one of positive/negative/correction (got {signal!r})",
                "output": "I need to know if that's positive, negative or a correction.",
            }
        result = self.loop.record_feedback(
            sig,
            subject=str(subject or ""),
            candidate_id=str(candidate_id or ""),
            note=str(note or ""),
            session_id=str(session_id or ""),
            key=str(key or ""),
            value=str(value or ""),
        )
        if not result.get("ok"):
            return self._done("learning_feedback", result, result.get("error", "Feedback was not recorded."))
        cycle = result.get("cycle") or {}
        said = f"Thanks — I noted that ({sig})."
        if cycle.get("promoted"):
            said = f"Thanks — I learned {cycle['promoted']} thing(s) from that and stored them."
        elif result.get("preference"):
            said = f"Got it — I'll remember {key or result['preference'].get('key')} = " \
                   f"{value or result['preference'].get('value')}."
        return self._done("learning_feedback", result, said)

    def cycle(self, candidate_id: str = "", promote: bool = True, limit: int = 0) -> dict[str, Any]:
        """Run one validation (+ promotion) sweep. Never bypasses a gate."""
        if self.loop is None:
            return self._missing("learning_cycle")
        if not self.loop.available():
            return self._done(
                "learning_cycle",
                {"ok": False, "error": "learning is disabled", "considered": 0, "promoted": 0},
                "Learning is disabled (STAR_LEARNING_ENABLED=false) — nothing to validate.",
            )
        cid = str(candidate_id or "").strip()
        if cid:
            candidate = self.loop.candidates.get(cid)
            if candidate is None:
                self.stats["calls"] += 1
                self.stats["refused"] += 1
                return {
                    "ok": False, "tool": "learning_cycle", "error": f"no candidate {cid!r}",
                    "output": "I couldn't find that candidate.",
                }
            result = self.loop.validate_candidate(candidate)
            promoted = None
            if result.ok and promote:
                promoted = self.loop.promote(candidate)
            payload = {
                "considered": 1,
                "validated": int(result.ok),
                "rejected": int(candidate.state is CandidateState.REJECTED),
                "promoted": int(bool(promoted and promoted.ok)),
                "candidate": candidate.public(),
                "gates": result.gates,
                "reasons": result.reasons,
                "promotion": promoted.model_dump(mode="json") if promoted else None,
            }
            if result.ok and promoted and promoted.ok:
                said = f"{candidate.signature} cleared every gate — promoted to {promoted.promoted_to}."
            elif result.ok:
                said = f"{candidate.signature} is validated but was not promoted (auto/flag off)."
            else:
                said = f"{candidate.signature} did not clear the gates: {'; '.join(result.reasons) or 'not enough evidence'}."
            return self._done("learning_cycle", payload, said)

        summary = self.loop.cycle(promote=promote, limit=(self._bound(limit, 0, 500) if limit else None))
        considered = summary.get("considered", 0)
        promoted = summary.get("promoted", 0)
        validated = summary.get("validated", 0)
        rejected = summary.get("rejected", 0)
        if considered == 0:
            self.stats["empty"] += 1
        return self._done(
            "learning_cycle",
            summary,
            f"Learning sweep: considered {considered}, validated {validated}, rejected {rejected}, "
            f"promoted {promoted}." if considered else "Nothing to validate yet — no observing candidates.",
        )


def register_learning_tools(
    registry: Any,
    settings: Settings | None = None,
    *,
    bus: Any = None,
    loop: LearningLoop | None = None,
    toolkit: LearningToolkit | None = None,
) -> LearningToolkit:
    """Add the four learning tools to a :class:`StarToolRegistry`; return the toolkit.

    ``loop=None`` is allowed (the tools then answer honestly that learning is not
    wired) so the registry surface stays complete in every build.
    """
    cfg = settings or (loop.settings if loop is not None else Settings())
    kit = toolkit or LearningToolkit(loop, cfg)
    if loop is not None:
        kit.loop = loop
    common: dict[str, Any] = {
        "origin": "star2",
        "module": "Backend.star.learning.tools",
        "agent": AgentName.CONVERSATION,
        "category": ToolCategory.LEARNING,
    }

    specs = (
        ToolSpec(
            name="learning_status",
            description=(
                "Report the safe learning loop: the validation gates, how many candidates are "
                "observing/validated/promoted/rejected, the promotion targets, and counters. Read-only."
            ),
            risk="low",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: kit.status(),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=(*_TAGS, "meta"), **common,
        ),
        ToolSpec(
            name="learning_list_candidates",
            description=(
                "List candidate patterns/preferences Star is considering, with their evidence, "
                "gate results and state. Filter by kind (pattern/preference) or state. Read-only."
            ),
            risk="low",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["", "pattern", "preference"], "default": ""},
                    "state": {
                        "type": "string",
                        "enum": ["", "observing", "validated", "promoted", "rejected"],
                        "default": "",
                    },
                    "limit": {"type": "integer", "default": 30, "description": "max candidates"},
                },
                "required": [],
            },
            handler=lambda kind="", state="", limit=30: kit.list_candidates(kind, state, limit),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=_TAGS, **common,
        ),
        ToolSpec(
            name="learning_feedback",
            description=(
                "Record explicit user feedback — the human half of the learning loop. Signal is "
                "positive/negative/correction. Optionally state a preference (key/value) to seed a "
                "user-approved habit; positive feedback is the only thing that can approve a preference."
            ),
            risk="medium",
            parameters={
                "type": "object",
                "properties": {
                    "signal": {"type": "string", "enum": ["positive", "negative", "correction"]},
                    "subject": {"type": "string", "default": "", "description": "candidate signature or tool name"},
                    "candidate_id": {"type": "string", "default": ""},
                    "key": {"type": "string", "default": "", "description": "preference key, e.g. reply_language"},
                    "value": {"type": "string", "default": "", "description": "preference value, e.g. bn"},
                    "note": {"type": "string", "default": ""},
                },
                "required": ["signal"],
            },
            handler=lambda signal, subject="", candidate_id="", key="", value="", note="", session_id="": (
                kit.feedback(signal, subject, candidate_id, key, value, note, session_id)
            ),
            timeout_s=_TIMEOUT, idempotent=False, reversible=True, dry_run_safe=True,
            tags=_TAGS, **common,
        ),
        ToolSpec(
            name="learning_cycle",
            description=(
                "Run one validation (+ promotion) sweep over observing candidates, or validate/promote a "
                "single candidate by id. Only candidates that clear EVERY gate are promoted; this never "
                "bypasses the safety gates and never writes code or model weights."
            ),
            risk="medium",
            parameters={
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "default": "", "description": "empty = sweep all observing"},
                    "promote": {"type": "boolean", "default": True, "description": "write validated candidates to stores"},
                    "limit": {"type": "integer", "default": 0, "description": "max candidates to consider (0 = all)"},
                },
                "required": [],
            },
            handler=lambda candidate_id="", promote=True, limit=0: kit.cycle(candidate_id, promote, limit),
            timeout_s=_TIMEOUT, idempotent=True, reversible=True, dry_run_safe=True,
            tags=_TAGS, **common,
        ),
    )

    for spec in specs:
        registry.register(spec, replace=True)
    _log.info("star2.learning.tools.registered count=%d loop=%s", len(specs), loop is not None)
    return kit
