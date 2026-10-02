"""Voice contracts — the seams every STT/TTS implementation plugs into.

Blueprint §7 Phase 2: "define ``STTProvider`` and ``TTSProvider`` interfaces; add
Bengali/English language routing; support interruption/barge-in; keep speaker
verification separate from STT; make transcript retention configurable."
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AudioInput",
    "STTProvider",
    "SynthesisResult",
    "TTSProvider",
    "Transcript",
]


class AudioInput(BaseModel):
    """A chunk of captured audio. Never persisted unless retention says so."""

    model_config = ConfigDict(extra="forbid")

    data: bytes = b""
    format: str = "wav"                    # wav | pcm16 | mp3 | webm | ogg
    sample_rate: int = Field(default=16000, ge=1000, le=192000)
    channels: int = Field(default=1, ge=1, le=8)
    language_hint: str | None = None       # "bn" | "en" | "bn-IN" | "en-IN" | None
    duration_ms: float = Field(default=0.0, ge=0.0)
    source: str = "microphone"             # microphone | file | browser | test

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.data).hexdigest()[:16] if self.data else "empty"


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    language: str = "en"                   # resolved BCP-47-ish code: bn / en / mixed
    script: str = "latin"                  # bengali | latin | mixed
    banglish: bool = False                 # romanised Bengali ("volume barao koro")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    provider: str = "null"
    is_final: bool = True
    speaker_id: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    audio_ref: str | None = None           # path/fingerprint, never the bytes
    alternatives: tuple[str, ...] = ()     # e.g. the rejected bn/en candidate

    def redacted(self) -> "Transcript":
        """Retention-safe copy: keeps the shape, drops the words."""
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]
        return self.model_copy(update={"text": f"<redacted:{digest}>", "alternatives": ()})


class SynthesisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = False
    text: str = ""
    language: str = "bn"
    voice: str = ""
    provider: str = "null"
    audio_path: str | None = None
    audio_bytes: bytes | None = None
    mime: str = "audio/mpeg"
    duration_ms: float = Field(default=0.0, ge=0.0)
    cached: bool = False
    error: str | None = None

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"audio_bytes"})
        if self.audio_bytes:
            data["audio_bytes_len"] = len(self.audio_bytes)
        return data


@runtime_checkable
class STTProvider(Protocol):
    """Speech → text. Implementations must be safe to call from the event loop."""

    name: str

    def available(self) -> bool: ...

    def supports(self, language: str) -> bool: ...

    async def transcribe(self, audio: AudioInput) -> Transcript | None: ...


@runtime_checkable
class TTSProvider(Protocol):
    """Text → speech audio."""

    name: str

    def available(self) -> bool: ...

    def supports(self, language: str) -> bool: ...

    async def synthesize(self, text: str, *, language: str = "bn", voice: str | None = None) -> SynthesisResult: ...
