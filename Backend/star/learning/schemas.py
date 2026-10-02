"""Phase 9 schemas — the typed vocabulary of the safe learning loop.

Blueprint §9: ``interaction → observation → feedback → candidate pattern →
validation → memory update → future retrieval``, with the hard rule *"Do not
permit uncontrolled self-modification of executable code or model weights."*

These models are deliberately small and JSON-serialisable so the gateway, the
console and the event stream can all show exactly what Star is learning, what it
has validated, what it promoted and **why** — learning that cannot be explained
is learning that cannot be trusted.

Two candidate kinds, and only two:

* ``pattern``    — a procedural ``before → after`` tool co-occurrence (promotes into
  the brain's :class:`~Backend.star.brain.prediction.PatternStore`, which the Phase 8
  procedural layer already reads);
* ``preference`` — a user-approved ``key = value`` habit (promotes into the shared
  :class:`~Backend.star.memory.manager.MemoryManager` preference layer).

There is **no** candidate kind that carries code, a file path to write, a shell
command or a model weight. That is structural: the loop can only ever promote
data into two existing stores, so "no self-modification of executable code or
model weights" is enforced by the shape of the schema, not by a runtime check
alone (the validator adds a belt-and-braces content scan on top).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Candidate",
    "CandidateKind",
    "CandidateState",
    "Feedback",
    "FeedbackSignal",
    "Promotion",
    "ValidationResult",
    "new_candidate_id",
    "new_feedback_id",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_candidate_id() -> str:
    from agent.core.ids import new_id

    return new_id("cand")


def new_feedback_id() -> str:
    from agent.core.ids import new_id

    return new_id("fb")


@enum.unique
class CandidateKind(str, enum.Enum):
    """What a candidate can become. Closed on purpose — see module docs."""

    PATTERN = "pattern"
    PREFERENCE = "preference"


@enum.unique
class CandidateState(str, enum.Enum):
    """Where a candidate is in the loop."""

    OBSERVING = "observing"      # still accumulating evidence
    VALIDATED = "validated"      # cleared every gate, eligible to promote
    PROMOTED = "promoted"        # written into a real store
    REJECTED = "rejected"        # failed a gate (reason recorded)


@enum.unique
class FeedbackSignal(str, enum.Enum):
    """Explicit user feedback — the only thing that can approve a preference."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    CORRECTION = "correction"


class Feedback(BaseModel):
    """One explicit feedback event from the user.

    Feedback is the *human* half of the loop. Implicit evidence (did the run
    succeed?) is counted on the :class:`Candidate` itself; this record is the
    user saying "yes, keep doing that" / "no, stop" / "actually, do this instead".
    """

    model_config = ConfigDict(extra="forbid")

    feedback_id: str = Field(default_factory=new_feedback_id)
    signal: FeedbackSignal = FeedbackSignal.POSITIVE
    subject: str = ""                       # candidate signature, tool name, or free text
    candidate_id: str = ""                  # resolved target, when it maps to a candidate
    note: str = ""
    session_id: str = ""
    ts: datetime = Field(default_factory=_utcnow)


class Candidate(BaseModel):
    """A pattern or preference Star is *considering*, with the evidence for it.

    A candidate is inert: nothing here can run, and nothing is written to a real
    store until :class:`~Backend.star.learning.validator.CandidateValidator` clears
    every gate and the loop promotes it.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(default_factory=new_candidate_id)
    kind: CandidateKind = CandidateKind.PATTERN
    signature: str = ""                     # "before→after" (pattern) or "key=value" (preference)

    # pattern fields
    before: str = ""
    after: str = ""

    # preference fields
    key: str = ""
    value: str = ""

    # evidence
    observations: int = Field(default=0, ge=0)
    successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    approvals: int = Field(default=0, ge=0)         # explicit positive feedback
    rejections: int = Field(default=0, ge=0)        # explicit negative feedback
    risk: str = "unknown"                           # highest risk among the tools involved
    source: str = "run"                             # run | chat | feedback | tool
    evidence: list[str] = Field(default_factory=list)   # bounded, human-readable

    state: CandidateState = CandidateState.OBSERVING
    reasons: list[str] = Field(default_factory=list)    # why it is validated / rejected
    session_id: str = ""
    first_seen: datetime = Field(default_factory=_utcnow)
    last_seen: datetime = Field(default_factory=_utcnow)
    promoted_at: datetime | None = None
    promoted_to: str = ""                       # which store accepted it

    @property
    def success_rate(self) -> float:
        decided = self.successes + self.failures
        return round(self.successes / decided, 4) if decided else 0.0

    @property
    def is_terminal(self) -> bool:
        return self.state in (CandidateState.PROMOTED, CandidateState.REJECTED)

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["success_rate"] = self.success_rate
        data["is_terminal"] = self.is_terminal
        return data


class ValidationResult(BaseModel):
    """The outcome of running every gate over one candidate — pass *and* fail reasons."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    ok: bool = False
    gates: dict[str, bool] = Field(default_factory=dict)     # gate name → passed
    reasons: list[str] = Field(default_factory=list)         # human-readable, in gate order
    risk: str = "unknown"
    success_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    observations: int = Field(default=0, ge=0)


class Promotion(BaseModel):
    """A validated candidate written into a real store (the loop's 'memory update')."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    kind: CandidateKind = CandidateKind.PATTERN
    signature: str = ""
    ok: bool = False
    promoted_to: str = ""                       # "pattern_store" | "preference_layer"
    detail: str = ""
    ts: datetime = Field(default_factory=_utcnow)
