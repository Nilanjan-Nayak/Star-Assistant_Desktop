"""Speaker verification — a **seam**, deliberately separate from STT.

Blueprint §7: "keep speaker verification separate from STT". Nothing here is a
security control: identity for *permissions* lives in
``Backend/star/security/identity.py``. This module answers one narrow question —
"does this audio look like the enrolled owner?" — so Star can, for example,
ignore a stranger's voice for HIGH/CRITICAL actions or personalise a greeting.

The default implementation is intentionally simple and dependency-optional
(numpy): PCM16 → frame energy + zero-crossing rate + spectral centroid → cosine
similarity against an enrolled profile. Swap in a real speaker-embedding model
behind the same protocol when needed.
"""

from __future__ import annotations

import math
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from Backend.star.voice.base import AudioInput

__all__ = ["NullSpeakerVerifier", "NumpySpeakerVerifier", "SpeakerMatch", "SpeakerVerifier"]


class SpeakerMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    speaker_id: str | None = None
    score: float = Field(default=0.0, ge=-1.0, le=1.0)
    accepted: bool = False
    enrolled: bool = False
    method: str = "null"
    reason: str = ""


@runtime_checkable
class SpeakerVerifier(Protocol):
    name: str

    def available(self) -> bool: ...

    def enrol(self, speaker_id: str, audio: AudioInput) -> bool: ...

    def verify(self, audio: AudioInput, *, speaker_id: str | None = None) -> SpeakerMatch: ...

    def forget(self, speaker_id: str) -> bool: ...

    def describe(self) -> dict[str, Any]: ...


class NullSpeakerVerifier:
    """Verification disabled — everything is 'unknown', nothing is rejected."""

    name = "null"

    def available(self) -> bool:
        return True

    def enrol(self, speaker_id: str, audio: AudioInput) -> bool:
        _ = (speaker_id, audio)
        return False

    def verify(self, audio: AudioInput, *, speaker_id: str | None = None) -> SpeakerMatch:
        _ = (audio, speaker_id)
        return SpeakerMatch(accepted=False, enrolled=False, method=self.name, reason="verification disabled")

    def forget(self, speaker_id: str) -> bool:
        _ = speaker_id
        return False

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "available": True, "enrolled": 0}


def _pcm16_from_wav(data: bytes) -> tuple[list[int], int]:
    """Minimal RIFF/WAVE parser (PCM16 mono/stereo → mono). No dependencies."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return [], 16000
    offset = 12
    sample_rate = 16000
    channels = 1
    bits = 16
    audio_bytes = b""
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        body = data[offset + 8 : offset + 8 + size]
        if chunk_id == b"fmt ":
            if len(body) >= 8:
                channels = int.from_bytes(body[2:4], "little") or 1
                sample_rate = int.from_bytes(body[4:8], "little") or 16000
                bits = int.from_bytes(body[14:16], "little") if len(body) >= 16 else 16
        elif chunk_id == b"data":
            audio_bytes = body
            break
        offset += 8 + size + (size % 2)
    if bits != 16 or not audio_bytes:
        return [], sample_rate
    import struct

    count = len(audio_bytes) // 2
    samples = list(struct.unpack(f"<{count}h", audio_bytes[: count * 2]))
    if channels > 1:
        samples = [
            sum(samples[i : i + channels]) // channels for i in range(0, len(samples) - channels + 1, channels)
        ]
    return samples, sample_rate


class NumpySpeakerVerifier:
    """Heuristic voice-profile matcher (energy/ZCR/centroid over frames)."""

    name = "profile"

    def __init__(self, *, threshold: float = 0.82, frame_ms: float = 25.0, min_frames: int = 8) -> None:
        self.threshold = threshold
        self.frame_ms = frame_ms
        self.min_frames = min_frames
        self._profiles: dict[str, list[float]] = {}
        self._numpy: Any = None

    # ── availability ────────────────────────────────────────────────────────
    def _np(self) -> Any:
        if self._numpy is None:
            try:
                import numpy as np  # type: ignore[import-not-found]

                self._numpy = np
            except Exception:  # noqa: BLE001
                self._numpy = False
        return self._numpy or None

    def available(self) -> bool:
        return self._np() is not None

    # ── features ────────────────────────────────────────────────────────────
    def features(self, audio: AudioInput) -> list[float] | None:
        np = self._np()
        if np is None:
            return None
        if audio.format == "wav" or audio.data[:4] == b"RIFF":
            samples, sample_rate = _pcm16_from_wav(audio.data)
        else:
            import struct

            count = len(audio.data) // 2
            if count == 0:
                return None
            samples = list(struct.unpack(f"<{count}h", audio.data[: count * 2]))
            sample_rate = audio.sample_rate
        if len(samples) < 512:
            return None
        array = np.asarray(samples, dtype=np.float32) / 32768.0
        frame = max(64, int(sample_rate * self.frame_ms / 1000.0))
        hop = frame // 2
        vectors: list[list[float]] = []
        for start in range(0, max(1, len(array) - frame), hop):
            chunk = array[start : start + frame]
            if len(chunk) < frame:
                break
            energy = float(np.sqrt(np.mean(chunk**2)))
            if energy < 1e-4:
                continue
            zcr = float(np.mean(np.abs(np.diff(np.sign(chunk))) > 0))
            spectrum = np.abs(np.fft.rfft(chunk * np.hanning(len(chunk))))
            freqs = np.fft.rfftfreq(len(chunk), d=1.0 / sample_rate)
            total = float(spectrum.sum())
            centroid = float((spectrum * freqs).sum() / total) if total > 0 else 0.0
            roll = float(freqs[min(len(freqs) - 1, int(np.searchsorted(np.cumsum(spectrum) / max(total, 1e-9), 0.85)))])
            vectors.append([math.log(energy + 1e-9), zcr, centroid / 1000.0, roll / 1000.0])
        if len(vectors) < self.min_frames:
            return None
        matrix = np.asarray(vectors, dtype=np.float64)
        mean = matrix.mean(axis=0)
        std = matrix.std(axis=0)
        return [float(x) for x in np.concatenate([mean, std])]

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        na = math.sqrt(sum(a * a for a in left))
        nb = math.sqrt(sum(b * b for b in right))
        if na == 0 or nb == 0:
            return 0.0
        return max(-1.0, min(1.0, dot / (na * nb)))

    # ── API ─────────────────────────────────────────────────────────────────
    def enrol(self, speaker_id: str, audio: AudioInput) -> bool:
        vector = self.features(audio)
        if vector is None:
            return False
        existing = self._profiles.get(speaker_id)
        self._profiles[speaker_id] = (
            [(a + b) / 2.0 for a, b in zip(existing, vector, strict=False)] if existing else vector
        )
        return True

    def forget(self, speaker_id: str) -> bool:
        return self._profiles.pop(speaker_id, None) is not None

    def verify(self, audio: AudioInput, *, speaker_id: str | None = None) -> SpeakerMatch:
        if not self.available():
            return SpeakerMatch(method=self.name, reason="numpy unavailable")
        if not self._profiles:
            return SpeakerMatch(method=self.name, reason="no enrolled speakers")
        vector = self.features(audio)
        if vector is None:
            return SpeakerMatch(enrolled=True, method=self.name, reason="audio too short for profiling")
        candidates = {speaker_id: self._profiles[speaker_id]} if speaker_id and speaker_id in self._profiles else self._profiles
        best_id, best_score = None, -1.0
        for candidate_id, profile in candidates.items():
            score = self._cosine(profile, vector)
            if score > best_score:
                best_id, best_score = candidate_id, score
        accepted = best_score >= self.threshold
        return SpeakerMatch(
            speaker_id=best_id if accepted else None,
            score=best_score,
            accepted=accepted,
            enrolled=True,
            method=self.name,
            reason="matched" if accepted else f"below threshold {self.threshold}",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available(),
            "enrolled": len(self._profiles),
            "threshold": self.threshold,
            "speakers": sorted(self._profiles),
        }


def build_speaker_verifier(enabled: bool) -> SpeakerVerifier:
    return NumpySpeakerVerifier() if enabled else NullSpeakerVerifier()
