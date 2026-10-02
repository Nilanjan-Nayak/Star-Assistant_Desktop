"""Reasoning — intent, answer text and *sanitised* action proposals.

Blueprint §7 Phase 3: "Use the existing LLM providers but always validate model
output. Never let raw model output directly execute arbitrary shell/OS commands."

Reuse, not rewrite:
  * :class:`ProviderReasoner` calls ``Backend.llm.provider.get_llm_provider()``
    (Gemini → local Ollama → offline intent engine, which itself runs the
    deterministic command router first).
  * :class:`RouterReasoner` calls ``Backend.nlu.command_router.route_command``.
  * :class:`FallbackReasoner` always answers, in the right language.

Every provider runs in a worker thread behind ``asyncio.wait_for`` so a hung LLM
can never block the gateway, and every returned action passes through
:func:`sanitize_actions` / :func:`sanitize_proposed_calls` before it can become a
:class:`~Backend.star.brain.schemas.ToolCall`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Iterable, Protocol, runtime_checkable

from Backend.star.brain.schemas import Context, RawAction, Reasoning, UserRequest
from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import star_logger

_log = star_logger("reasoning")

__all__ = [
    "ARG_KEY_RE",
    "DANGEROUS_ARG_KEYS",
    "FallbackReasoner",
    "ProviderReasoner",
    "Reasoner",
    "ReasoningCascade",
    "RouterReasoner",
    "SHELLY",
    "TOOL_NAME_RE",
    "build_reasoner",
    "registered_tools",
    "sanitize_actions",
    "sanitize_proposed_calls",
]

TOOL_NAME_RE = re.compile(r"^[a-z0-9_][a-z0-9_.\-]{0,63}$", re.IGNORECASE)
ARG_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_\-]{0,39}$")
SHELLY = re.compile(r"[;&|`$><\n\r]|\$\(|\|\||&&")
DANGEROUS_ARG_KEYS = frozenset({"command", "cmd", "shell", "script", "code", "exec", "bash", "powershell"})
MAX_ARG_DEPTH = 3
MAX_ARG_STR = 4000


def registered_tools() -> list[str]:
    """Tool names known to the legacy registry (auto-discovery makes this dynamic)."""
    try:
        from Backend.tools.registry import get_all_tools

        return [str(name) for name in get_all_tools()]
    except Exception:  # noqa: BLE001 — tools may not import on a bare machine
        return []


def _coerce(value: Any, depth: int = 0) -> tuple[Any, bool]:
    """Return a JSON-safe value plus a flag saying whether it was altered."""
    if depth > MAX_ARG_DEPTH:
        return None, True
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        return (value[:MAX_ARG_STR], len(value) > MAX_ARG_STR)
    if isinstance(value, (list, tuple)):
        out, changed = [], False
        for item in value:
            coerced, item_changed = _coerce(item, depth + 1)
            changed = changed or item_changed
            if coerced is not None:
                out.append(coerced)
        return out, changed
    if isinstance(value, dict):
        out, changed = {}, False
        for key, item in value.items():
            if not isinstance(key, str) or not ARG_KEY_RE.match(key):
                changed = True
                continue
            coerced, item_changed = _coerce(item, depth + 1)
            changed = changed or item_changed
            out[key] = coerced
        return out, changed
    return repr(value)[:MAX_ARG_STR], True     # exotic objects → repr, never passed through


def sanitize_actions(
    raw_actions: Iterable[Any],
    *,
    allowed_tools: Iterable[str] | None = None,
    allow_shell: bool = False,
) -> tuple[list[RawAction], list[str]]:
    """Validate legacy ``{tool, args, result}`` actions.

    Returns ``(actions, notes)``. Rules:
      * tool name must look like a tool and, when an allow-list is given, be registered
      * argument keys must be identifiers; values JSON-safe and depth-limited
      * shell-ish payloads in command-like arguments are dropped unless ``allow_shell``
    """
    allow = {str(name).lower() for name in (allowed_tools if allowed_tools is not None else registered_tools())}
    clean: list[RawAction] = []
    notes: list[str] = []
    for entry in raw_actions or []:
        if isinstance(entry, RawAction):
            action = entry
        elif isinstance(entry, dict):
            result = entry.get("result")
            action = RawAction(
                tool=str(entry.get("tool") or entry.get("name") or ""),
                args=dict(entry.get("args") or entry.get("arguments") or {}),
                result=dict(result) if isinstance(result, dict) else {},
                executed=bool(entry.get("executed", True)),
                source=str(entry.get("source") or "legacy"),
            )
        else:
            notes.append("skipped malformed action")
            continue

        tool = action.tool.strip()
        if not tool or not TOOL_NAME_RE.match(tool):
            notes.append(f"dropped invalid tool name {tool!r}")
            _log.warning("dropped action with invalid tool name: %r", tool)
            continue
        if allow and tool.lower() not in allow:
            notes.append(f"dropped unregistered tool {tool}")
            _log.warning("dropped action for unregistered tool: %s", tool)
            continue

        args: dict[str, Any] = {}
        for key, value in (action.args or {}).items():
            if not isinstance(key, str) or not ARG_KEY_RE.match(key):
                notes.append(f"dropped argument key {key!r} for {tool}")
                continue
            if key.lower() in DANGEROUS_ARG_KEYS:
                text = value if isinstance(value, str) else json.dumps(value, default=str)
                if SHELLY.search(text) and not allow_shell:
                    notes.append(f"dropped shell-ish {key} for {tool}")
                    _log.warning("dropped shell-ish %s argument for %s", key, tool)
                    continue
            coerced, changed = _coerce(value)
            if changed:
                notes.append(f"coerced {key} for {tool}")
            args[key] = coerced

        result, _ = _coerce(dict(action.result or {}))
        clean.append(
            RawAction(
                tool=tool,
                args=args,
                result=result if isinstance(result, dict) else {},
                executed=action.executed,
                source=action.source,
            )
        )
    return clean, notes


def sanitize_proposed_calls(
    calls: Iterable[Any],
    *,
    allowed_tools: Iterable[str] | None = None,
    allow_shell: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate *proposed* tool calls from an LLM (function calling / JSON blobs).

    Proposals are never executed as-is — they become :class:`ToolCall` entries in a
    plan that the orchestrator runs through policy + permissions (Phase 4/11).
    """
    allow = {str(name).lower() for name in (allowed_tools if allowed_tools is not None else registered_tools())}
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for call in calls or []:
        if isinstance(call, str):
            try:
                call = json.loads(call)
            except json.JSONDecodeError:
                notes.append("unparseable tool-call string")
                continue
        if not isinstance(call, dict):
            continue
        inner = call.get("function") if isinstance(call.get("function"), dict) else {}
        tool = str(call.get("tool") or call.get("name") or inner.get("name") or "").strip()
        if not tool or not TOOL_NAME_RE.match(tool):
            notes.append(f"dropped proposal with bad tool name {tool!r}")
            continue
        if allow and tool.lower() not in allow:
            notes.append(f"dropped proposal for unregistered tool {tool}")
            _log.warning("LLM proposed unregistered tool %s — dropped", tool)
            continue
        raw_args = call.get("arguments") or call.get("args") or inner.get("arguments") or {}
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError:
                notes.append(f"unparseable arguments for {tool}")
                raw_args = {}
        if not isinstance(raw_args, dict):
            raw_args = {}
        args: dict[str, Any] = {}
        for key, value in raw_args.items():
            if not isinstance(key, str) or not ARG_KEY_RE.match(key):
                continue
            if key.lower() in DANGEROUS_ARG_KEYS and not allow_shell:
                notes.append(f"dropped {key} argument for {tool} (allow_shell=false)")
                _log.warning("LLM proposed %s for %s — dropped (allow_shell=false)", key, tool)
                continue
            coerced, _ = _coerce(value)
            args[key] = coerced
        try:
            confidence = max(0.0, min(1.0, float(call.get("confidence") or 0.5)))
        except (TypeError, ValueError):
            confidence = 0.5
        out.append({"tool": tool, "arguments": args, "confidence": confidence})
    return out, notes


@runtime_checkable
class Reasoner(Protocol):
    name: str

    def available(self) -> bool: ...

    async def reason(self, request: UserRequest, context: Context) -> Reasoning | None: ...


class ProviderReasoner:
    """The existing provider chain: Gemini → local Ollama → offline intent engine."""

    name = "provider"

    def __init__(self, settings: Settings, *, timeout_s: float = 25.0, use_memory: bool = True) -> None:
        self.settings = settings
        self._timeout_s = timeout_s
        self._use_memory = use_memory
        self._provider: Any = None
        self._failed = False

    def _get_provider(self) -> Any:
        if self._provider is not None or self._failed:
            return self._provider
        try:
            from Backend.llm.provider import get_llm_provider

            self._provider = get_llm_provider()
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            _log.warning("LLM provider unavailable: %s", exc)
        return self._provider

    def available(self) -> bool:
        return self._get_provider() is not None

    def provider_name(self) -> str:
        provider = self._get_provider()
        return type(provider).__name__ if provider is not None else "none"

    async def reason(self, request: UserRequest, context: Context) -> Reasoning | None:
        provider = self._get_provider()
        if provider is None:
            return None
        memory_block = context.memory_block if self._use_memory else ""
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(provider.process_query, request.text, memory_block),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            _log.warning("LLM provider timed out after %.1fs", self._timeout_s)
            return None
        except Exception as exc:  # noqa: BLE001
            _log.warning("LLM provider failed: %s", exc)
            return None
        if not isinstance(raw, dict):
            return None
        response_text = str(raw.get("response") or "").strip()
        source = str(raw.get("source") or "provider")
        actions, action_notes = sanitize_actions(
            raw.get("actions") or [], allow_shell=self.settings.security.allow_shell
        )
        calls, call_notes = sanitize_proposed_calls(
            raw.get("tool_calls") or raw.get("proposed") or [],
            allow_shell=self.settings.security.allow_shell,
        )
        if not response_text and not actions and not calls:
            return None
        return Reasoning(
            intent=str(raw.get("intent") or source),
            response_text=response_text,
            actions=actions,
            proposed_calls=calls,
            source=source,
            confidence=float(raw.get("confidence") or 0.7),
            used_llm=source not in {"command_router", "offline_intent", "fallback", "dataset", "dataset_exact"},
            language=request.language,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            notes="; ".join(action_notes + call_notes)[:400],
        )


class RouterReasoner:
    """Deterministic command router — real tools, zero LLM, sub-millisecond."""

    name = "router"

    def __init__(self, settings: Settings, *, timeout_s: float | None = None) -> None:
        self.settings = settings
        self._timeout_s = timeout_s if timeout_s is not None else settings.brain.router_timeout_s
        self._failed = False

    def available(self) -> bool:
        if self._failed:
            return False
        try:
            from Backend.nlu.command_router import route_command  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            _log.warning("command router unavailable: %s", exc)
            return False
        return True

    async def reason(self, request: UserRequest, context: Context) -> Reasoning | None:
        try:
            from Backend.nlu.command_router import route_command
        except Exception:  # noqa: BLE001
            return None
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(route_command, request.text), timeout=self._timeout_s)
        except TimeoutError:
            _log.warning("command router timed out")
            return None
        except Exception as exc:  # noqa: BLE001
            _log.warning("command router failed: %s", exc)
            return None
        if not isinstance(raw, dict) or not (raw.get("response") or raw.get("actions")):
            return None
        actions, notes = sanitize_actions(
            raw.get("actions") or [], allow_shell=self.settings.security.allow_shell
        )
        return Reasoning(
            intent=str(raw.get("intent") or raw.get("source") or "command"),
            response_text=str(raw.get("response") or "").strip(),
            actions=actions,
            source=str(raw.get("source") or "command_router"),
            confidence=float(raw.get("confidence") or 0.95),
            used_llm=False,
            language=request.language,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            notes="; ".join(notes)[:400],
        )


class FallbackReasoner:
    """Always answers — in Bengali or English, using context when it helps."""

    name = "fallback"

    _BN = {
        "greeting": "হ্যালো বন্ধু! আমি স্টার — বলো কী করে দিতে পারি?",
        "thanks": "ধন্যবাদ দেওয়ার একদম দরকার নেই বন্ধু, তোমার কাজে আসতে পেরে আমি আনন্দিত!",
        "identity": "আমি স্টার — তোমার নিজের কম্পিউটারের ভয়েস অ্যাসিস্ট্যান্ট। সব কিছু লোকালি চলে, তোমার কথা আমার কাছেই থাকে।",
        "capability": "আমি তোমার সার্বক্ষণিক বন্ধু ও সহকারী। যে কোনো প্রশ্ন, গান শোনা, কম্পিউটার ও ব্রাউজার নিয়ন্ত্রণ বা দৈনন্দিন কাজে আমি সবসময় তোমার পাশে আছি বন্ধু।",
        "time": "এখন সময় দেখে নাও বন্ধু — আমি ঘড়িটা খুলে দিতে পারি, চাইলে বলো।",
        "unknown": "আমি তোমার কথা মন দিয়ে শুনলাম বন্ধু। তুমি ঠিক কী জানতে চাইছো বা কী করাতে চাইছো আমাকে আরেকটু বুঝিয়ে বলবে? আমি সাথে সাথেই সাহায্য করছি!",
    }
    _EN = {
        "greeting": "Hey! Star here — what can I do for you?",
        "thanks": "No need to thank me, I'm glad I could help!",
        "identity": "I'm Star — your own computer's voice assistant. Everything runs locally and your words stay with you.",
        "capability": "I'm your intelligent desktop companion and assistant. I can help with information, music, volume and system control, browser tasks, and whatever you need, friend.",
        "time": "Let me open the clock for you — just say the word.",
        "unknown": "I'm listening closely, friend! Could you tell me a little more about what you'd like to do or know? I'm right here with you.",
    }

    def available(self) -> bool:
        return True

    async def reason(self, request: UserRequest, context: Context) -> Reasoning:
        text = request.text.lower()
        table = self._EN if request.language == "en" else self._BN
        intent = "unknown"
        if any(word in text for word in ("hello", "hi ", "hey", "হ্যালো", "হাই", "নমস্কার", "salam")):
            intent = "greeting"
        elif any(word in text for word in ("thank", "dhonnobad", "ধন্যবাদ", "shukriya")):
            intent = "thanks"
        elif any(word in text for word in ("who are you", "tomar nam", "তুমি কে", "what are you", "intro")):
            intent = "identity"
        elif any(word in text for word in ("what can you", "ki ki paro", "কী কী পারো", "help", "capability")):
            intent = "capability"
        elif any(word in text for word in ("time", "koyta baje", "কয়টা বাজে", "ঘড়ি")):
            intent = "time"
        response = table[intent]
        if intent == "greeting" and context.time_of_day and request.language != "en":
            greeting = {"morning": "শুভ সকাল", "afternoon": "শুভ দুপুর", "evening": "শুভ সন্ধ্যा", "night": "শুভ রাত্রি"}.get(
                context.time_of_day, ""
            )
            if greeting:
                response = f"{greeting} বন্ধু! {response}"
        return Reasoning(
            intent=intent,
            response_text=response,
            actions=[],
            source="fallback",
            confidence=0.4 if intent == "unknown" else 0.75,
            used_llm=False,
            language=request.language,
            latency_ms=0.0,
            notes="deterministic offline answer",
        )


class ReasoningCascade:
    """Runs reasoners in order; the first one that answers wins."""

    name = "cascade"

    def __init__(self, reasoners: Iterable[Reasoner], *, bus: StarEventBus | None = None) -> None:
        self.reasoners = list(reasoners)
        self.bus = bus or StarEventBus()
        self.stats: dict[str, int] = {"calls": 0, "answered": 0, "failures": 0}

    def available(self) -> bool:
        return any(self._safe_available(reasoner) for reasoner in self.reasoners)

    @staticmethod
    def _safe_available(reasoner: Reasoner) -> bool:
        try:
            return bool(reasoner.available())
        except Exception:  # noqa: BLE001
            return False

    async def reason(self, request: UserRequest, context: Context) -> Reasoning:
        self.stats["calls"] += 1
        tried: list[str] = []
        for reasoner in self.reasoners:
            if not self._safe_available(reasoner):
                tried.append(f"{reasoner.name}:unavailable")
                continue
            try:
                result = await reasoner.reason(request, context)
            except Exception as exc:  # noqa: BLE001 — one bad reasoner must not kill the turn
                self.stats["failures"] += 1
                _log.warning("reasoner %s failed: %s", reasoner.name, exc)
                tried.append(f"{reasoner.name}:error")
                continue
            if result is not None and (result.response_text or result.has_work):
                self.stats["answered"] += 1
                self.bus.emit(
                    "reasoning.complete",
                    phase=EventPhase.BRAIN,
                    request_id=request.request_id,
                    reasoner=reasoner.name,
                    intent=result.intent,
                    source=result.source,
                    used_llm=result.used_llm,
                    actions=len(result.actions),
                    proposals=len(result.proposed_calls),
                    confidence=round(result.confidence, 3),
                    latency_ms=result.latency_ms,
                    tried=tried,
                )
                return result
            tried.append(f"{reasoner.name}:empty")
        return Reasoning(
            intent="unknown",
            response_text="দুঃখিত বন্ধু, এখন কিছু বলা যাচ্ছে না।",
            source="none",
            confidence=0.0,
            language=request.language,
            notes="all reasoners failed: " + ",".join(tried)[:200],
        )

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": getattr(r, "name", type(r).__name__), "available": self._safe_available(r)}
            for r in self.reasoners
        ]


def build_reasoner(settings: Settings, *, bus: StarEventBus | None = None) -> ReasoningCascade:
    """Default chain: deterministic router → LLM provider chain → offline fallback.

    Unavailable stages (missing optional deps, ``llm_mode=null``) are skipped at
    build time; :class:`FallbackReasoner` is always appended last so Star answers
    even on a machine with no LLM and no router.
    """
    factories = {
        "router": lambda: RouterReasoner(settings),
        "provider": lambda: ProviderReasoner(settings, timeout_s=settings.brain.llm_timeout_s),
        "fallback": FallbackReasoner,
    }
    chain: list[Reasoner] = []
    for stage in settings.brain.reasoning_chain:
        factory = factories.get(stage)
        if factory is None:
            _log.warning("unknown reasoning stage %r — skipped", stage)
            continue
        if stage == "provider" and settings.brain.llm_mode == "null":
            continue
        reasoner = factory()
        if not reasoner.available():
            _log.info("reasoning stage %s unavailable — skipped", stage)
            continue
        chain.append(reasoner)
    if not any(getattr(r, "name", "") == "fallback" for r in chain):
        chain.append(FallbackReasoner())
    return ReasoningCascade(chain, bus=bus)
