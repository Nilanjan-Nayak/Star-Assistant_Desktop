"""The voice pipeline: hear → understand → (brain) → speak, with barge-in.

Wiring (blueprint §7 Phase 2)::

    mic/browser audio ──▶ STTProvider ──▶ Transcript ──▶ language routing
                                                             │
                              speaker verification (optional)│
                                                             ▼
                                                     brain / orchestrator
                                                             │
    audio playback ◀── TTSProvider ◀── SynthesisResult ◀─────┘

Barge-in: ``speech_started``/``voice.barge_in`` bumps a generation counter, stops
playback and cancels the in-flight task token, so an interruption never leaves two
voices talking. Transcript retention is configurable and defaults to **off**.
"""

from __future__ import annotations

from typing import Any, Callable

from Backend.star.config.settings import Settings, get_settings
from Backend.star.observability.events import EventPhase, StarEventBus, get_event_bus, new_event
from Backend.star.voice.base import AudioInput, SynthesisResult, Transcript
from Backend.star.voice.language import detect_language, normalize_text, response_language
from Backend.star.voice.speaker import SpeakerVerifier, build_speaker_verifier
from Backend.star.voice.stt import audio_from_base64, build_stt
from Backend.star.voice.tts import build_tts

__all__ = ["VoicePipeline"]

StopPlayback = Callable[[], None]
CancelCurrent = Callable[[str], Any]


class VoicePipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: StarEventBus | None = None,
        stt: Any = None,
        tts: Any = None,
        speaker: SpeakerVerifier | None = None,
        stop_playback: StopPlayback | None = None,
        cancel_current: CancelCurrent | None = None,
        on_speech_started: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.bus = bus or get_event_bus()
        voice_cfg = self.settings.voice
        self.stt = stt if stt is not None else build_stt(voice_cfg.stt_provider)
        self.tts = tts if tts is not None else build_tts_safe(voice_cfg.tts_provider, voice_cfg.tts_voice)
        self.speaker = speaker if speaker is not None else build_speaker_verifier(voice_cfg.speaker_verification)
        self.stop_playback = stop_playback or (lambda: None)
        self.cancel_current = cancel_current or (lambda reason: None)
        self.on_speech_started = on_speech_started or (lambda: None)
        self.generation = 0
        self.speaking = False
        self.last_transcript: Transcript | None = None
        self.stats = {"heard": 0, "spoken": 0, "barge_ins": 0, "stt_failures": 0}

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def startup(self) -> None:
        self.bus.emit(
            "agent.ready",
            phase=EventPhase.VOICE,
            subsystem="voice",
            stt=self.describe_providers(self.stt),
            tts=self.describe_providers(self.tts),
            keep_transcripts=self.settings.voice.keep_transcripts,
        )

    async def aclose(self) -> None:
        self.speaking = False

    def health(self) -> dict[str, Any]:
        stt_ok = bool(self.stt.available())
        tts_ok = bool(self.tts.available())
        status = "ok" if (stt_ok or tts_ok) else "degraded"
        return {
            "status": status,
            "detail": {
                "stt": self.describe_providers(self.stt),
                "tts": self.describe_providers(self.tts),
                "speaker": self.speaker.describe(),
                "generation": self.generation,
                "speaking": self.speaking,
                "stats": dict(self.stats),
                "keep_transcripts": self.settings.voice.keep_transcripts,
            },
        }

    @staticmethod
    def describe_providers(provider: Any) -> Any:
        describe = getattr(provider, "describe", None)
        if callable(describe):
            return describe()
        return {"name": getattr(provider, "name", type(provider).__name__), "available": provider.available()}

    # ── hearing ─────────────────────────────────────────────────────────────
    def speech_started(self) -> None:
        """VAD/echo hook: the user began talking (used for barge-in)."""
        if self.settings.voice.allow_barge_in:
            self.barge_in("speech_started")
        self.bus.emit("request.received", phase=EventPhase.VOICE, stage="speech_started")
        self.on_speech_started()

    async def hear(self, audio: AudioInput) -> Transcript | None:
        """Run STT (+ optional speaker verification) and publish the result."""
        self.stats["heard"] += 1
        transcript: Transcript | None = None
        try:
            transcript = await self.stt.transcribe(audio)
        except Exception as exc:  # noqa: BLE001 — STT must never take the app down
            self.stats["stt_failures"] += 1
            self.bus.publish(
                new_event("task.failed", phase=EventPhase.VOICE, error=repr(exc), stage="stt")
            )
            return None
        if transcript is None or not transcript.text.strip():
            self.stats["stt_failures"] += 1
            self.bus.emit("language.detected", phase=EventPhase.VOICE, text=None, reason="no transcript")
            return None

        match = None
        if self.settings.voice.speaker_verification:
            try:
                match = self.speaker.verify(audio)
                transcript = transcript.model_copy(update={"speaker_id": match.speaker_id})
            except Exception:  # noqa: BLE001
                match = None

        self.last_transcript = transcript
        safe = transcript if self.settings.voice.keep_transcripts else transcript.redacted()
        self.bus.emit(
            "language.detected",
            phase=EventPhase.VOICE,
            text=safe.text,
            language=transcript.language,
            script=transcript.script,
            banglish=transcript.banglish,
            confidence=round(transcript.confidence, 3),
            provider=transcript.provider,
            audio_ref=transcript.audio_ref,
            speaker=(match.model_dump(mode="json") if match is not None else None),
            retained=self.settings.voice.keep_transcripts,
        )
        return transcript

    def transcript_from_text(self, text: str, *, provider: str = "browser", source: str = "browser") -> Transcript | None:
        """Client-side STT (Web Speech API) hands us the transcript directly."""
        cleaned = normalize_text(text or "")
        if not cleaned:
            return None
        audio = AudioInput(source=source, format="text", data=cleaned.encode("utf-8"))
        profile = detect_language(cleaned)
        transcript = Transcript(
            text=cleaned,
            language=profile.code,
            script=profile.script,
            banglish=profile.banglish,
            confidence=0.9,
            provider=provider,
            audio_ref=audio.fingerprint,
        )
        self.last_transcript = transcript
        safe = transcript if self.settings.voice.keep_transcripts else transcript.redacted()
        self.bus.emit(
            "language.detected",
            phase=EventPhase.VOICE,
            text=safe.text,
            language=profile.code,
            banglish=profile.banglish,
            provider=provider,
            retained=self.settings.voice.keep_transcripts,
        )
        return transcript

    # ── speaking ────────────────────────────────────────────────────────────
    async def speak(
        self, text: str, *, language: str | None = None, voice: str | None = None, generation: int | None = None
    ) -> SynthesisResult:
        if not (text or "").strip():
            return SynthesisResult(ok=False, text=text or "", provider=getattr(self.tts, "name", "?"), error="empty text")
        resolved = language or response_language(
            detect_language(text), default=self.settings.voice.default_response_language
        )
        generation = self.generation if generation is None else generation
        self.speaking = True
        try:
            result = await self.tts.synthesize(text, language=resolved, voice=voice)
        except Exception as exc:  # noqa: BLE001
            result = SynthesisResult(
                ok=False, text=text, language=resolved, provider=getattr(self.tts, "name", "?"), error=repr(exc)
            )
        finally:
            self.speaking = False

        if generation != self.generation:
            # Interrupted while synthesising — drop the audio instead of talking over the user.
            self.bus.emit("task.cancelled", phase=EventPhase.VOICE, stage="tts", generation=generation)
            return result.model_copy(update={"ok": False, "error": "interrupted", "audio_path": None, "audio_bytes": None})

        if result.ok:
            self.stats["spoken"] += 1
        self.bus.emit(
            "response.spoken",
            phase=EventPhase.VOICE,
            text=text[:300] if self.settings.voice.keep_transcripts else f"<{len(text)} chars>",
            ok=result.ok,
            language=result.language,
            voice=result.voice,
            provider=result.provider,
            audio_path=result.audio_path,
            error=result.error,
        )
        return result

    # ── barge-in ────────────────────────────────────────────────────────────
    def barge_in(self, reason: str = "user") -> dict[str, Any]:
        """Stop talking, cancel the in-flight task, and start a new generation."""
        if not self.settings.voice.allow_barge_in:
            return {"ok": False, "reason": "barge-in disabled", "generation": self.generation}
        self.generation += 1
        self.stats["barge_ins"] += 1
        self.speaking = False
        try:
            self.stop_playback()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.cancel_current(f"barge_in:{reason}")
        except Exception:  # noqa: BLE001
            pass
        self.bus.emit("task.cancelled", phase=EventPhase.VOICE, reason=reason, generation=self.generation)
        return {"ok": True, "generation": self.generation, "reason": reason}

    # ── WS/HTTP verbs ───────────────────────────────────────────────────────
    async def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        verb = str(message.get("type") or "").lower()
        if verb in {"voice.transcribe", "voice.hear", "stt"}:
            if message.get("text"):
                transcript = self.transcript_from_text(
                    str(message["text"]), provider=str(message.get("provider") or "browser")
                )
                if transcript is None:
                    return {"ok": False, "error": "empty transcript"}
                safe = transcript if self.settings.voice.keep_transcripts else transcript.redacted()
                return {"ok": True, "transcript": safe.model_dump(mode="json")}
            audio = self._audio_from_message(message)
            if audio is None:
                return {"ok": False, "error": "provide 'text' or 'audio_base64'"}
            transcript = await self.hear(audio)
            if transcript is None:
                return {"ok": False, "error": "no transcript produced"}
            safe = transcript if self.settings.voice.keep_transcripts else transcript.redacted()
            return {"ok": True, "transcript": safe.model_dump(mode="json")}
        if verb in {"voice.synthesize", "voice.speak", "tts"}:
            text = str(message.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "field 'text' is required"}
            result = await self.speak(text, language=message.get("language"), voice=message.get("voice"))
            return {"ok": result.ok, "synthesis": result.public()}
        if verb in {"voice.barge_in", "barge_in", "interrupt"}:
            return self.barge_in(str(message.get("reason") or "client"))
        if verb == "voice.speech_started":
            self.speech_started()
            return {"ok": True, "generation": self.generation}
        if verb == "voice.enrol":
            speaker_id = str(message.get("speaker_id") or self.settings.owner)
            audio = self._audio_from_message(message)
            if audio is None:
                return {"ok": False, "error": "provide 'audio_base64'"}
            enrolled = self.speaker.enrol(speaker_id, audio)
            self.bus.emit("memory.updated", phase=EventPhase.VOICE, layer="speaker", speaker_id=speaker_id, ok=enrolled)
            return {"ok": enrolled, "speaker_id": speaker_id, "verifier": self.speaker.describe()}
        if verb in {"voice.status", "voice.config"}:
            return {"ok": True, **self.health()["detail"], "generation": self.generation}
        return {"ok": False, "error": f"unknown voice verb: {verb}"}

    @staticmethod
    def _audio_from_message(message: dict[str, Any]) -> AudioInput | None:
        payload = message.get("audio_base64") or message.get("audio")
        if not payload:
            return None
        try:
            return audio_from_base64(
                str(payload),
                fmt=str(message.get("format") or "wav"),
                source=str(message.get("source") or "client"),
            )
        except ValueError:
            return None


def build_tts_safe(mode: str, default_voice: str) -> Any:
    """Small wrapper so a broken TTS install degrades to silence, not a crash."""
    try:
        return build_tts(mode, default_voice=default_voice)
    except Exception:  # noqa: BLE001
        from Backend.star.voice.tts import NullTTS

        return NullTTS()


def build_voice_pipeline(settings: Settings | None = None, **kwargs: Any) -> VoicePipeline:
    """Factory used by the composition root."""
    return VoicePipeline(settings, **kwargs)
