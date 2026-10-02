"""Text-to-speech providers.

The real synthesis is the **existing** ``Backend/voice/tts.VoiceEngine``
(edge-tts neural voices with a bn/en fallback chain, mp3 cache, SAPI5 offline
fallback). This module only adapts it to the async ``TTSProvider`` contract — the
engine is called through :func:`asyncio.to_thread` because it internally runs
``asyncio.run`` and must never be invoked on the gateway's loop thread.
"""

from __future__ import annotations

import asyncio
import math
import struct
from pathlib import Path
from typing import Any

from Backend.star.voice.base import SynthesisResult
from Backend.star.voice.language import detect_language, tts_voice_for, voice_chain

__all__ = ["EdgeTTSAdapter", "NullTTS", "ScriptedTTS", "build_tts", "sine_wav"]


def sine_wav(duration_ms: float = 220.0, frequency: float = 440.0, *, sample_rate: int = 16000, volume: float = 0.25) -> bytes:
    """Pure-stdlib mono 16-bit PCM WAV — used by the test/offline TTS double."""
    samples = int(max(1, sample_rate * duration_ms / 1000.0))
    frames = bytearray()
    for index in range(samples):
        envelope = min(1.0, index / 200.0) * min(1.0, (samples - index) / 200.0)
        value = int(32767 * volume * envelope * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames += struct.pack("<h", max(-32768, min(32767, value)))
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(frames),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(frames),
    )
    return bytes(header) + bytes(frames)


class NullTTS:
    """Text-only mode: reports ``ok=False`` with no audio (HUD shows text)."""

    name = "null"

    def available(self) -> bool:
        return True

    def supports(self, language: str) -> bool:
        return language in {"bn", "en", "mixed"}

    async def synthesize(self, text: str, *, language: str = "bn", voice: str | None = None) -> SynthesisResult:
        return SynthesisResult(
            ok=False,
            text=text,
            language=language,
            voice=voice or "",
            provider=self.name,
            error="tts disabled",
        )


class ScriptedTTS:
    """Deterministic TTS double: returns a short sine WAV. Used by tests/CI."""

    name = "scripted"

    def __init__(self, *, duration_ms: float = 200.0) -> None:
        self.duration_ms = duration_ms
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def supports(self, language: str) -> bool:
        return True

    async def synthesize(self, text: str, *, language: str = "bn", voice: str | None = None) -> SynthesisResult:
        resolved_language = language or detect_language(text).code
        chosen = voice or tts_voice_for(resolved_language)
        self.calls.append((text, chosen))
        audio = sine_wav(self.duration_ms, frequency=520.0 if resolved_language == "bn" else 440.0)
        return SynthesisResult(
            ok=True,
            text=text,
            language=resolved_language,
            voice=chosen,
            provider=self.name,
            audio_bytes=audio,
            mime="audio/wav",
            duration_ms=self.duration_ms,
        )


class EdgeTTSAdapter:
    """Adapter over the existing ``Backend.voice.tts.VoiceEngine`` (edge-tts)."""

    name = "edge"

    def __init__(self, *, default_voice: str = "", rate: str = "", pitch: str = "", cache_dir: Path | str | None = None) -> None:
        self.default_voice = default_voice
        self.rate = rate
        self.pitch = pitch
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._engine: Any = None
        self._failed = False

    def _load(self) -> Any:
        if self._engine is not None or self._failed:
            return self._engine
        try:
            from Backend.voice.tts import VoiceEngine, clean_for_speech  # reuse, don't reimplement

            engine = VoiceEngine(self.default_voice) if self.default_voice else VoiceEngine()
            if self.rate:
                engine.rate = self.rate
            if self.pitch:
                engine.pitch = self.pitch
            if self.cache_dir is not None:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                engine.cache_dir = self.cache_dir
            self._engine = (engine, clean_for_speech)
        except Exception:  # noqa: BLE001 — edge-tts/pygame stack missing
            self._failed = True
            self._engine = None
        return self._engine

    def available(self) -> bool:
        loaded = self._load()
        if loaded is None:
            return False
        try:
            import edge_tts  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def supports(self, language: str) -> bool:
        return language in {"bn", "en", "mixed"}

    async def synthesize(self, text: str, *, language: str = "bn", voice: str | None = None) -> SynthesisResult:
        loaded = self._load()
        if loaded is None:
            return SynthesisResult(ok=False, text=text, language=language, provider=self.name, error="engine unavailable")
        engine, clean_for_speech = loaded
        resolved = language or detect_language(text).code
        chosen = voice or tts_voice_for(resolved, preferred=self.default_voice)
        spoken = clean_for_speech(text)
        if not spoken:
            return SynthesisResult(ok=False, text=text, language=resolved, voice=chosen, provider=self.name, error="empty after cleaning")
        try:
            engine.voice = chosen
            path = await asyncio.to_thread(engine.synthesize, spoken)
        except Exception as exc:  # noqa: BLE001
            return SynthesisResult(
                ok=False, text=text, language=resolved, voice=chosen, provider=self.name, error=repr(exc)
            )
        if not path:
            return SynthesisResult(
                ok=False, text=text, language=resolved, voice=chosen, provider=self.name, error="no audio produced"
            )
        audio_file = Path(path)
        return SynthesisResult(
            ok=True,
            text=spoken,
            language=resolved,
            voice=chosen,
            provider=self.name,
            audio_path=str(audio_file),
            mime="audio/mpeg",
            cached=audio_file.exists(),
        )

    def voice_chain(self, language: str) -> tuple[str, ...]:
        return voice_chain(language)


def build_tts(mode: str = "auto", *, default_voice: str = "", cache_dir: Path | str | None = None) -> Any:
    mode = (mode or "auto").lower()
    if mode == "null":
        return NullTTS()
    if mode in {"scripted", "test"}:
        return ScriptedTTS()
    engine = EdgeTTSAdapter(default_voice=default_voice, cache_dir=cache_dir)
    if mode == "edge":
        return engine
    # auto: real neural voice when edge-tts is installed, otherwise a silent no-op
    return _TTSCascade([engine, NullTTS()])


class _TTSCascade:
    """First provider that actually produces audio wins."""

    name = "cascade"

    def __init__(self, providers: list[Any]) -> None:
        self.providers = providers

    def available(self) -> bool:
        return any(provider.available() for provider in self.providers)

    def supports(self, language: str) -> bool:
        return any(provider.supports(language) for provider in self.providers)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": getattr(p, "name", type(p).__name__), "available": p.available()} for p in self.providers
        ]

    async def synthesize(self, text: str, *, language: str = "bn", voice: str | None = None) -> SynthesisResult:
        last: SynthesisResult | None = None
        for provider in self.providers:
            if not provider.available():
                continue
            try:
                result = await provider.synthesize(text, language=language, voice=voice)
            except Exception as exc:  # noqa: BLE001
                last = SynthesisResult(ok=False, text=text, language=language, provider=getattr(provider, "name", "?"), error=repr(exc))
                continue
            last = result
            if result.ok:
                return result
        return last or SynthesisResult(ok=False, text=text, language=language, provider=self.name, error="no provider")
