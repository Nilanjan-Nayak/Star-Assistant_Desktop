"""STAR 2.0 learning package (Phase 9) — the safe learning loop.

Blueprint §9: ``interaction → observation → feedback → candidate pattern →
validation → memory update → future retrieval``, with the hard rule *"Do not
permit uncontrolled self-modification of executable code or model weights"* and
*"A prediction must still pass the normal policy/tool boundary."*

Nothing here owns long-term memory. The loop keeps a **candidate ledger** and a
**feedback log** (both staging, both append-only JSONL under ``data/learning/``),
validates candidates against the blueprint's gates, and promotes only the ones
that clear every gate into stores that already exist:

* :mod:`.schemas`   — ``Candidate`` / ``Feedback`` / ``ValidationResult`` / ``Promotion``
  and the closed ``CandidateKind`` (only ``pattern`` and ``preference`` — there is
  no kind that can carry code or a weight).
* :mod:`.patterns`  — ``CandidateStore``: accumulates observations into bounded,
  persisted candidates (the loop's working set, not a memory store).
* :mod:`.feedback`  — ``FeedbackLog``: the explicit, human half of the loop; the
  only signal that can approve a preference.
* :mod:`.validator` — ``CandidateValidator``: the gates (frequency, success rate,
  risk ceiling, no executable code, no secret material, preference approval).
* :mod:`.loop`      — ``LearningLoop``: the conductor (``observe_*`` → ``record_feedback``
  → ``cycle``/``promote``), publishing ``learning.*`` events.
* :mod:`.tools`     — four typed Phase 4 tools that expose the loop without ever
  offering a way to bypass a gate.

Exports are lazy (PEP 562) so ``import Backend.star.learning`` stays cheap.
"""

from __future__ import annotations

__all__ = [
    "Candidate",
    "CandidateKind",
    "CandidateState",
    "CandidateStore",
    "CandidateValidator",
    "Feedback",
    "FeedbackLog",
    "FeedbackSignal",
    "GATE_ORDER",
    "LearningLoop",
    "LearningToolkit",
    "Promotion",
    "ValidationResult",
    "build_learning",
    "register_learning_tools",
]

_LAZY = {
    "Candidate": ("Backend.star.learning.schemas", "Candidate"),
    "CandidateKind": ("Backend.star.learning.schemas", "CandidateKind"),
    "CandidateState": ("Backend.star.learning.schemas", "CandidateState"),
    "Feedback": ("Backend.star.learning.schemas", "Feedback"),
    "FeedbackSignal": ("Backend.star.learning.schemas", "FeedbackSignal"),
    "Promotion": ("Backend.star.learning.schemas", "Promotion"),
    "ValidationResult": ("Backend.star.learning.schemas", "ValidationResult"),
    "new_candidate_id": ("Backend.star.learning.schemas", "new_candidate_id"),
    "new_feedback_id": ("Backend.star.learning.schemas", "new_feedback_id"),
    "CandidateStore": ("Backend.star.learning.patterns", "CandidateStore"),
    "pattern_signature": ("Backend.star.learning.patterns", "pattern_signature"),
    "preference_signature": ("Backend.star.learning.patterns", "preference_signature"),
    "FeedbackLog": ("Backend.star.learning.feedback", "FeedbackLog"),
    "CandidateValidator": ("Backend.star.learning.validator", "CandidateValidator"),
    "GATE_ORDER": ("Backend.star.learning.validator", "GATE_ORDER"),
    "LearningLoop": ("Backend.star.learning.loop", "LearningLoop"),
    "build_learning": ("Backend.star.learning.loop", "build_learning"),
    "LearningToolkit": ("Backend.star.learning.tools", "LearningToolkit"),
    "register_learning_tools": ("Backend.star.learning.tools", "register_learning_tools"),
}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        module_name, attribute = _LAZY[name]
        return getattr(importlib.import_module(module_name), attribute)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
