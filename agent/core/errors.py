"""Typed exception hierarchy. Every error carries structured context."""

from __future__ import annotations

from typing import Final


class AgentError(Exception):
    """Base for all agent errors. ``context`` is JSON-serialisable metadata."""

    def __init__(self, message: str, /, **context: object) -> None:
        super().__init__(message)
        self.message: str = message
        self.context: dict[str, object] = dict(context)

    def to_dict(self) -> dict[str, object]:
        return {
            "error": type(self).__name__,
            "message": self.message,
            **self.context,
        }

    def __str__(self) -> str:
        if not self.context:
            return self.message
        extras = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"{self.message} ({extras})"


class PerceptionError(AgentError):
    """Could not see or identify UI."""


class MotorError(AgentError):
    """Physical action failed."""


class SafetyViolation(AgentError):
    """Blocked by governor."""


class BudgetExhausted(SafetyViolation):
    """Action/time budget consumed."""


class CircuitOpen(AgentError):
    """Circuit breaker rejected call."""


class StateTransitionError(AgentError):
    """Illegal FSM transition."""


class SkillNotFound(AgentError):
    """Unknown skill name."""


class PreconditionFailed(AgentError):
    """Skill precondition not met."""


class ConfigurationError(AgentError):
    """Invalid configuration."""


class BackendUnavailable(AgentError):
    """Required backend not installed."""


class CancelledError(AgentError):
    """Cooperative cancellation requested."""


class TimeoutExceeded(AgentError):
    """Operation exceeded its deadline."""


class ValidationFailed(AgentError):
    """Input failed a domain invariant."""


class ReplayError(AgentError):
    """Action journal could not be replayed."""


ERROR_TAXONOMY: Final[tuple[type[AgentError], ...]] = (
    PerceptionError,
    MotorError,
    SafetyViolation,
    BudgetExhausted,
    CircuitOpen,
    StateTransitionError,
    SkillNotFound,
    PreconditionFailed,
    ConfigurationError,
    BackendUnavailable,
    CancelledError,
    TimeoutExceeded,
    ValidationFailed,
    ReplayError,
)
