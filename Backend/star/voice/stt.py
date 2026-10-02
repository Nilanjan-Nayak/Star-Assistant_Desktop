"""Speech-to-text providers.

``auto`` picks the first provider that is actually importable/usable on this
machine and degrades in this order:

    whisper (local, offline)  →  google_dual (network, bn-IN + en-IN in parallel)
                              →  browser (Web Speech API transcript handed to us)
                              →  scripted/file (tests, offline dev)
                              →  null

Every provider is async and never blocks the event loop: blocking recognisers run
through :func:`asyncio.to_thread`. The bn/en disambiguation is the pure
:func:`Backend.star.voice.language.prefer_english` heuristic lifted from the
existing ``VoiceListener`` (which stays untouched).
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from Backend.star.voice.base import AudioInput, Transcript
from Backend.star.voice.language import (
    detect_language,
    normalize_text,
    prefer_english,
    stt_locales,
)

__all__ = [
    "BrowserSTT",
    "FileSTT",
    "GoogleDualSTT",
    "NullSTT",
    "STTChain",
    "ScriptedSTT",
    "WhisperSTT",
    "build_stt",
]


def _make_transcript(text: str, *, provider: str, audio: AudioInput, confidence: float = 0.8,
                     alternatives: tuple[str, ...] = ()) -> Transcript:
    profile = detect_language(text)
    return Transcript(
        text=normalize_text(text, strip_wake=False),
        language=profile.code,
        script=profile.script,
        banglish=profile.banglish,
        confidence=confidence,
        provider=provider,
        audio_ref=audio.fingerprint,
        alternatives=alternatives,
    )


class NullSTT:
    """No microphone, no recogniser — always yields ``None`` (never raises)."""

    name = "null"

    def available(self) -> bool:
        return True

    def supports(self, language: str) -> bool:
        return language in {"bn", "en", "mixed"}

    async def transcribe(self, audio: AudioInput) -> Transcript | None:
        _ = audio
        return None


class ScriptedSTT:
    """Deterministic test double: fingerprint/text → transcript."""

    name = "scripted"

    def __init__(self, script: dict[str, str] | None = None, default: str = "") -> None:
        self.script = dict(script or {})
        self.default = default
        self.calls: list[AudioInput] = []

    def available(self) -> bool:
        return True

    def supports(self, language: str) -> bool:
        return True

    def add(self, data: bytes, text: str) -> None:
        self.script[AudioInput(data=data).fingerprint] = text

    async def transcribe(self, audio: AudioInput) -> Transcript | None:
        self.calls.append(audio)
        text = self.script.get(audio.fingerprint) or self.script.get(audio.source) or self.default
        if not text:
            return None
        return _make_transcript(text, provider=self.name, audio=audio, confidence=1.0)


class FileSTT:
    """Reads a sidecar transcript for an audio file (offline dev + tests).

    ``speech.wav`` → ``speech.txt`` (plain) or ``speech.json`` (``{"text": ...}``).
    ``AudioInput.data`` may also be a UTF-8 path when ``source == 'file'``.
    """

    name = "file"

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else None

    def available(self) -> bool:
        return True

    def supports(self, language: str) -> bool:
        return True

    async def transcribe(self, audio: AudioInput) -> Transcript | None:
        candidate = await asyncio.to_thread(self._locate, audio)
        if candidate is None:
            return None
        return _make_transcript(candidate, provider=self.name, audio=audio, confidence=0.9)

    def _locate(self, audio: AudioInput) -> str | None:
        paths: list[Path] = []
        if audio.source == "file" and audio.data:
            try:
                paths.append(Path(audio.data.decode("utf-8", errors="strict")))
            except UnicodeDecodeError:
                pass
        for path in paths:
            if self.root is not None and not path.is_absolute():
                path = self.root / path
            for suffix in (".txt", ".json"):
                sidecar = path.with_suffix(suffix)
                if sidecar.is_file():
                    raw = sidecar.read_text(encoding="utf-8").strip()
                    if suffix == ".json":
                        try:
                            return str(json.loads(raw).get("text") or "").strip() or None
                        except json.JSONDecodeError:
                            return None
                    return raw or None
        return None


class BrowserSTT:
    """Accepts a transcript produced by the browser's Web Speech API.

    The payload travels as UTF-8 text (or JSON ``{"text": ...}``) inside
    ``AudioInput.data`` — no audio decoding happens server-side, so nothing is
    recorded and retention stays trivially satisfiable.
    """

    name = "browser"

    def available(self) -> bool:
        return True

    def supports(self, language: str) -> bool:
        return language in {"bn", "en", "mixed", "bn-IN", "en-IN"}

    async def transcribe(self, audio: AudioInput) -> Transcript | None:
        if not audio.data:
            return None
        try:
            raw = audio.data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        text = raw.strip()
        if text.startswith("{"):
            try:
                text = str(json.loads(text).get("text") or "").strip()
            except json.JSONDecodeError:
                return None
        if not text:
            return None
        return _make_transcript(text, provider=self.name, audio=audio, confidence=0.85)


class GoogleDualSTT:
    """``speech_recognition`` + Google Web Speech, bn-IN and en-IN in parallel.

    Mirrors the proven behaviour of ``Backend/bridge.py::VoiceListener`` without
    importing Qt: two recognitions run concurrently, then
    :func:`prefer_english` decides which transcript is the real one.
    """

    name = "google_dual"

    def __init__(self, *, locales: tuple[str, ...] = ("bn-IN", "en-IN"), timeout: float = 12.0) -> None:
        self.locales = locales
        self.timeout = timeout
        self._sr: Any = None

    def _module(self) -> Any:
        if self._sr is None:
            try:
                import speech_recognition as sr  # type: ignore[import-not-found]

                self._sr = sr
            except Exception:  # noqa: BLE001 — optional dependency
                self._sr = False
        return self._sr or None

    def available(self) -> bool:
        return self._module() is not None

    def supports(self, language: str) -> bool:
        return language in {"bn", "en", "mixed", "bn-IN", "en-IN"}

    async def transcribe(self, audio: AudioInput) -> Transcript | None:
        sr = self._module()
        if sr is None or not audio.data:
            return None
        results = await asyncio.wait_for(
            asyncio.gather(*(asyncio.to_thread(self._recognize, sr, audio, locale) for locale in self.locales)),
            timeout=self.timeout + 2,
        )
        by_locale = dict(zip(self.locales, results, strict=False))
        bn_text = str(by_locale.get("bn-IN") or "").strip()
        en_text = str(by_locale.get("en-IN") or "").strip()
        if bn_text and en_text:
            chosen, rejected = (en_text, bn_text) if prefer_english(en_text, bn_text) else (bn_text, en_text)
        else:
            chosen, rejected = (bn_text or en_text, en_text if bn_text else bn_text)
        if not chosen:
            return None
        return _make_transcript(
            chosen,
            provider=self.name,
            audio=audio,
            confidence=0.75,
            alternatives=(rejected,) if rejected else (),
        )

    def _recognize(self, sr: Any, audio: AudioInput, locale: str) -> str | None:
        import io

        recognizer = sr.Recognizer()
        try:
            with io.BytesIO(audio.data) as handle:
                source = sr.AudioFile(handle)
                with source as opened:
                    recorded = recognizer.record(opened)
            return str(recognizer.recognize_google(recorded, language=locale) or "").strip()
        except Exception:  # noqa: BLE001 — one locale failing must not kill the other
            return None


class WhisperSTT:
    """Local offline STT via ``faster-whisper`` (preferred) or ``openai-whisper``."""

    name = "whisper"

    def __init__(self, model_size: str = "small", *, device: str = "cpu", compute_type: str = "int8") -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model: Any = None
        self._failed = False

    def _load(self) -> Any:
        if self._model is not None or self._failed:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]

            self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        except Exception:  # noqa: BLE001
            try:
                import whisper  # type: ignore[import-not-found]

                self._model = whisper.load_model(self.model_size)
                self._model._star_backend = "openai-whisper"  # noqa: SLF001
            except Exception:  # noqa: BLE001
                self._failed = True
                self._model = None
        return self._model

    def available(self) -> bool:
        return self._load() is not None

    def supports(self, language: str) -> bool:
        return language in {"bn", "en", "mixed"}

    async def transcribe(self, audio: AudioInput) -> Transcript | None:
        model = self._load()
        if model is None or not audio.data:
            return None
        hint = (audio.language_hint or "").split("-")[0] or None
        text = await asyncio.to_thread(self._run, model, audio, hint)
        if not text:
            return None
        return _make_transcript(text, provider=self.name, audio=audio, confidence=0.85)

    def _run(self, model: Any, audio: AudioInput, language: str | None) -> str:
        import io
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as handle:
            handle.write(audio.data)
            handle.flush()
            path = handle.name
            try:
                if getattr(model, "_star_backend", None) == "openai-whisper":
                    result = model.transcribe(path, language=language)  # noqa: SLF001
                    return str(result.get("text") or "").strip()
                segments, _info = model.transcribe(path, language=language)
                return " ".join(str(segment.text).strip() for segment in segments).strip()
            except Exception:  # noqa: BLE001
                return ""
            finally:
                _ = io


class STTChain:
    """Try providers in order; first non-empty transcript wins."""

    name = "chain"

    def __init__(self, providers: list[Any]) -> None:
        self.providers = [provider for provider in providers if provider is not None]
        self.last_used = "none"

    def available(self) -> bool:
        return any(provider.available() for provider in self.providers)

    def supports(self, language: str) -> bool:
        return any(provider.supports(language) for provider in self.providers)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": getattr(provider, "name", type(provider).__name__), "available": provider.available()}
            for provider in self.providers
        ]

    async def transcribe(self, audio: AudioInput) -> Transcript | None:
        for provider in self.providers:
            if not provider.available():
                continue
            try:
                transcript = await provider.transcribe(audio)
            except Exception:  # noqa: BLE001 — fall through to the next provider
                continue
            if transcript is not None and transcript.text.strip():
                self.last_used = getattr(provider, "name", type(provider).__name__)
                return transcript
        return None


def build_stt(mode: str = "auto", *, extra: list[Any] | None = None) -> Any:
    """Factory used by :class:`Backend.star.voice.pipeline.VoicePipeline`."""
    providers: list[Any] = list(extra or [])
    mode = (mode or "auto").lower()
    if mode == "null":
        return NullSTT()
    if mode == "browser":
        return STTChain([BrowserSTT(), *providers, NullSTT()])
    if mode == "file":
        return STTChain([FileSTT(), *providers, NullSTT()])
    if mode == "whisper":
        return STTChain([WhisperSTT(), *providers, NullSTT()])
    if mode == "google_dual":
        return STTChain([GoogleDualSTT(), *providers, NullSTT()])
    # auto: local first (private + offline), then network, then browser/file/null
    return STTChain(
        [
            WhisperSTT(),
            GoogleDualSTT(),
            BrowserSTT(),
            FileSTT(),
            *providers,
            NullSTT(),
        ]
    )


def audio_from_base64(payload: str, *, fmt: str = "wav", source: str = "browser") -> AudioInput:
    """Helper for the WS/HTTP verbs that carry base64 audio."""
    raw = payload.strip()
    if "," in raw and raw.split(",", 1)[0].startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"invalid base64 audio: {exc}") from exc
    if not data:
        raise ValueError("base64 audio decoded to zero bytes")
    return AudioInput(data=data, format=fmt, source=source)


_ = stt_locales  # re-exported for callers that build their own provider chains
