"""The five memory layers — adapters over the stores this repository already has.

Blueprint §9: *Working* (current session/task), *Episodic* (past interactions),
*Semantic* (stable extracted knowledge), *Preferences* (user-approved habits),
*Procedural patterns* (successful repeatable workflows) — and: *"The current
repository README already describes local-first long-term memory using SQLite and
optional vector embeddings. Inspect that implementation before introducing
another memory store."*

So nothing here owns storage. Each layer wraps an existing one:

===========  ======================================================================
layer        backed by
===========  ======================================================================
working      the brain's :class:`~Backend.star.brain.context.ContextBuilder` scratch
             (attached at runtime) — or a bounded local deque when there is no brain
episodic     ``agent.planning.memory.EpisodicMemory`` (JSONL episode log)
semantic     ``agent.memory.store.MemoryStore`` rows of kind ``fact``
preference   ``MemoryStore`` rows of kind ``preference`` (+ ``latest_by_key``)
pattern      ``Backend.star.brain.prediction.PatternStore`` (JSONL tool→tool counters)
===========  ======================================================================

Every layer answers the same two questions — ``recall(query)`` → ranked
:class:`~Backend.star.brain.schemas.MemoryHit`s and ``describe()`` → honest counts —
so :mod:`.retrieval` can blend them without knowing where anything lives.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable

from agent.core.enums import AgentState, MemoryKind
from agent.core.ids import new_episode_id, new_step_id
from agent.planning.episode import Episode
from agent.planning.step import PlanStep

from Backend.star.brain.schemas import MemoryHit
from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger

__all__ = [
    "EpisodicLayer",
    "LAYER_NAMES",
    "PreferenceLayer",
    "ProceduralLayer",
    "SemanticLayer",
    "WorkingMemory",
    "token_overlap",
]

_log = star_logger("star2.memory.layers")

#: blueprint order — also the order the console shows them in
LAYER_NAMES: tuple[str, ...] = ("working", "episodic", "semantic", "preference", "pattern")


def _tokens(text: str) -> set[str]:
    return {token for token in "".join(char if char.isalnum() else " " for char in str(text or "").lower()).split() if token}


def token_overlap(query: str, text: str) -> float:
    """Jaccard-ish overlap in ``[0, 1]`` — the same idea ``EpisodicMemory`` uses."""
    left, right = _tokens(query), _tokens(text)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _mentions_tool(tool: str, lowered_query: str, query_tokens: set[str]) -> bool:
    """Did the query talk about this tool? (literal name, or its distinguishing tokens)"""
    name = str(tool or "").lower()
    if name and name in lowered_query:
        return True
    parts = _tokens(tool)
    shared = parts & query_tokens
    return len(shared) >= 2 or (len(shared) == 1 and len(parts) == 1)


def _clamp(score: float) -> float:
    return max(-1.0, min(1.0, float(score or 0.0)))


# ── working ───────────────────────────────────────────────────────────────────


class WorkingMemory:
    """Current session/task scratch. Delegates to the brain when one is attached.

    The brain's :class:`ContextBuilder` already keeps per-session turns, and the
    blueprint forbids duplicate subsystems — so ``attach(provider)`` hands this
    layer over to it and the manager stops keeping its own copy.
    """

    name = "working"

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = (settings or Settings()).memory
        self.sessions: int = int(cfg.working_sessions)
        self.turns: int = int(cfg.working_turns)
        self._scratch: dict[str, deque[str]] = {}
        self._provider: Any = None
        self.last_session: str = str((settings or Settings()).session_id or "")
        self.stats = {"turns": 0, "recalled": 0}

    def _resolve(self, session_id: str = "") -> str:
        """Explicit session wins, then the session that last spoke, then the default."""
        return str(session_id or "").strip() or self.last_session or "default"

    def attach(self, provider: Any) -> bool:
        """Use the brain's scratch buffer instead of a private one."""
        if provider is None or not all(hasattr(provider, attr) for attr in ("remember_turn", "working_memory")):
            return False
        self._provider = provider
        self._scratch.clear()
        _log.info("star2.memory.working.attached provider=%s", type(provider).__name__)
        return True

    @property
    def delegated(self) -> bool:
        return self._provider is not None

    def remember_turn(self, session_id: str, text: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        key = self._resolve(session_id)
        self.last_session = key
        if self._provider is not None:
            try:
                self._provider.remember_turn(key, body)
                self.stats["turns"] += 1
                return
            except Exception as exc:  # noqa: BLE001 — fall back to the local scratch
                _log.warning("working memory provider failed (%s) — keeping a local copy", exc)
        buffer = self._scratch.setdefault(key, deque(maxlen=self.turns))
        buffer.append(body[:2000])
        while len(self._scratch) > self.sessions:
            self._scratch.pop(next(iter(self._scratch)))
        self.stats["turns"] += 1

    def turns_for(self, session_id: str = "") -> list[str]:
        key = self._resolve(session_id)
        if self._provider is not None:
            try:
                return [str(turn) for turn in self._provider.working_memory(key)]
            except Exception as exc:  # noqa: BLE001
                _log.warning("working memory read failed: %s", exc)
                return []
        return list(self._scratch.get(key, ()))

    def forget(self, session_id: str = "") -> int:
        key = self._resolve(session_id)
        dropped = len(self.turns_for(key))
        if self._provider is not None and hasattr(self._provider, "forget_session"):
            try:
                self._provider.forget_session(key)
            except Exception as exc:  # noqa: BLE001
                _log.warning("working memory forget failed: %s", exc)
        self._scratch.pop(key, None)
        return dropped

    def recall(self, query: str, session_id: str = "", *, limit: int = 5) -> list[MemoryHit]:
        turns = self.turns_for(session_id)
        scored: list[tuple[float, int, str]] = []
        for index, turn in enumerate(turns):
            overlap = token_overlap(query, turn)
            recency = (index + 1) / max(1, len(turns))          # newer turns rank higher
            score = 0.75 * overlap + 0.25 * recency if query.strip() else 0.25 * recency
            scored.append((score, index, turn))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        hits = [
            MemoryHit(text=turn[:500], layer=self.name, kind="turn", score=_clamp(score))
            for score, _index, turn in scored[: max(0, limit)]
            if score > 0.0 or not query.strip()
        ]
        self.stats["recalled"] += len(hits)
        return hits

    def count(self, session_id: str = "") -> int:
        if session_id or self._provider is not None:
            return len(self.turns_for(session_id))
        return sum(len(buffer) for buffer in self._scratch.values())

    def describe(self, session_id: str = "") -> dict[str, Any]:
        return {
            "count": self.count(session_id),
            "store": "brain ContextBuilder scratch" if self.delegated else "in-process deque",
            "detail": (
                f"{self.turns} turns/session · {self.sessions} sessions · "
                f"{self.stats['turns']} remembered · session '{self.last_session[:16]}'"
            ),
        }


# ── episodic ──────────────────────────────────────────────────────────────────


class EpisodicLayer:
    """Past interactions: what Star did, and whether it worked."""

    name = "episodic"

    def __init__(self, episodes: Any, *, recall: int = 5) -> None:
        #: Episodes live in the JSONL episode log only. ``MemoryStore.ingest_episode`` would
        #: write a second, verbose copy of the same interaction into SQLite, where it pollutes
        #: ``build_context_block`` — one episode, one home (blueprint: no duplicate subsystems).
        self.episodes = episodes
        self.recall_limit = int(recall)
        self.stats = {"recorded": 0, "recalled": 0}

    def available(self) -> bool:
        return self.episodes is not None

    def record(
        self,
        goal: str,
        *,
        steps: Iterable[dict[str, Any]] | None = None,
        succeeded: bool = True,
        episode_id: str = "",
        started_at: datetime | None = None,
    ) -> Episode:
        """Build an :class:`Episode` from a finished run and persist it in both stores."""
        text = str(goal or "").strip()
        if not text:
            raise ValueError("an episode needs a goal")
        plan_steps: list[PlanStep] = []
        for raw in list(steps or [])[:40]:
            if not isinstance(raw, dict):
                raw = {"tool": str(raw)[:64]}
            skill = str(raw.get("tool") or raw.get("skill") or raw.get("action") or "step")[:64]
            plan_steps.append(
                PlanStep(
                    step_id=new_step_id(),
                    # PlanStep wants real strings: thought is required, observation defaults to ""
                    thought=str(raw.get("expect") or raw.get("thought") or skill)[:300],
                    skill=skill,
                    params=_json_params(raw.get("arguments") or raw.get("params") or {}),
                    observation=str(raw.get("note") or raw.get("observation") or "")[:300],
                    success=bool(raw.get("ok", raw.get("success", True))),
                )
            )
        ended = datetime.now(timezone.utc)
        episode = Episode(
            episode_id=episode_id or new_episode_id(),
            goal=text[:500],
            steps=plan_steps,
            final_state=AgentState.SUCCEEDED if succeeded else AgentState.FAILED,
            started_at=started_at or ended,
            ended_at=ended,
        )
        try:
            self.episodes.save(episode)
        except Exception as exc:  # noqa: BLE001 — memory must never break a run
            _log.warning("episode save failed: %s", exc)
        self.stats["recorded"] += 1
        return episode

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        if not self.available() or not str(query or "").strip():
            return []
        try:
            similar = self.episodes.recall_similar(str(query))[: max(0, min(limit, self.recall_limit))]
        except Exception as exc:  # noqa: BLE001
            _log.warning("episode recall failed: %s", exc)
            return []
        hits: list[MemoryHit] = []
        for episode in similar:
            steps = getattr(episode, "steps", []) or []
            summary = ", ".join(str(getattr(step, "skill", "")) for step in steps[:6]) or "no steps recorded"
            hits.append(
                MemoryHit(
                    text=f"{episode.goal} → {summary}",
                    layer=self.name,
                    kind="episode",
                    score=_clamp(0.4 + 0.6 * token_overlap(query, episode.goal)),
                    key=str(episode.episode_id),
                    value="succeeded" if getattr(episode, "succeeded", False) else "failed",
                )
            )
        self.stats["recalled"] += len(hits)
        return hits

    def recent(self, *, limit: int = 5) -> list[dict[str, Any]]:
        if not self.available():
            return []
        stored = list(getattr(self.episodes, "_episodes", []) or [])[-max(0, limit):]
        return [
            {
                "episode_id": str(item.episode_id),
                "goal": str(item.goal)[:200],
                "steps": len(item.steps),
                "succeeded": bool(getattr(item, "succeeded", False)),
                "started_at": item.started_at.isoformat(timespec="seconds"),
            }
            for item in reversed(stored)
        ]

    def count(self) -> int:
        try:
            return int(getattr(self.episodes, "size", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def describe(self) -> dict[str, Any]:
        return {
            "count": self.count(),
            "store": "agent.planning.memory.EpisodicMemory (JSONL episode log)",
            "detail": f"{self.stats['recorded']} recorded here · {self.stats['recalled']} recalled · only succeeded episodes are recalled",
        }


# ── semantic ──────────────────────────────────────────────────────────────────


class SemanticLayer:
    """Stable extracted knowledge — ``MemoryStore`` rows of kind ``fact``."""

    name = "semantic"

    def __init__(self, store: Any, *, recent_scan: int = 300) -> None:
        self.store = store
        self.recent_scan = int(recent_scan)
        self.stats = {"remembered": 0, "recalled": 0}

    def available(self) -> bool:
        return self.store is not None

    def remember(self, text: str, *, key: str | None = None, value: str | None = None) -> Any:
        if not self.available():
            raise RuntimeError("the long-term memory store is not available")
        record = self.store.remember(str(text), kind=MemoryKind.FACT, key=key, value=value)
        self.stats["remembered"] += 1
        return record

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        if not self.available() or not str(query or "").strip():
            return []
        try:
            records = self.store.recall_relevant(str(query), top_k=max(1, limit * 3))
        except Exception as exc:  # noqa: BLE001
            _log.warning("semantic recall failed: %s", exc)
            return []
        hits = [
            MemoryHit(
                text=str(record.text)[:500],
                layer=self.name,
                kind=_kind_value(record),
                score=_clamp(record.score if record.score is not None else token_overlap(query, record.text)),
                key=record.key,
                value=record.value,
            )
            for record in records
            if _kind_value(record) == "fact"
        ][: max(0, limit)]
        self.stats["recalled"] += len(hits)
        return hits

    def recent(self, *, limit: int = 5) -> list[dict[str, Any]]:
        return [_record_brief(record) for record in self._scan() if _kind_value(record) == "fact"][: max(0, limit)]

    def _scan(self) -> list[Any]:
        if not self.available():
            return []
        try:
            return list(self.store.recall_recent(self.recent_scan))
        except Exception as exc:  # noqa: BLE001
            _log.warning("memory scan failed: %s", exc)
            return []

    def count(self) -> int:
        return sum(1 for record in self._scan() if _kind_value(record) == "fact")

    def describe(self) -> dict[str, Any]:
        total = 0
        if self.available():
            try:
                total = int(self.store.count())
            except Exception:  # noqa: BLE001
                total = 0
        return {
            "count": self.count(),
            "store": "agent.memory.store.MemoryStore (SQLite + hashing embeddings)",
            "detail": f"{total} record(s) of every kind in the store · counted over the last {self.recent_scan} · "
            f"{self.stats['remembered']} written here",
        }


# ── preferences ───────────────────────────────────────────────────────────────


class PreferenceLayer:
    """User-approved habits — ``MemoryStore`` rows of kind ``preference``."""

    name = "preference"

    def __init__(self, store: Any, *, recent_scan: int = 300) -> None:
        self.store = store
        self.recent_scan = int(recent_scan)
        self.stats = {"set": 0, "recalled": 0}

    def available(self) -> bool:
        return self.store is not None

    def set(self, key: str, value: str, *, text: str = "") -> Any:
        if not self.available():
            raise RuntimeError("the long-term memory store is not available")
        clean_key = str(key or "").strip()[:80]
        clean_value = str(value or "").strip()[:400]
        if not clean_key or not clean_value:
            raise ValueError("a preference needs both a key and a value")
        body = str(text or "").strip() or f"{clean_key} = {clean_value}"
        record = self.store.remember(body, kind=MemoryKind.PREFERENCE, key=clean_key, value=clean_value)
        self.stats["set"] += 1
        return record

    def get(self, key: str) -> str | None:
        if not self.available():
            return None
        try:
            record = self.store.latest_by_key(str(key))
        except Exception as exc:  # noqa: BLE001
            _log.warning("preference lookup failed: %s", exc)
            return None
        return str(record.value) if record is not None and record.value is not None else None

    def all(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Latest value per key, newest first."""
        if not self.available():
            return []
        try:
            records = list(self.store.recall_recent(self.recent_scan))
        except Exception as exc:  # noqa: BLE001
            _log.warning("preference scan failed: %s", exc)
            return []
        latest: dict[str, dict[str, Any]] = {}
        for record in records:
            if _kind_value(record) != "preference":
                continue
            key = str(record.key or record.text)[:80]
            candidate = {
                "key": key,
                "value": str(record.value or record.text)[:400],
                "text": str(record.text)[:200],
                "timestamp": record.timestamp.isoformat(timespec="seconds"),
                "memory_id": int(getattr(record, "memory_id", 0) or 0),
            }
            previous = latest.get(key)
            # ``recall_recent`` is newest-first, so a later row for the same key is only
            # interesting when it is genuinely newer (timestamp, then insertion order).
            if previous is None or (candidate["timestamp"], candidate["memory_id"]) > (
                previous["timestamp"],
                previous["memory_id"],
            ):
                latest[key] = candidate
        return list(latest.values())[: max(0, limit)]

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        tokens = _tokens(query)
        hits: list[MemoryHit] = []
        for item in self.all(limit=max(limit * 3, 10)):
            haystack = f"{item['key']} {item['value']} {item['text']}"
            overlap = token_overlap(query, haystack)
            direct = 0.35 if tokens and (tokens & _tokens(item["key"])) else 0.0
            score = min(1.0, overlap + direct)
            if score <= 0.0 and tokens:
                continue
            hits.append(
                MemoryHit(
                    text=f"{item['key']} = {item['value']}",
                    layer=self.name,
                    kind="preference",
                    score=_clamp(score if score > 0 else 0.2),
                    key=item["key"],
                    value=item["value"],
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        self.stats["recalled"] += len(hits[:limit])
        return hits[: max(0, limit)]

    def count(self) -> int:
        return len(self.all(limit=self.recent_scan))

    def describe(self) -> dict[str, Any]:
        return {
            "count": self.count(),
            "store": "MemoryStore rows of kind 'preference' (latest value per key)",
            "detail": f"{self.stats['set']} set here · {self.stats['recalled']} recalled",
        }


# ── procedural patterns ───────────────────────────────────────────────────────


class ProceduralLayer:
    """How a repeated task is performed — the brain's ``tool → tool`` counters."""

    name = "pattern"

    def __init__(self, patterns: Any, *, top: int = 5) -> None:
        self.patterns = patterns
        self.top = int(top)
        self.stats = {"recorded": 0, "recalled": 0}

    def available(self) -> bool:
        return self.patterns is not None

    def record(self, sequence: Iterable[str]) -> int:
        if not self.available():
            return 0
        try:
            added = int(self.patterns.record([str(item) for item in sequence if item]))
        except Exception as exc:  # noqa: BLE001
            _log.warning("pattern record failed: %s", exc)
            return 0
        self.stats["recorded"] += added
        return added

    def successors(self, tool: str) -> dict[str, int]:
        if not self.available():
            return {}
        try:
            return dict(self.patterns.successors(str(tool)))
        except Exception as exc:  # noqa: BLE001
            _log.warning("pattern read failed: %s", exc)
            return {}

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        """When the query mentions a known tool, report what usually follows it."""
        if not self.available() or not str(query or "").strip():
            return []
        try:
            known = set(self.patterns.snapshot())
        except Exception as exc:  # noqa: BLE001
            _log.warning("pattern snapshot failed: %s", exc)
            return []
        tokens = _tokens(query)
        lowered = str(query).lower()
        # Tool names are dotted or snake_case ("browser.open", "set_volume"), so plain
        # token equality would never match one. Accept the literal name, or enough of the
        # name's own tokens — and keep the three best-known tools so hits stay bounded.
        mentioned = sorted(
            (tool for tool in known if tool and _mentions_tool(tool, lowered, tokens)),
            key=lambda tool: -sum(self.successors(tool).values()),
        )[:3]
        hits: list[MemoryHit] = []
        for tool in mentioned:
            followers = self.successors(tool)
            for name, count in sorted(followers.items(), key=lambda item: item[1], reverse=True)[: self.top]:
                total = sum(followers.values()) or 1
                hits.append(
                    MemoryHit(
                        text=f"after '{tool}' Star usually runs '{name}' ({count} of {total} times)",
                        layer=self.name,
                        kind="pattern",
                        score=_clamp(0.3 + 0.5 * (count / total)),
                        key=tool,
                        value=name,
                    )
                )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        self.stats["recalled"] += len(hits[:limit])
        return hits[: max(0, limit)]

    def top_pairs(self, *, limit: int = 8) -> list[dict[str, Any]]:
        if not self.available():
            return []
        try:
            snapshot = self.patterns.snapshot()
        except Exception:  # noqa: BLE001
            return []
        pairs = [
            {"from": first, "to": second, "count": int(count)}
            for first, followers in snapshot.items()
            for second, count in followers.items()
        ]
        pairs.sort(key=lambda pair: pair["count"], reverse=True)
        return pairs[: max(0, limit)]

    def count(self) -> int:
        if not self.available():
            return 0
        try:
            return int(self.patterns.total())
        except Exception:  # noqa: BLE001
            return 0

    def describe(self) -> dict[str, Any]:
        return {
            "count": self.count(),
            "store": "Backend.star.brain.prediction.PatternStore (JSONL tool→tool counters)",
            "detail": f"{self.stats['recorded']} pair(s) recorded here · read-only in Phase 8; Phase 9 validates candidates",
        }


# ── shared helpers ────────────────────────────────────────────────────────────


_JSON_SAFE = (str, int, float, bool, type(None))


def _json_params(params: Any) -> dict[str, Any]:
    """Episodes are written as JSONL — keep parameters serialisable, honestly truncated."""
    if not isinstance(params, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, value in list(params.items())[:20]:
        if isinstance(value, _JSON_SAFE):
            safe[str(key)[:40]] = value
        elif isinstance(value, (list, tuple)):
            safe[str(key)[:40]] = [item if isinstance(item, _JSON_SAFE) else str(item)[:120] for item in value[:10]]
        elif isinstance(value, dict):
            safe[str(key)[:40]] = {str(k)[:40]: str(v)[:120] for k, v in list(value.items())[:10]}
        else:
            safe[str(key)[:40]] = str(value)[:200]
    return safe


def _kind_value(record: Any) -> str:
    kind = getattr(record, "kind", None)
    return str(getattr(kind, "value", kind) or "fact")


def _record_brief(record: Any) -> dict[str, Any]:
    return {
        "memory_id": int(getattr(record, "memory_id", 0) or 0),
        "text": str(getattr(record, "text", ""))[:200],
        "kind": _kind_value(record),
        "key": getattr(record, "key", None),
        "value": getattr(record, "value", None),
        "timestamp": record.timestamp.isoformat(timespec="seconds") if getattr(record, "timestamp", None) else None,
    }
