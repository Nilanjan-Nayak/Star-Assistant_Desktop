"""STAR 2.0 brain schemas — the typed vocabulary above the tool layer.

Blueprint §7 Phase 3: "define ``UserRequest``, ``Context``, ``Plan``, ``Task`` and
``ToolCall`` schemas. Retrieve relevant memory before planning. Add reflection
after execution. Never let raw model output directly execute arbitrary shell/OS
commands."

These models are the contract between the gateway, the brain, the orchestrator and
the audit log. Everything is JSON-round-trippable (``model_dump(mode="json")``) so
the HUD/console never needs to import them.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AgentName",
    "Context",
    "MemoryHit",
    "Plan",
    "Prediction",
    "RawAction",
    "Reasoning",
    "Reflection",
    "Task",
    "TaskResult",
    "TaskState",
    "ToolCall",
    "ToolCallState",
    "UserRequest",
    "Verification",
    "new_call_id",
    "new_plan_id",
    "new_request_id",
    "new_task_id",
]


@enum.unique
class TaskState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERED = "recovered"
    BLOCKED = "blocked"


TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED, TaskState.RECOVERED, TaskState.BLOCKED}
)


@enum.unique
class ToolCallState(str, enum.Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DENIED = "denied"
    SKIPPED = "skipped"


@enum.unique
class AgentName(str, enum.Enum):
    CONVERSATION = "conversation"
    RESEARCH = "research"
    BROWSER = "browser"
    COMPUTER = "computer"
    FILESYSTEM = "filesystem"
    SYSTEM = "system"
    CODING = "coding"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_request_id() -> str:
    from agent.core.ids import new_id

    return new_id("req")


def new_plan_id() -> str:
    from agent.core.ids import new_id

    return new_id("plan")


def new_task_id() -> str:
    from agent.core.ids import new_id

    return new_id("task")


def new_call_id() -> str:
    from agent.core.ids import new_id

    return new_id("call")


class UserRequest(BaseModel):
    """One thing the user asked for (voice or text)."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(default_factory=new_request_id)
    text: str
    language: str = "bn"
    language_profile: dict[str, Any] = Field(default_factory=dict)
    source: str = "text"                    # text | voice | websocket | http | hud | console
    session_id: str = "star-default"
    speaker_id: str | None = None
    audio_ref: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class MemoryHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    layer: str = "semantic"                 # working | episodic | semantic | preference | pattern
    kind: str = "fact"
    score: float = Field(default=0.0, ge=-1.0, le=1.0)
    key: str | None = None
    value: str | None = None


class Context(BaseModel):
    """Everything the brain knows *before* it plans (memory retrieval happens first)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = "star-default"
    user_id: str = "user"
    language: str = "bn"
    time_of_day: str = ""                   # morning | afternoon | evening | night
    working_memory: list[str] = Field(default_factory=list)
    retrieved_memory: list[MemoryHit] = Field(default_factory=list)
    memory_block: str = ""                  # prompt-ready digest
    screen_summary: str | None = None
    active_window: str | None = None
    recent_tasks: list[dict[str, Any]] = Field(default_factory=list)
    predictions: list["Prediction"] = Field(default_factory=list)
    dry_run: bool = True
    built_at: datetime = Field(default_factory=_utcnow)

    def compact(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "language": self.language,
            "time_of_day": self.time_of_day,
            "working_memory": self.working_memory[-5:],
            "retrieved": [hit.model_dump(mode="json") for hit in self.retrieved_memory[:5]],
            "screen_summary": self.screen_summary,
            "dry_run": self.dry_run,
        }


class Prediction(BaseModel):
    """A *suggestion*. Never executed directly — it must become a Task and pass policy."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    source: str = "history"                 # history | preference | pattern | sequence
    executed: bool = False                  # always False: predictions are not actions


class RawAction(BaseModel):
    """An action as reported by the legacy router/provider (already validated shape)."""

    model_config = ConfigDict(extra="allow")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    executed: bool = True                   # the legacy pipeline executes as it routes
    source: str = "legacy"


class Reasoning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = "unknown"
    response_text: str = ""
    actions: list[RawAction] = Field(default_factory=list)
    proposed_calls: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "fallback"                # command_router | offline_intent | llm | dataset | fallback
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    used_llm: bool = False
    language: str = "bn"
    latency_ms: float = Field(default=0.0, ge=0.0)
    notes: str = ""

    @property
    def has_work(self) -> bool:
        return bool(self.actions) or bool(self.proposed_calls)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(default_factory=new_call_id)
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk: str = "unknown"                   # low | medium | high | critical | unknown (Phase 4/11 fill)
    requires_confirmation: bool = False
    state: ToolCallState = ToolCallState.PROPOSED
    attempt: int = 0
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = Field(default=0.0, ge=0.0)
    from_prediction: bool = False
    confirmation_id: str | None = None


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected: str = ""
    observed: str = ""
    ok: bool = False
    method: str = "result_flag"             # result_flag | screen_diff | output_match | none
    note: str = ""


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(default_factory=new_task_id)
    goal: str
    agent: AgentName = AgentName.CONVERSATION
    steps: list[ToolCall] = Field(default_factory=list)
    state: TaskState = TaskState.PENDING
    priority: int = Field(default=5, ge=1, le=10)
    risk: str = "unknown"
    retries: int = 0
    max_retries: int = 1
    verification: Verification = Field(default_factory=Verification)
    summary: str = ""
    error: str | None = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TASK_STATES

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["is_terminal"] = self.is_terminal
        return data


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=new_plan_id)
    request_id: str = ""
    intent: str = "unknown"
    rationale: str = ""
    language: str = "bn"
    tasks: list[Task] = Field(default_factory=list)
    requires_confirmation: bool = False
    predicted_next: list[Prediction] = Field(default_factory=list)
    source: str = "brain"
    created_at: datetime = Field(default_factory=_utcnow)

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    @property
    def all_terminal(self) -> bool:
        return all(task.is_terminal for task in self.tasks) if self.tasks else True

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["task_count"] = self.task_count
        data["all_terminal"] = self.all_terminal
        return data


class Reflection(BaseModel):
    """Post-execution critique (blueprint: OBSERVE → ACT → VERIFY → RECOVER)."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = False
    verdict: str = "unknown"                # success | partial | failed | blocked | no_action
    expected: str = ""
    observed: str = ""
    suggestion: str = ""
    should_retry: bool = False
    should_recover: bool = False
    should_escalate: bool = False           # needs a human confirmation
    evidence: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    ok: bool = False
    summary: str = ""                       # Bengali (user facing)
    summary_en: str = ""
    artifacts: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    spoken_response: str = ""


Context.model_rebuild()
Reasoning.model_rebuild()
