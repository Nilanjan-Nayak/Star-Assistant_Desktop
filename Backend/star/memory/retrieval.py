"""Hybrid retrieval — one ranked answer from five very different stores.

The layers do not agree on what a score means: the semantic layer returns an
embedding cosine, the episodic layer a token overlap, the preference layer a
key/text match, the pattern layer a conditional frequency, and working memory a
recency-weighted overlap. Blending raw numbers would be meaningless, so:

1. every layer is asked in parallel (``asyncio.to_thread`` + a per-layer timeout,
   because SQLite and JSONL reads are blocking);
2. each candidate's relevance is clamped to ``[0, 1]`` and multiplied by that
   layer's weight (:class:`MemorySettings`) — the weight is the *only* place where
   one layer is allowed to outrank another, and it is visible in ``describe()``;
3. duplicates (same normalised text) collapse, keeping the best score;
4. a per-layer cap keeps one loud layer from monopolising the answer, so the brain
   always sees a mix of "what is happening now", "what the user likes" and "what
   is known";
5. the trace (candidates per layer, duplicates dropped, timeouts) is returned so
   the manager can publish an honest ``memory.retrieved`` event.

Nothing here writes anything: retrieval is read-only by construction.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Iterable

from Backend.star.brain.schemas import MemoryHit
from Backend.star.observability.logging import star_logger

__all__ = ["RetrievalTrace", "blend", "hybrid_retrieve"]

_log = star_logger("star2.memory.retrieval")


class RetrievalTrace(dict):
    """A dict that also reads nicely in logs/events (candidates, dupes, timeouts)."""

    @property
    def candidates(self) -> int:
        return int(self.get("candidates", 0))

    @property
    def returned(self) -> int:
        return int(self.get("returned", 0))


def _normalise(text: str) -> str:
    return " ".join("".join(char if char.isalnum() else " " for char in str(text or "").lower()).split())[:200]


def blend(
    candidates: Iterable[tuple[str, MemoryHit]],
    *,
    weights: dict[str, float],
    limit: int = 8,
    per_layer_cap: int | None = None,
) -> list[MemoryHit]:
    """Weight, dedupe, cap and rank. ``candidates`` are ``(layer, hit)`` pairs."""
    best: dict[str, MemoryHit] = {}
    for layer, hit in candidates:
        relevance = max(0.0, min(1.0, float(hit.score or 0.0)))
        weight = float(weights.get(layer, weights.get(hit.layer, 0.5)))
        score = max(-1.0, min(1.0, round(relevance * weight, 4)))
        ranked = hit.model_copy(update={"score": score, "layer": hit.layer or layer})
        key = _normalise(ranked.text) or f"{ranked.layer}:{ranked.key or ''}:{ranked.value or ''}"
        current = best.get(key)
        if current is None or ranked.score > current.score:
            best[key] = ranked

    ordered = sorted(best.values(), key=lambda hit: (-hit.score, hit.layer))
    cap = per_layer_cap if per_layer_cap is not None else max(1, math.ceil(limit / 2))
    taken: dict[str, int] = {}
    out: list[MemoryHit] = []
    deferred: list[MemoryHit] = []
    for hit in ordered:
        used = taken.get(hit.layer, 0)
        if used >= cap:
            deferred.append(hit)
            continue
        taken[hit.layer] = used + 1
        out.append(hit)
        if len(out) >= limit:
            break
    if len(out) < limit:                      # caps must not waste good candidates
        for hit in deferred:
            if len(out) >= limit:
                break
            out.append(hit)
    return out


async def hybrid_retrieve(
    layers: dict[str, Any],
    query: str,
    *,
    limit: int = 8,
    session_id: str = "",
    weights: dict[str, float] | None = None,
    per_layer: int = 5,
    timeout_s: float = 4.0,
) -> tuple[list[MemoryHit], RetrievalTrace]:
    """Ask every layer, blend the answers. Never raises: a dead layer is reported."""
    text = str(query or "").strip()
    weights = weights or {}
    trace = RetrievalTrace(candidates=0, per_layer={}, duplicates=0, timeouts=[], errors=[], query=text[:160])
    if not text:
        trace["returned"] = 0
        return [], trace

    async def ask(name: str, layer: Any) -> tuple[str, list[MemoryHit]]:
        recall = getattr(layer, "recall", None)
        if recall is None:
            return name, []
        kwargs: dict[str, Any] = {"limit": per_layer}
        if name == "working":
            kwargs["session_id"] = session_id
        try:
            return name, list(await asyncio.wait_for(asyncio.to_thread(recall, text, **kwargs), timeout=timeout_s))
        except asyncio.TimeoutError:
            trace["timeouts"].append(name)
            _log.warning("memory layer %s timed out after %.1fs", name, timeout_s)
            return name, []
        except Exception as exc:  # noqa: BLE001 — one dead layer must not kill retrieval
            trace["errors"].append(f"{name}: {type(exc).__name__}: {exc}"[:160])
            _log.warning("memory layer %s failed: %s", name, exc)
            return name, []

    results = await asyncio.gather(*(ask(name, layer) for name, layer in layers.items()))

    candidates: list[tuple[str, MemoryHit]] = []
    seen: set[str] = set()
    for name, hits in results:
        trace["per_layer"][name] = len(hits)
        trace["candidates"] += len(hits)
        for hit in hits:
            key = _normalise(hit.text)
            if key and key in seen:
                trace["duplicates"] += 1
            seen.add(key)
            candidates.append((name, hit))

    ranked = blend(candidates, weights=weights, limit=limit)
    trace["returned"] = len(ranked)
    trace["layers"] = sorted({hit.layer for hit in ranked})
    return ranked, trace
