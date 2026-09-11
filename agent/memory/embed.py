"""Embedding backends. Real models are optional; hashing works offline."""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

from agent.core.errors import BackendUnavailable, ConfigurationError

type Embedding = tuple[float, ...]


@runtime_checkable
class EmbeddingBackend(Protocol):
    dim: int

    def encode(self, text: str) -> Embedding: ...


def _l2_normalize(vec: list[float]) -> Embedding:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return tuple(v / norm for v in vec)


class HashingEmbedder:
    """Signed hashing-trick embedder. No extra deps, deterministic, offline.

    Not as good as MiniLM, but ``volume 40`` and ``volume 70`` already
    separate, and cosine ranking is meaningful for short preference strings.
    """

    def __init__(self, dim: int = 128) -> None:
        if dim < 8:
            raise ConfigurationError("embedding dim must be >= 8", dim=dim)
        self.dim = dim

    def encode(self, text: str) -> Embedding:
        vec = [0.0] * self.dim
        blob = text.lower().strip()
        if not blob:
            return tuple(vec)
        self._accumulate(vec, blob)
        for token in blob.split():
            self._accumulate(vec, token)
        return _l2_normalize(vec)

    def _accumulate(self, vec: list[float], token: str) -> None:
        h = 2166136261
        for char in token:
            h ^= ord(char)
            h = (h * 16777619) & 0xFFFFFFFF
        index = h % self.dim
        sign = 1.0 if (h >> 1) & 1 else -1.0
        vec[index] += sign
        vec[(h >> 8) % self.dim] += 0.5 * sign


class SentenceTransformerEmbedder:
    """Optional real model: ``pip install sentence-transformers``."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise BackendUnavailable(
                "sentence-transformers is required for this embedder"
            ) from exc
        self._model = SentenceTransformer(model_name)
        dim = int(self._model.get_sentence_embedding_dimension())
        self.dim = dim

    def encode(self, text: str) -> Embedding:
        vector = self._model.encode(text)
        return tuple(float(x) for x in vector.tolist())
