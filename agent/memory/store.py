"""Local-first SQLite memory. SQL for recency, cosine for meaning."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Self

from agent.core.clock import DEFAULT_CLOCK, Clock
from agent.core.enums import MemoryKind
from agent.core.ids import EpisodeId, MemoryId, SkillName
from agent.core.metrics import METRICS
from agent.memory.embed import Embedding, EmbeddingBackend, HashingEmbedder
from agent.memory.extract import extract_from_goal, extract_from_skill
from agent.memory.types import MemoryRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT,
    value TEXT,
    timestamp REAL NOT NULL,
    embedding TEXT NOT NULL,
    source_episode TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(key);
CREATE INDEX IF NOT EXISTS idx_memories_ts ON memories(timestamp);
"""


def _cosine(left: Embedding, right: Embedding) -> float:
    if not left or not right:
        return 0.0
    n = min(len(left), len(right))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        a = left[i]
        b = right[i]
        dot += a * b
        na += a * a
        nb += b * b
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom


class MemoryStore:
    """Long-term brain. Survives process restarts; never trains a model."""

    def __init__(
        self,
        path: Path | str = Path("star_memory.db"),
        *,
        embedder: EmbeddingBackend | None = None,
        clock: Clock = DEFAULT_CLOCK,
        owner: str = "user",
    ) -> None:
        self.path = Path(path)
        self.embedder = embedder or HashingEmbedder()
        self.clock = clock
        self.owner = owner
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, tb)
        self.close()

    def remember(
        self,
        text: str,
        *,
        kind: MemoryKind = MemoryKind.EPISODE,
        key: str | None = None,
        value: str | None = None,
        source_episode: EpisodeId | None = None,
    ) -> MemoryRecord:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("memory text must be non-empty")
        if key is not None:
            existing = self.latest_by_key(key)
            if existing is not None and existing.value == value and existing.text == cleaned:
                return existing
        else:
            # FIX: keyless facts ("amar pochondo coffee") used to be stored
            # again on EVERY mention — duplicates then flooded recall's top_k
            # and pushed newer facts out of the results entirely.
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT id, text, kind, key, value, timestamp, embedding, source_episode
                    FROM memories WHERE text = ? AND kind = ?
                    ORDER BY timestamp DESC LIMIT 1
                    """,
                    (cleaned, kind.value),
                ).fetchone()
            if row is not None:
                return self._row(row)
        embedding = self.embedder.encode(cleaned)
        ts = self.clock.now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO memories (text, kind, key, value, timestamp, embedding, source_episode)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cleaned,
                    kind.value,
                    key,
                    value,
                    ts.timestamp(),
                    json.dumps(list(embedding)),
                    str(source_episode) if source_episode is not None else None,
                ),
            )
            self._conn.commit()
            row_id = int(cursor.lastrowid or 0)
        METRICS.inc("memory.stored", kind=kind.value)
        return MemoryRecord(
            memory_id=MemoryId(row_id),
            text=cleaned,
            kind=kind,
            timestamp=ts,
            key=key,
            value=value,
            source_episode=source_episode,
            embedding=embedding,
        )

    def recall_recent(self, n: int = 10) -> list[MemoryRecord]:
        if n < 1:
            raise ValueError("n must be >= 1")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, text, kind, key, value, timestamp, embedding, source_episode
                FROM memories ORDER BY timestamp DESC LIMIT ?
                """,
                (n,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def recall_relevant(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        q_emb = self.embedder.encode(query)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, text, kind, key, value, timestamp, embedding, source_episode
                FROM memories
                """
            ).fetchall()
        scored: list[MemoryRecord] = []
        tokens = {tok for tok in query.lower().split() if len(tok) > 2}
        for row in rows:
            record = self._row(row)
            emb = record.embedding
            score = _cosine(q_emb, emb) if emb is not None else 0.0
            hay = f"{record.text} {record.key or ''} {record.value or ''}".lower()
            boost = 0.2 * sum(1.0 for tok in tokens if tok in hay)
            scored.append(record.model_copy(update={"score": min(1.0, score + boost)}))
        scored.sort(key=lambda rec: rec.score or 0.0, reverse=True)
        # FIX: collapse duplicate texts (legacy rows may repeat) so a single
        # fact cannot occupy multiple top_k slots.
        seen_texts: set[str] = set()
        deduped: list[MemoryRecord] = []
        for rec in scored:
            sig = rec.text.strip().lower()
            if sig in seen_texts:
                continue
            seen_texts.add(sig)
            deduped.append(rec)
        METRICS.observe("memory.recall", float(len(rows)))
        return deduped[:top_k]

    def latest_by_key(self, key: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, text, kind, key, value, timestamp, embedding, source_episode
                FROM memories WHERE key = ? ORDER BY timestamp DESC LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return self._row(row)

    def preferred_int(self, key: str, default: int) -> int:
        rec = self.latest_by_key(key)
        if rec is None or rec.value is None:
            return default
        try:
            return int(rec.value)
        except ValueError:
            return default

    def preferred_str(self, key: str, default: str) -> str:
        rec = self.latest_by_key(key)
        if rec is None or rec.value is None or not rec.value.strip():
            return default
        return rec.value

    def forget(self, memory_id: MemoryId) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
            self._conn.commit()
            return cur.rowcount > 0

    def build_context_block(self, query: str, *, top_k: int = 5) -> str:
        relevant = [m for m in self.recall_relevant(query, top_k=top_k) if (m.score or 0) > 0.05]
        if not relevant:
            return ""
        lines = [f"Relevant things you know about {self.owner}:"]
        lines.extend(f"- {m.text}" for m in relevant)
        return "\n".join(lines)

    def learn_from_goal(self, goal: str, *, episode_id: EpisodeId | None = None) -> int:
        written = 0
        for pref in extract_from_goal(goal):
            self.remember(
                pref.text,
                kind=pref.kind,
                key=pref.key,
                value=pref.value,
                source_episode=episode_id,
            )
            written += 1
        return written

    def learn_from_skill(
        self,
        skill: SkillName,
        params: dict[str, object],
        *,
        episode_id: EpisodeId | None = None,
    ) -> int:
        written = 0
        for pref in extract_from_skill(skill, params):
            self.remember(
                pref.text,
                kind=pref.kind,
                key=pref.key,
                value=pref.value,
                source_episode=episode_id,
            )
            written += 1
        return written

    def ingest_episode(self, episode: Episode) -> None:
        summary = (
            f"Episode {episode.episode_id}: goal={episode.goal!r} "
            f"state={episode.final_state.value} steps={len(episode.steps)}"
        )
        self.remember(
            summary,
            kind=MemoryKind.EPISODE,
            source_episode=episode.episode_id,
        )
        self.learn_from_goal(episode.goal, episode_id=episode.episode_id)
        if episode.final_state.value != "succeeded":
            return
        for step in episode.steps:
            if step.success and step.skill is not None:
                self.learn_from_skill(
                    step.skill, dict(step.params), episode_id=episode.episode_id
                )

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return int(row[0]) if row else 0

    def _row(self, row: tuple[object, ...]) -> MemoryRecord:
        raw_emb = row[6]
        embedding: Embedding | None
        if isinstance(raw_emb, str):
            embedding = tuple(float(x) for x in json.loads(raw_emb))
        else:
            embedding = None
        source = row[7]
        return MemoryRecord(
            memory_id=MemoryId(int(row[0])),  # type: ignore[arg-type]
            text=str(row[1]),
            kind=MemoryKind(str(row[2])),
            key=str(row[3]) if row[3] is not None else None,
            value=str(row[4]) if row[4] is not None else None,
            timestamp=datetime.fromtimestamp(float(row[5]), tz=timezone.utc),
            embedding=embedding,
            source_episode=EpisodeId(str(source)) if source is not None else None,
        )
