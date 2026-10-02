"""Phase 9 validator — the gates a candidate must clear before Star may learn it.

Blueprint §9: *"Safe learning loop: interaction → observation → feedback →
candidate pattern → validation → memory update → future retrieval. Do not permit
uncontrolled self-modification of executable code or model weights."* The
architecture doc names the gates explicitly: **frequency ≥ N, success rate ≥
threshold, risk ≤ MEDIUM, no executable code, no secret material.**

Every gate is conservative and *explainable*: a candidate that fails carries the
reason, so the console can show "rejected: success rate 0.50 < 0.80" instead of a
silent no. The gates run in a fixed order and **all** must pass.

The two content gates (``no_code``, ``no_secret``) are the structural guarantee
against self-modification. A candidate only ever holds a ``before → after`` tool
pair or a ``key = value`` preference, so there is nowhere to put code or weights;
these gates additionally refuse anything that *smells* like an attempt to smuggle
either through a tool name, a preference value or the evidence text.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from Backend.star.config.settings import Settings
from Backend.star.learning.schemas import Candidate, CandidateKind, ValidationResult
from Backend.star.observability.logging import is_secret_key, redact
from Backend.star.observability.logging import star_logger
from Backend.star.tools.risk import RISK_ORDER, classify_risk, highest, risk_at_least, risk_rank

__all__ = ["CandidateValidator", "GATE_ORDER"]

_log = star_logger("star2.learning.validator")

#: fixed evaluation order — also the order reasons are reported in
GATE_ORDER: tuple[str, ...] = (
    "frequency",
    "success_rate",
    "risk",
    "no_code",
    "no_secret",
    "approval",
)

#: Anything that looks like an attempt to carry executable code / a weight update
#: through a candidate field. Tool names are dotted or snake_case and preference
#: values are short habits, so none of these should ever appear legitimately.
_CODE_SMELL: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(import|exec|eval|compile|__import__|subprocess|os\.system|popen)\b"),
    re.compile(r"(?i)\b(lambda|globals|locals|setattr|getattr|delattr|globals\(\))\b\s*\("),
    re.compile(r"(?i)\.(py|so|dll|exe|bin|pt|pth|onnx|ckpt|safetensors|gguf)\b"),
    re.compile(r"(?i)\b(model[_\s-]?weights?|state[_\s-]?dict|load[_\s-]?state|checkpoint|fine[_\s-]?tune)\b"),
    re.compile(r"(?i)\b(rm\s+-rf|mkfs|dd\s+if=|:\(\)\s*\{|fork\s*bomb|shutdown|reboot)\b"),
    re.compile(r"[;|`$]|\$\(|&&|\|\||\bcurl\b|\bwget\b"),       # shell metacharacters / pipes
    re.compile(r"def\s+\w+\s*\(|class\s+\w+\s*[:(]"),           # python definitions
    re.compile(r"<\s*script|javascript:|on\w+\s*=", re.I),      # injected script
)

#: a tool name is dotted/snake_case; a preference key is a short slug
_VALID_NAME = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")


class CandidateValidator:
    """Decides whether a :class:`Candidate` is safe to learn. Pure and side-effect free.

    The validator never writes anything and never promotes — it only answers
    "does this clear every gate, and if not, why?". The loop acts on the answer.
    """

    def __init__(self, settings: Settings, *, risk_of: Callable[[str, dict[str, Any]], str] | None = None) -> None:
        self.settings = settings
        cfg = settings.learning
        self.min_frequency = int(cfg.min_frequency)
        self.min_success_rate = float(cfg.min_success_rate)
        self.max_risk = str(cfg.max_risk).lower()
        self.require_approval = bool(cfg.require_approval_for_preferences)
        # the registry's classifier when wired; otherwise the keyword default
        self._risk_of = risk_of

    # ── risk ──────────────────────────────────────────────────────────────
    def _risk(self, tool: str) -> str:
        if not tool:
            return "low"
        classifier = self._risk_of or classify_risk
        try:
            risk = str(classifier(tool, {})).lower()
        except Exception:  # noqa: BLE001 — a bad classifier must never block learning
            _log.warning("risk classifier failed for %s", tool)
            return "high"
        return risk if risk in RISK_ORDER else "medium"

    def candidate_risk(self, candidate: Candidate) -> str:
        """Highest risk among the tools a candidate would touch."""
        if candidate.kind is CandidateKind.PATTERN:
            return highest([self._risk(candidate.before), self._risk(candidate.after)], default="low")
        # a preference writes a key/value into the preference layer (a medium 'set_')
        return self._risk("memory_preference_set")

    # ── content gates ─────────────────────────────────────────────────────
    @staticmethod
    def _scannable_text(candidate: Candidate) -> str:
        parts = [
            candidate.before,
            candidate.after,
            candidate.key,
            candidate.value,
            candidate.signature,
            *candidate.evidence,
        ]
        return " ".join(str(part) for part in parts if part)

    def _has_code_smell(self, candidate: Candidate) -> str | None:
        text = self._scannable_text(candidate)
        for pattern in _CODE_SMELL:
            if pattern.search(text):
                return f"looks like executable code / a weight update ({pattern.pattern[:32]}…)"
        # tool names and preference keys must be plain slugs, never a payload
        for name in (candidate.before, candidate.after, candidate.key):
            if name and not _VALID_NAME.match(name):
                return f"name {name[:24]!r} is not a plain tool/key slug"
        return None

    def _has_secret(self, candidate: Candidate) -> str | None:
        if is_secret_key(candidate.key):
            return f"key {candidate.key[:24]!r} names a credential"
        text = self._scannable_text(candidate)
        if redact(text) != text:
            return "value/evidence contains credential-shaped material"
        return None

    # ── the gates ─────────────────────────────────────────────────────────
    def validate(self, candidate: Candidate) -> ValidationResult:
        """Run every gate. ``ok`` is True only when all of them pass.

        The gates are **kind-aware**. A *pattern* is learned from repetition and
        outcomes, so it needs ``min_frequency`` sightings and a ``min_success_rate``
        of successful runs. A *preference* is a user-approved habit, not an inference
        from repetition: one explicit statement is enough to count, and its "success
        rate" is the balance of approvals over rejections. The ``approval`` gate is
        what makes a preference safe — it can never promote without a human yes.
        """
        gates: dict[str, bool] = {}
        reasons: list[str] = []
        is_pref = candidate.kind is CandidateKind.PREFERENCE
        risk = self.candidate_risk(candidate)

        # effective evidence + success, per kind
        if is_pref:
            decided = candidate.approvals + candidate.rejections
            rate = round(candidate.approvals / decided, 4) if decided else 0.0
            frequency, min_frequency = candidate.observations, 1
        else:
            rate = candidate.success_rate
            frequency, min_frequency = candidate.observations, self.min_frequency

        # 1. frequency — enough evidence to be a pattern, not a one-off
        enough = frequency >= min_frequency
        gates["frequency"] = enough
        if not enough:
            reasons.append(f"frequency {frequency} < {min_frequency}")

        # 2. success rate — Star only copies what actually worked (patterns) /
        #    what the user approved more than rejected (preferences)
        rate_ok = rate >= self.min_success_rate and (
            (candidate.approvals + candidate.rejections) > 0 if is_pref
            else (candidate.successes + candidate.failures) > 0
        )
        gates["success_rate"] = rate_ok
        if not rate_ok:
            reasons.append(f"success rate {rate:.2f} < {self.min_success_rate:.2f}")

        # 3. risk — never learn a pattern that reaches above the ceiling
        risk_ok = not risk_at_least(risk, _tier_above(self.max_risk))
        gates["risk"] = risk_ok
        if not risk_ok:
            reasons.append(f"risk {risk} exceeds ceiling {self.max_risk}")

        # 4. no executable code / weight update — structural anti-self-modification
        code_smell = self._has_code_smell(candidate)
        gates["no_code"] = code_smell is None
        if code_smell is not None:
            reasons.append(f"no_code: {code_smell}")

        # 5. no secret material
        secret = self._has_secret(candidate)
        gates["no_secret"] = secret is None
        if secret is not None:
            reasons.append(f"no_secret: {secret}")

        # 6. approval — a preference is a *user-approved* habit, never an inference
        if is_pref and self.require_approval:
            approved = candidate.approvals > 0 and candidate.rejections == 0
            gates["approval"] = approved
            if not approved:
                reasons.append("approval: a preference needs explicit positive feedback and no rejection")
        else:
            gates["approval"] = True       # patterns learn from outcomes, not approvals

        ok = all(gates.values())
        # explicit rejection outranks everything: the user said stop
        if candidate.rejections > 0:
            ok = False
            if not any("rejected by user" in reason for reason in reasons):
                reasons.append(f"rejected by user ({candidate.rejections}×)")
            gates["approval"] = False

        return ValidationResult(
            candidate_id=candidate.candidate_id,
            ok=ok,
            gates=gates,
            reasons=reasons,
            risk=risk,
            success_rate=rate,
            observations=candidate.observations,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "gates": list(GATE_ORDER),
            "min_frequency": self.min_frequency,
            "min_success_rate": self.min_success_rate,
            "max_risk": self.max_risk,
            "require_approval_for_preferences": self.require_approval,
            "anti_self_modification": True,
        }


def _tier_above(tier: str) -> str:
    """The tier just above ``tier`` in :data:`RISK_ORDER` (used for the risk ceiling).

    ``risk_at_least(risk, above)`` is then True only when ``risk`` strictly exceeds
    the ceiling, so ``max_risk='medium'`` rejects high/critical but keeps medium.
    """
    rank = risk_rank(tier)
    if rank + 1 < len(RISK_ORDER):
        return RISK_ORDER[rank + 1]
    return RISK_ORDER[-1]
