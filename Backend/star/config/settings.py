"""STAR 2.0 settings — one frozen, env-driven configuration object.

Design rules
------------
* **No secret values are stored in a field that can be serialised.** API keys are
  resolved lazily through :mod:`Backend.star.security.secrets` and only ever
  appear as ``"<set>"`` / ``"<unset>"`` in diagnostics.
* Every field has a working default so the system boots with an empty env.
* ``dry_run`` defaults to **True** — the blueprint requires computer automation to
  start in dry-run/test mode.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from Backend.star.config.dotenv import load_dotenv_file

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on", "y"})


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw is not None and raw.strip() else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw is not None and raw.strip() else default
    except ValueError:
        return default


def _tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _ints(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return tuple(out) or default


def _str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw is not None and raw.strip() else default


def _csv(name: str, default: str) -> list[str]:
    raw = _str(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class GatewaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "0.0.0.0"  # noqa: S104 — bound inside a trusted desktop/sandbox, not the internet
    port: int = Field(default=8765, ge=0, le=65535)
    enabled: bool = True
    request_timeout_s: float = Field(default=30.0, gt=0)
    max_body_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    event_buffer: int = Field(default=500, ge=1)
    console_enabled: bool = True
    allowed_origins: tuple[str, ...] = ("*",)


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stt_provider: str = "auto"          # auto | google_dual | whisper | browser | file | null
    tts_provider: str = "auto"          # auto | edge | null
    tts_voice: str = "bn-IN-BashkarNeural"
    languages: tuple[str, ...] = ("bn", "en")
    default_response_language: str = "bn"
    keep_transcripts: bool = False       # privacy: transcripts are dropped by default
    allow_barge_in: bool = True
    auto_speak: bool = False           # gateway default: the HUD owns playback
    speaker_verification: bool = False
    max_utterance_s: float = Field(default=12.0, gt=0)


class BrainSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    llm_mode: str = "auto"               # auto | local | gemini | offline | null
    local_llm_url: str = "http://localhost:11434/v1"
    local_llm_model: str = "star-local:latest"
    gemini_model: str = "gemini-2.5-flash"
    max_plan_tasks: int = Field(default=8, ge=1, le=32)
    max_tool_calls_per_task: int = Field(default=12, ge=1, le=64)
    reflection_enabled: bool = True
    prediction_enabled: bool = True
    retrieval_top_k: int = Field(default=5, ge=1, le=25)
    llm_timeout_s: float = Field(default=25.0, gt=0)
    router_timeout_s: float = Field(default=20.0, gt=0)
    reasoning_chain: tuple[str, ...] = ("router", "provider", "fallback")


class MemorySettings(BaseModel):
    """Phase 8 — the layered memory manager's bounds.

    There is deliberately **no second store** here: these numbers shape retrieval
    over the existing ``agent.memory.store.MemoryStore`` (SQLite + hashing-trick
    embeddings), ``agent.planning.memory.EpisodicMemory`` (JSONL) and the brain's
    ``PatternStore`` (JSONL).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    working_sessions: int = Field(default=32, ge=1, le=256)   # how many sessions keep scratch
    working_turns: int = Field(default=8, ge=1, le=64)        # turns kept per session
    retrieval_limit: int = Field(default=8, ge=1, le=40)      # hits returned to the brain
    per_layer_limit: int = Field(default=5, ge=1, le=25)      # candidates gathered per layer
    episode_recall: int = Field(default=5, ge=1, le=25)       # similar past episodes
    recent_scan: int = Field(default=300, ge=1, le=5000)      # window used for counts/preferences
    timeout_s: float = Field(default=4.0, gt=0)               # bound on every blocking store call
    weight_working: float = Field(default=1.0, ge=0.0, le=1.0)
    weight_preference: float = Field(default=0.95, ge=0.0, le=1.0)
    weight_episodic: float = Field(default=0.85, ge=0.0, le=1.0)
    weight_semantic: float = Field(default=0.8, ge=0.0, le=1.0)
    weight_pattern: float = Field(default=0.6, ge=0.0, le=1.0)


class LearningSettings(BaseModel):
    """Phase 9 — the safe learning loop's gates.

    Learning here is **validated promotion**, never self-modification. The loop
    observes finished runs, accumulates *candidate* patterns/preferences, and only
    promotes the ones that clear every gate below into the stores Phase 8 already
    reads (the brain's ``PatternStore`` for procedural patterns, the shared
    ``MemoryStore`` for preferences). It writes data only — it can never write
    executable code or model weights, and the validator rejects any candidate that
    even looks like either (blueprint §9: "Do not permit uncontrolled
    self-modification of executable code or model weights").
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    auto_promote: bool = True               # promote validated candidates without being asked
    min_frequency: int = Field(default=3, ge=1, le=1000)        # observations before a pair is a candidate
    min_success_rate: float = Field(default=0.8, ge=0.0, le=1.0)  # successes / (successes + failures)
    max_risk: str = "medium"                # never learn a pattern whose tools exceed this tier
    max_candidates: int = Field(default=500, ge=1, le=5000)     # bound the working set
    max_promotions_per_cycle: int = Field(default=8, ge=1, le=200)
    require_approval_for_preferences: bool = True   # a preference needs explicit positive feedback
    timeout_s: float = Field(default=4.0, gt=0)


class OrchestratorSettings(BaseModel):
    """Phase 10 — advanced orchestration.

    Decides how tasks and plans are sequenced, routed across agents,
    checkpointed for pause/resume/rollback, and recovered upon failure.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    max_parallel: int = Field(default=4, ge=1, le=32)
    max_retries: int = Field(default=2, ge=0, le=10)
    task_timeout_s: float = Field(default=30.0, gt=0)
    plan_timeout_s: float = Field(default=120.0, gt=0)
    checkpoint_enabled: bool = True
    max_checkpoints: int = Field(default=20, ge=1, le=500)
    allow_parallel_agents: bool = True


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    safety_level: str = "normal"         # permissive | normal | strict | paranoid
    dry_run: bool = True                 # ★ blueprint: automation starts in dry-run
    confirm_above_risk: str = "high"     # low | medium | high | critical
    deny_risk: str = "critical"          # deny-by-default tier
    max_actions_per_session: int = Field(default=200, ge=1)
    max_actions_per_minute: int = Field(default=60, ge=1)
    confirmation_ttl_s: float = Field(default=120.0, gt=0)
    audit_path: str = "logs/audit.jsonl"
    tool_allowlist: tuple[str, ...] = ()          # empty = all registered tools allowed
    tool_denylist: tuple[str, ...] = ()
    allow_shell: bool = False                     # raw shell is deny-by-default forever
    legacy_gate: bool = True                      # route legacy execute_tool() through policy


class PathsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path = _REPO_ROOT
    data_dir: Path = _REPO_ROOT / "data"
    workspace_root: Path = _REPO_ROOT / "data" / "workspace"
    logs_dir: Path = _REPO_ROOT / "logs"
    memory_db: Path = _REPO_ROOT / "star_memory.db"
    episodes_path: Path = _REPO_ROOT / "episodes.jsonl"
    patterns_path: Path = _REPO_ROOT / "data" / "patterns.jsonl"
    candidates_path: Path = _REPO_ROOT / "data" / "learning" / "candidates.jsonl"
    feedback_path: Path = _REPO_ROOT / "data" / "learning" / "feedback.jsonl"
    checkpoints_path: Path = _REPO_ROOT / "data" / "orchestrator" / "checkpoints.jsonl"
    browser_profile_root: Path = _REPO_ROOT / "data" / "workspace" / "_profiles"

    def ensure(self) -> None:
        for path in (
            self.data_dir,
            self.workspace_root,
            self.logs_dir,
            self.browser_profile_root,
            self.checkpoints_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


class WorkspaceSettings(BaseModel):
    """Phase 7 — the invisible, isolated background workspace.

    The jail (a per-session directory the manager refuses to escape) is always on;
    these numbers bound how much a background session may hold and how long it lives.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    max_sessions: int = Field(default=4, ge=1, le=32)
    session_ttl_s: float = Field(default=3600.0, gt=0)
    isolation_backend: str = "null"      # null | process | windows_virtual_desktop (future)
    checkpoint_enabled: bool = True
    max_checkpoints: int = Field(default=5, ge=1, le=50)
    max_files: int = Field(default=500, ge=1)            # per session
    max_bytes: int = Field(default=50_000_000, ge=1)     # per session
    max_write_bytes: int = Field(default=2_000_000, ge=1)  # per single write
    checkpoint_max_bytes: int = Field(default=10_000_000, ge=1)
    allow_spawn: bool = False        # opt-in: let a session start its own OS process


class BrowserSettings(BaseModel):
    """Phase 5 — the browser agent's leash.

    Defaults are paranoid: only http/https, no private/loopback hosts, no
    automatic launching of a real browser, and a hard cap on bytes/steps.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    allowed_schemes: tuple[str, ...] = ("http", "https")
    allowed_ports: tuple[int, ...] = (80, 443)
    allow_private_hosts: bool = False      # loopback / RFC1918 / link-local
    resolve_hosts: bool = False            # DNS-check every resolved IP (SSRF hardening)
    host_allowlist: tuple[str, ...] = ()   # empty = any public host
    host_denylist: tuple[str, ...] = ()
    timeout_s: float = Field(default=8.0, gt=0)
    max_bytes: int = Field(default=2_000_000, ge=1024)
    max_chars: int = Field(default=4000, ge=200)
    max_steps: int = Field(default=6, ge=1, le=32)
    step_timeout_s: float = Field(default=20.0, gt=0)
    auto_open: bool = False                # never launch a GUI browser unless asked
    user_agent: str = "StarAssistant/2.0 (desktop agent; dry-run by default)"


class ComputerSettings(BaseModel):
    """Phase 6 — the computer agent's leash (screen, mouse, keyboard).

    Dry-run is inherited from :class:`SecuritySettings`; these fields bound what a
    *real* run may do, and what a simulated run may claim.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    backend: str = "auto"                   # auto | null | pyautogui
    screen_width: int = Field(default=1920, ge=64, le=32768)
    screen_height: int = Field(default=1080, ge=48, le=32768)
    forbidden_regions: tuple[str, ...] = ()  # "x,y,w,h" — never click in here
    blocked_hotkeys: tuple[str, ...] = ("ctrl+alt+del", "ctrl+alt+backspace", "alt+sysrq")
    max_text_chars: int = Field(default=2000, ge=1)
    max_scroll: int = Field(default=20, ge=1)
    max_wait_s: float = Field(default=30.0, gt=0)
    max_steps: int = Field(default=8, ge=1, le=32)
    step_timeout_s: float = Field(default=20.0, gt=0)
    verify_delay_s: float = Field(default=0.4, ge=0.0)
    change_threshold: float = Field(default=0.02, ge=0.0, le=1.0)


class Settings(BaseModel):
    """Root configuration object for the whole STAR 2.0 stack."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    env_file: str = ".env"
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    brain: BrainSettings = Field(default_factory=BrainSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    learning: LearningSettings = Field(default_factory=LearningSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    computer: ComputerSettings = Field(default_factory=ComputerSettings)
    session_id: str = "star-default"
    owner: str = "Nilanjan"
    log_level: str = "INFO"
    frontend_mirror: bool = False

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls, *, load_env_file: bool = True) -> Settings:
        if load_env_file:
            load_dotenv_file(_REPO_ROOT / _str("STAR_ENV_FILE", ".env"))
        paths = PathsSettings(
            repo_root=_REPO_ROOT,
            data_dir=Path(_str("STAR_DATA_DIR", str(_REPO_ROOT / "data"))),
            workspace_root=Path(_str("STAR_WORKSPACE_ROOT", str(_REPO_ROOT / "data" / "workspace"))),
            logs_dir=Path(_str("STAR_LOGS_DIR", str(_REPO_ROOT / "logs"))),
            memory_db=Path(_str("STAR_MEMORY_DB", str(_REPO_ROOT / "star_memory.db"))),
            episodes_path=Path(_str("STAR_EPISODES_PATH", str(_REPO_ROOT / "episodes.jsonl"))),
            patterns_path=Path(_str("STAR_PATTERNS_PATH", str(_REPO_ROOT / "data" / "patterns.jsonl"))),
            candidates_path=Path(
                _str("STAR_CANDIDATES_PATH", str(_REPO_ROOT / "data" / "learning" / "candidates.jsonl"))
            ),
            feedback_path=Path(_str("STAR_FEEDBACK_PATH", str(_REPO_ROOT / "data" / "learning" / "feedback.jsonl"))),
            checkpoints_path=Path(
                _str("STAR_CHECKPOINTS_PATH", str(_REPO_ROOT / "data" / "orchestrator" / "checkpoints.jsonl"))
            ),
            browser_profile_root=Path(
                _str("STAR_BROWSER_PROFILE_ROOT", str(_REPO_ROOT / "data" / "workspace" / "_profiles"))
            ),
        )
        return cls(
            env_file=_str("STAR_ENV_FILE", ".env"),
            gateway=GatewaySettings(
                host=_str("STAR_GATEWAY_HOST", "0.0.0.0"),
                port=_int("STAR_GATEWAY_PORT", 8765),
                enabled=_bool("STAR_GATEWAY_ENABLED", True),
                request_timeout_s=_float("STAR_GATEWAY_TIMEOUT_S", 30.0),
                max_body_bytes=_int("STAR_GATEWAY_MAX_BODY", 2 * 1024 * 1024),
                event_buffer=_int("STAR_EVENT_BUFFER", 500),
                console_enabled=_bool("STAR_CONSOLE_ENABLED", True),
                allowed_origins=tuple(_csv("STAR_ALLOWED_ORIGINS", "*")),
            ),
            voice=VoiceSettings(
                stt_provider=_str("STAR_STT_PROVIDER", "auto"),
                tts_provider=_str("STAR_TTS_PROVIDER", "auto"),
                tts_voice=_str("STAR_TTS_VOICE", "bn-IN-BashkarNeural"),
                languages=tuple(_csv("STAR_LANGUAGES", "bn,en")),
                default_response_language=_str("STAR_RESPONSE_LANGUAGE", "bn"),
                keep_transcripts=_bool("STAR_VOICE_KEEP_TRANSCRIPTS", False),
                allow_barge_in=_bool("STAR_VOICE_BARGE_IN", True),
                auto_speak=_bool("STAR_VOICE_AUTO_SPEAK", False),
                speaker_verification=_bool("STAR_VOICE_SPEAKER_VERIFY", False),
                max_utterance_s=_float("STAR_VOICE_MAX_UTTERANCE_S", 12.0),
            ),
            brain=BrainSettings(
                llm_mode=_str("STAR_LLM_MODE", "auto"),
                local_llm_url=_str("STAR_LOCAL_LLM_URL", "http://localhost:11434/v1"),
                local_llm_model=_str("STAR_LOCAL_LLM_MODEL", "star-local:latest"),
                gemini_model=_str("STAR_GEMINI_MODEL", "gemini-2.5-flash"),
                max_plan_tasks=_int("STAR_MAX_PLAN_TASKS", 8),
                max_tool_calls_per_task=_int("STAR_MAX_TOOL_CALLS", 12),
                reflection_enabled=_bool("STAR_REFLECTION", True),
                prediction_enabled=_bool("STAR_PREDICTION", True),
                retrieval_top_k=_int("STAR_RETRIEVAL_TOP_K", 5),
                llm_timeout_s=_float("STAR_LLM_TIMEOUT_S", 25.0),
                router_timeout_s=_float("STAR_ROUTER_TIMEOUT_S", 20.0),
                reasoning_chain=_tuple("STAR_REASONING_CHAIN", ("router", "provider", "fallback")),
            ),
            security=SecuritySettings(
                safety_level=_str("STAR_SAFETY_LEVEL", "normal"),
                dry_run=_bool("STAR_DRY_RUN", True),
                confirm_above_risk=_str("STAR_CONFIRM_ABOVE_RISK", "high"),
                deny_risk=_str("STAR_DENY_RISK", "critical"),
                max_actions_per_session=_int("STAR_MAX_ACTIONS_SESSION", 200),
                max_actions_per_minute=_int("STAR_MAX_ACTIONS_MINUTE", 60),
                confirmation_ttl_s=_float("STAR_CONFIRMATION_TTL_S", 120.0),
                audit_path=_str("STAR_AUDIT_PATH", "logs/audit.jsonl"),
                tool_allowlist=tuple(_csv("STAR_TOOL_ALLOWLIST", "")),
                tool_denylist=tuple(_csv("STAR_TOOL_DENYLIST", "")),
                allow_shell=_bool("STAR_ALLOW_SHELL", False),
                legacy_gate=_bool("STAR_LEGACY_GATE", True),
            ),
            browser=BrowserSettings(
                enabled=_bool("STAR_BROWSER_ENABLED", True),
                allowed_schemes=tuple(_csv("STAR_BROWSER_SCHEMES", "http,https")),
                allowed_ports=_ints("STAR_BROWSER_PORTS", (80, 443)),
                allow_private_hosts=_bool("STAR_BROWSER_ALLOW_PRIVATE", False),
                resolve_hosts=_bool("STAR_BROWSER_RESOLVE_HOSTS", False),
                host_allowlist=tuple(_csv("STAR_BROWSER_HOST_ALLOWLIST", "")),
                host_denylist=tuple(_csv("STAR_BROWSER_HOST_DENYLIST", "")),
                timeout_s=_float("STAR_BROWSER_TIMEOUT_S", 8.0),
                max_bytes=_int("STAR_BROWSER_MAX_BYTES", 2_000_000),
                max_chars=_int("STAR_BROWSER_MAX_CHARS", 4000),
                max_steps=_int("STAR_BROWSER_MAX_STEPS", 6),
                step_timeout_s=_float("STAR_BROWSER_STEP_TIMEOUT_S", 20.0),
                auto_open=_bool("STAR_BROWSER_AUTO_OPEN", False),
                user_agent=_str("STAR_BROWSER_USER_AGENT", "StarAssistant/2.0 (desktop agent; dry-run by default)"),
            ),
            computer=ComputerSettings(
                enabled=_bool("STAR_COMPUTER_ENABLED", True),
                backend=_str("STAR_COMPUTER_BACKEND", "auto"),
                screen_width=_int("STAR_SCREEN_WIDTH", 1920),
                screen_height=_int("STAR_SCREEN_HEIGHT", 1080),
                forbidden_regions=tuple(_csv("STAR_FORBIDDEN_REGIONS", "")),
                blocked_hotkeys=_tuple("STAR_BLOCKED_HOTKEYS", ("ctrl+alt+del", "ctrl+alt+backspace", "alt+sysrq")),
                max_text_chars=_int("STAR_COMPUTER_MAX_TEXT", 2000),
                max_scroll=_int("STAR_COMPUTER_MAX_SCROLL", 20),
                max_wait_s=_float("STAR_COMPUTER_MAX_WAIT_S", 30.0),
                max_steps=_int("STAR_COMPUTER_MAX_STEPS", 8),
                step_timeout_s=_float("STAR_COMPUTER_STEP_TIMEOUT_S", 20.0),
                verify_delay_s=_float("STAR_COMPUTER_VERIFY_DELAY_S", 0.4),
                change_threshold=_float("STAR_COMPUTER_CHANGE_THRESHOLD", 0.02),
            ),
            paths=paths,
            memory=MemorySettings(
                enabled=_bool("STAR_MEMORY_ENABLED", True),
                working_sessions=_int("STAR_MEMORY_WORKING_SESSIONS", 32),
                working_turns=_int("STAR_MEMORY_WORKING_TURNS", 8),
                retrieval_limit=_int("STAR_MEMORY_RETRIEVAL_LIMIT", 8),
                per_layer_limit=_int("STAR_MEMORY_PER_LAYER", 5),
                episode_recall=_int("STAR_MEMORY_EPISODE_RECALL", 5),
                recent_scan=_int("STAR_MEMORY_RECENT_SCAN", 300),
                timeout_s=_float("STAR_MEMORY_TIMEOUT_S", 4.0),
                weight_working=_float("STAR_MEMORY_WEIGHT_WORKING", 1.0),
                weight_preference=_float("STAR_MEMORY_WEIGHT_PREFERENCE", 0.95),
                weight_episodic=_float("STAR_MEMORY_WEIGHT_EPISODIC", 0.85),
                weight_semantic=_float("STAR_MEMORY_WEIGHT_SEMANTIC", 0.8),
                weight_pattern=_float("STAR_MEMORY_WEIGHT_PATTERN", 0.6),
            ),
            learning=LearningSettings(
                enabled=_bool("STAR_LEARNING_ENABLED", True),
                auto_promote=_bool("STAR_LEARNING_AUTO_PROMOTE", True),
                min_frequency=_int("STAR_LEARNING_MIN_FREQUENCY", 3),
                min_success_rate=_float("STAR_LEARNING_MIN_SUCCESS_RATE", 0.8),
                max_risk=_str("STAR_LEARNING_MAX_RISK", "medium"),
                max_candidates=_int("STAR_LEARNING_MAX_CANDIDATES", 500),
                max_promotions_per_cycle=_int("STAR_LEARNING_MAX_PROMOTIONS", 8),
                require_approval_for_preferences=_bool("STAR_LEARNING_REQUIRE_APPROVAL", True),
                timeout_s=_float("STAR_LEARNING_TIMEOUT_S", 4.0),
            ),
            orchestrator=OrchestratorSettings(
                enabled=_bool("STAR_ORCHESTRATOR_ENABLED", True),
                max_parallel=_int("STAR_ORCHESTRATOR_MAX_PARALLEL", 4),
                max_retries=_int("STAR_ORCHESTRATOR_MAX_RETRIES", 2),
                task_timeout_s=_float("STAR_ORCHESTRATOR_TASK_TIMEOUT_S", 30.0),
                plan_timeout_s=_float("STAR_ORCHESTRATOR_PLAN_TIMEOUT_S", 120.0),
                checkpoint_enabled=_bool("STAR_ORCHESTRATOR_CHECKPOINTS", True),
                max_checkpoints=_int("STAR_ORCHESTRATOR_MAX_CHECKPOINTS", 20),
                allow_parallel_agents=_bool("STAR_ORCHESTRATOR_PARALLEL_AGENTS", True),
            ),
            workspace=WorkspaceSettings(
                enabled=_bool("STAR_WORKSPACE_ENABLED", True),
                max_sessions=_int("STAR_WORKSPACE_MAX_SESSIONS", 4),
                session_ttl_s=_float("STAR_WORKSPACE_TTL_S", 3600.0),
                isolation_backend=_str("STAR_ISOLATION_BACKEND", "null"),
                checkpoint_enabled=_bool("STAR_WORKSPACE_CHECKPOINTS", True),
                max_checkpoints=_int("STAR_WORKSPACE_MAX_CHECKPOINTS", 5),
                max_files=_int("STAR_WORKSPACE_MAX_FILES", 500),
                max_bytes=_int("STAR_WORKSPACE_MAX_BYTES", 50_000_000),
                max_write_bytes=_int("STAR_WORKSPACE_MAX_WRITE_BYTES", 2_000_000),
                checkpoint_max_bytes=_int("STAR_WORKSPACE_CHECKPOINT_MAX_BYTES", 10_000_000),
                allow_spawn=_bool("STAR_WORKSPACE_ALLOW_SPAWN", False),
            ),
            session_id=_str("STAR_SESSION_ID", "star-default"),
            owner=_str("STAR_OWNER", "Nilanjan"),
            log_level=_str("STAR_LOG_LEVEL", "INFO"),
            frontend_mirror=_bool("STAR_GATEWAY_MIRROR", False),
        )

    # ── diagnostics ─────────────────────────────────────────────────────────
    def public_dict(self) -> dict[str, Any]:
        """Serialisable, secret-free view used by ``/api/v1/info`` and logs."""
        data = self.model_dump(mode="json")
        data["secrets"] = {
            "GEMINI_API_KEY": "<set>" if os.getenv("GEMINI_API_KEY") else "<unset>",
            "YOUTUBE_API_KEY": "<set>" if os.getenv("YOUTUBE_API_KEY") else "<unset>",
            "OPENAI_API_KEY": "<set>" if os.getenv("OPENAI_API_KEY") else "<unset>",
        }
        return data


_SETTINGS: Settings | None = None


def get_settings(*, reload: bool = False) -> Settings:
    """Process-wide settings singleton (``reload=True`` re-reads the environment)."""
    global _SETTINGS
    if _SETTINGS is None or reload:
        _SETTINGS = Settings.from_env()
        _SETTINGS.paths.ensure()
    return _SETTINGS


def reset_settings() -> None:
    """Test helper: drop the cached singleton."""
    global _SETTINGS
    _SETTINGS = None
