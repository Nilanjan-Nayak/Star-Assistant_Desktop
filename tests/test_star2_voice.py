"""Phase 2 — voice layer tests: language routing, STT/TTS providers, barge-in."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from Backend.star.config.settings import Settings, VoiceSettings
from Backend.star.observability.events import StarEventBus
from Backend.star.voice.base import AudioInput, SynthesisResult
from Backend.star.voice.language import (
    BANGLISH_LEXICON,
    detect_language,
    normalize_text,
    prefer_english,
    response_language,
    stt_locales,
    strip_wake_word,
    tts_voice_for,
)
from Backend.star.voice.pipeline import VoicePipeline
from Backend.star.voice.speaker import NullSpeakerVerifier, NumpySpeakerVerifier, _pcm16_from_wav
from Backend.star.voice.stt import (
    BrowserSTT,
    FileSTT,
    GoogleDualSTT,
    NullSTT,
    ScriptedSTT,
    STTChain,
    WhisperSTT,
    audio_from_base64,
    build_stt,
)
from Backend.star.voice.tts import EdgeTTSAdapter, NullTTS, ScriptedTTS, build_tts, sine_wav


def _settings(**voice: Any) -> Settings:
    base = Settings()
    return base.model_copy(update={"voice": VoiceSettings(**{**base.voice.model_dump(), **voice})})


# ── language routing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "code", "banglish"),
    [
        ("আমার ভলিউম একটু বাড়াও", "bn", False),
        ("স্ক্রিনে কী আছে দেখো", "bn", False),
        ("volume ta ektu barao", "bn", True),
        ("gaan chalaao", "bn", True),
        ("screen e ki ache dakho", "bn", True),
        ("kemon acho?", "bn", True),
        ("play some lo-fi music", "en", False),
        ("what is on my screen", "en", False),
        ("open chrome and search weather", "en", False),
        ("", "en", False),
    ],
)
def test_detect_language_matrix(text: str, code: str, banglish: bool) -> None:
    profile = detect_language(text)
    assert profile.code == code, f"{text!r} → {profile.as_dict()}"
    assert profile.banglish is banglish
    assert 0.0 <= profile.confidence <= 1.0


def test_detect_language_mixed_script() -> None:
    profile = detect_language("volume টা ৪০ এ set koro")
    assert profile.code == "mixed"
    assert profile.script == "mixed"
    assert profile.bengali_chars > 0 and profile.latin_chars > 0
    assert profile.is_bengali is True
    assert profile.spoken_form == "Mixed"


def test_normalize_text_converts_bengali_digits_and_strips_wake_words() -> None:
    assert normalize_text("ভলিউম ৪০ কোরো") == "ভলিউম 40 কোরো"
    assert normalize_text("  hey   star,  volume 45 koro ") == "volume 45 koro"
    assert normalize_text("হেই star, ভলিউম ৪০ কোরো") == "ভলিউম 40 কোরো"
    assert normalize_text("শোনো স্টার একটা গান চালাও") == "একটা গান চালাও"


def test_wake_word_stripping_never_eats_the_message() -> None:
    assert strip_wake_word("star") == "star"
    assert strip_wake_word("hey") == "hey"
    assert strip_wake_word("hello star") == "hello star", "'hello' is content, not a wake word"
    assert strip_wake_word("ok star, lock the pc") == "lock the pc"
    assert strip_wake_word("no wake word here") == "no wake word here"


def test_prefer_english_matches_the_legacy_heuristic() -> None:
    assert prefer_english("open chrome", "ওপেন ক্রোম") is True
    assert prefer_english("play", "প্লে") is True
    assert prefer_english("ওপেন ক্রোম", "open chrome") is False
    assert prefer_english("", "বাংলা") is False
    assert prefer_english("xyzzy qqq", "বাংলা") is False


def test_response_language_policy() -> None:
    bn = detect_language("ভলিউম বাড়াও")
    en = detect_language("raise the volume")
    mixed = detect_language("volume টা বাড়াও")
    assert response_language(bn) == "bn"
    assert response_language(en) == "en"
    assert response_language(mixed, default="bn") == "bn"
    assert response_language(mixed, default="en") == "en"
    assert response_language(mixed, default="fr") == "bn", "unsupported default falls back to Bengali"


def test_stt_locale_order_and_voice_choice() -> None:
    assert stt_locales() == ("bn-IN", "en-IN")
    assert stt_locales(detect_language("ভলিউম")) == ("bn-IN", "en-IN")
    assert stt_locales(detect_language("raise the volume")) == ("en-IN", "bn-IN")
    assert tts_voice_for("bn").startswith("bn-")
    assert tts_voice_for("en").startswith("en-")
    assert tts_voice_for("bn", preferred="bn-IN-TanishaaNeural") == "bn-IN-TanishaaNeural"
    assert tts_voice_for("en", preferred="bn-IN-TanishaaNeural").startswith("en-")
    assert len(BANGLISH_LEXICON) > 80


# ── STT providers ─────────────────────────────────────────────────────────────


async def test_null_and_scripted_stt() -> None:
    audio = AudioInput(data=b"1234", source="test")
    assert await NullSTT().transcribe(audio) is None
    scripted = ScriptedSTT(default="volume barao")
    transcript = await scripted.transcribe(audio)
    assert transcript is not None
    assert transcript.text == "volume barao"
    assert transcript.language == "bn" and transcript.banglish is True
    assert transcript.confidence == 1.0
    assert scripted.calls == [audio]
    assert ScriptedSTT().transcribe is not None
    assert await ScriptedSTT().transcribe(audio) is None


async def test_browser_stt_accepts_text_and_json() -> None:
    provider = BrowserSTT()
    plain = await provider.transcribe(AudioInput(data="আমার ভলিউম বাড়াও".encode(), format="text"))
    assert plain is not None and plain.language == "bn" and plain.provider == "browser"
    wrapped = await provider.transcribe(AudioInput(data=json.dumps({"text": "play lo-fi"}).encode(), format="text"))
    assert wrapped is not None and wrapped.text == "play lo-fi" and wrapped.language == "en"
    assert await provider.transcribe(AudioInput(data=b"")) is None
    assert await provider.transcribe(AudioInput(data=b"\xff\xfe\x00bin")) is None
    assert await provider.transcribe(AudioInput(data=b"{broken json")) is None


async def test_file_stt_reads_sidecars(tmp_path) -> None:
    audio_file = tmp_path / "speech.wav"
    audio_file.write_bytes(sine_wav(120))
    (tmp_path / "speech.txt").write_text("brightness 60 koro", encoding="utf-8")
    provider = FileSTT(root=tmp_path)
    transcript = await provider.transcribe(AudioInput(data=b"speech.wav", source="file", format="wav"))
    assert transcript is not None and transcript.text == "brightness 60 koro"
    assert transcript.banglish is True

    (tmp_path / "other.txt").write_text("", encoding="utf-8")
    assert await provider.transcribe(AudioInput(data=b"other.wav", source="file")) is None
    assert await provider.transcribe(AudioInput(data=b"\xff\xfe", source="file")) is None


async def test_stt_chain_skips_unavailable_and_falls_through() -> None:
    class Broken:
        name = "broken"

        def available(self) -> bool:
            return True

        def supports(self, language: str) -> bool:
            return True

        async def transcribe(self, audio: AudioInput) -> Any:
            raise RuntimeError("kaboom")

    class Unavailable:
        name = "unavailable"

        def available(self) -> bool:
            return False

        def supports(self, language: str) -> bool:
            return True

        async def transcribe(self, audio: AudioInput) -> Any:  # pragma: no cover - never called
            raise AssertionError("must be skipped")

    chain = STTChain([Unavailable(), Broken(), ScriptedSTT(default="চালাও গান"), NullSTT()])
    assert chain.available() is True
    transcript = await chain.transcribe(AudioInput(data=b"x"))
    assert transcript is not None and transcript.text == "চালাও গান"
    assert chain.last_used == "scripted"
    assert [entry["name"] for entry in chain.describe()] == ["unavailable", "broken", "scripted", "null"]
    assert await STTChain([NullSTT()]).transcribe(AudioInput(data=b"x")) is None


def test_build_stt_modes() -> None:
    assert isinstance(build_stt("null"), NullSTT)
    for mode in ("auto", "whisper", "google_dual", "browser", "file"):
        provider = build_stt(mode)
        assert isinstance(provider, STTChain)
        assert provider.available() is True, "every chain ends in a provider that is available"
    names = [getattr(p, "name", "?") for p in build_stt("auto").providers]
    assert names[0] == "whisper" and names[-1] == "null"


def test_optional_stt_providers_report_unavailable_cleanly() -> None:
    assert WhisperSTT().available() in {True, False}
    assert GoogleDualSTT().available() in {True, False}
    assert GoogleDualSTT().supports("bn") is True


async def test_google_dual_stt_without_audio_is_none() -> None:
    provider = GoogleDualSTT()
    assert await provider.transcribe(AudioInput(data=b"")) is None


def test_audio_from_base64() -> None:
    raw = sine_wav(60)
    encoded = base64.b64encode(raw).decode()
    audio = audio_from_base64(encoded)
    assert audio.data == raw and audio.format == "wav" and audio.source == "browser"
    data_uri = audio_from_base64(f"data:audio/wav;base64,{encoded}", fmt="wav")
    assert data_uri.data == raw
    with pytest.raises(ValueError):
        audio_from_base64("!!!not base64!!!")
    assert AudioInput(data=raw).fingerprint != AudioInput(data=b"").fingerprint
    assert AudioInput().fingerprint == "empty"


# ── TTS providers ─────────────────────────────────────────────────────────────


def test_sine_wav_is_a_valid_pcm16_file() -> None:
    audio = sine_wav(200, 440.0, sample_rate=16000)
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    assert audio[12:16] == b"fmt " and audio[36:40] == b"data"
    samples, rate = _pcm16_from_wav(audio)
    assert rate == 16000
    assert len(samples) == 3200


async def test_null_and_scripted_tts() -> None:
    result = await NullTTS().synthesize("হ্যালো", language="bn")
    assert result.ok is False and result.error == "tts disabled" and result.text == "হ্যালো"

    scripted = ScriptedTTS()
    result = await scripted.synthesize("ভলিউম বাড়িয়ে দিলাম", language="bn")
    assert result.ok is True and result.audio_bytes is not None
    assert result.audio_bytes[:4] == b"RIFF"
    assert result.voice.startswith("bn-")
    assert scripted.calls and scripted.calls[0][1].startswith("bn-")
    public = result.public()
    assert "audio_bytes" not in public and public["audio_bytes_len"] > 1000


async def test_edge_tts_degrades_when_edge_tts_is_missing() -> None:
    adapter = EdgeTTSAdapter(default_voice="bn-IN-BashkarNeural")
    available = adapter.available()
    result = await adapter.synthesize("পরীক্ষা", language="bn")
    if not available:
        assert result.ok is False and result.error
    else:  # pragma: no cover - only on machines with edge-tts installed
        assert result.ok is True and result.audio_path
    assert adapter.voice_chain("bn")[0].startswith("bn-")
    assert adapter.voice_chain("en")[0].startswith("en-")


def test_build_tts_modes() -> None:
    assert isinstance(build_tts("null"), NullTTS)
    assert isinstance(build_tts("scripted"), ScriptedTTS)
    assert isinstance(build_tts("edge"), EdgeTTSAdapter)
    cascade = build_tts("auto")
    assert cascade.name == "cascade"
    assert [p.name for p in cascade.providers] == ["edge", "null"]
    assert cascade.supports("bn") is True


async def test_tts_cascade_falls_back_to_the_next_provider() -> None:
    cascade = build_tts("auto")
    result = await cascade.synthesize("hello", language="en")
    assert isinstance(result, SynthesisResult)
    assert result.provider in {"edge", "null"}


# ── speaker verification (separate from STT) ──────────────────────────────────


def test_null_speaker_verifier_never_rejects_or_accepts() -> None:
    verifier = NullSpeakerVerifier()
    match = verifier.verify(AudioInput(data=sine_wav(100)))
    assert match.accepted is False and match.enrolled is False and match.speaker_id is None
    assert verifier.enrol("owner", AudioInput(data=b"x")) is False
    assert verifier.forget("owner") is False
    assert verifier.describe() == {"name": "null", "available": True, "enrolled": 0}


def test_numpy_speaker_verifier_enrol_and_match() -> None:
    verifier = NumpySpeakerVerifier(threshold=0.9)
    if not verifier.available():  # pragma: no cover - numpy is a dev dependency here
        pytest.skip("numpy unavailable")
    voice_a = AudioInput(data=sine_wav(600, 320.0), format="wav")
    voice_b = AudioInput(data=sine_wav(600, 1500.0), format="wav")

    assert verifier.verify(voice_a).reason == "no enrolled speakers"
    assert verifier.enrol("owner", voice_a) is True
    assert verifier.enrol("owner", AudioInput(data=b"short")) is False, "too-short audio cannot enrol"

    same = verifier.verify(voice_a)
    assert same.enrolled is True
    assert same.score == pytest.approx(1.0, abs=1e-6)
    assert same.accepted is True and same.speaker_id == "owner"

    other = verifier.verify(voice_b)
    assert other.score < 1.0
    assert other.method == "profile"
    assert verifier.verify(AudioInput(data=sine_wav(10, 440.0)), speaker_id="owner").enrolled is True

    assert verifier.describe()["enrolled"] == 1
    assert verifier.forget("owner") is True
    assert verifier.forget("owner") is False
    assert verifier.describe()["enrolled"] == 0


# ── pipeline ──────────────────────────────────────────────────────────────────


def _pipeline(**kwargs: Any) -> tuple[VoicePipeline, StarEventBus]:
    bus = StarEventBus(history_size=200)
    settings = kwargs.pop("settings", _settings())
    pipeline = VoicePipeline(
        settings,
        bus=bus,
        stt=kwargs.pop("stt", ScriptedSTT(default="volume 40 koro")),
        tts=kwargs.pop("tts", ScriptedTTS()),
        **kwargs,
    )
    return pipeline, bus


def _kinds(bus: StarEventBus) -> list[str]:
    return [event.kind for event in bus.history(limit=200)]


async def test_hear_produces_transcript_and_language_event() -> None:
    pipeline, bus = _pipeline()
    transcript = await pipeline.hear(AudioInput(data=b"audio-bytes"))
    assert transcript is not None
    assert transcript.text == "volume 40 koro"
    assert transcript.language == "bn" and transcript.banglish is True
    assert "language.detected" in _kinds(bus)
    event = next(e for e in bus.history(limit=50) if e.kind == "language.detected")
    assert event.payload["language"] == "bn"
    assert event.payload["retained"] is False
    assert event.payload["text"].startswith("<redacted:"), "transcripts are dropped by default"
    assert pipeline.last_transcript is transcript
    assert pipeline.stats["heard"] == 1


async def test_retention_flag_keeps_the_transcript() -> None:
    pipeline, bus = _pipeline(settings=_settings(keep_transcripts=True))
    await pipeline.hear(AudioInput(data=b"audio"))
    event = next(e for e in bus.history(limit=50) if e.kind == "language.detected")
    assert event.payload["text"] == "volume 40 koro"
    assert event.payload["retained"] is True


async def test_hear_handles_stt_failure_without_raising() -> None:
    class Exploding:
        name = "explode"

        def available(self) -> bool:
            return True

        def supports(self, language: str) -> bool:
            return True

        async def transcribe(self, audio: AudioInput) -> Any:
            raise RuntimeError("mic on fire")

    pipeline, bus = _pipeline(stt=Exploding())
    assert await pipeline.hear(AudioInput(data=b"x")) is None
    assert pipeline.stats["stt_failures"] == 1
    assert "task.failed" in _kinds(bus)


async def test_transcript_from_text_path() -> None:
    pipeline, bus = _pipeline()
    transcript = pipeline.transcript_from_text("  hey star, brightness 70 koro  ")
    assert transcript is not None
    assert transcript.text == "brightness 70 koro", "wake words are stripped"
    assert transcript.provider == "browser"
    assert pipeline.transcript_from_text("   ") is None
    assert "language.detected" in _kinds(bus)


async def test_speak_emits_response_and_uses_language_policy() -> None:
    pipeline, bus = _pipeline()
    result = await pipeline.speak("ভলিউম বাড়িয়ে দিলাম বন্ধু")
    assert result.ok is True
    assert result.language == "bn" and result.voice.startswith("bn-")
    assert result.audio_bytes is not None
    event = next(e for e in bus.history(limit=50) if e.kind == "response.spoken")
    assert event.payload["ok"] is True
    assert event.payload["text"].endswith("chars>"), "text is not retained by default"
    assert pipeline.stats["spoken"] == 1
    assert (await pipeline.speak("   ")).ok is False


async def test_barge_in_stops_playback_and_cancels_the_task() -> None:
    stopped: list[str] = []
    cancelled: list[str] = []
    pipeline, bus = _pipeline(
        stop_playback=lambda: stopped.append("stop"),
        cancel_current=lambda reason: cancelled.append(reason),
    )
    first = pipeline.generation
    result = pipeline.barge_in("user spoke")
    assert result["ok"] is True and result["generation"] == first + 1
    assert stopped == ["stop"] and cancelled == ["barge_in:user spoke"]
    assert "task.cancelled" in _kinds(bus)

    disabled, _ = _pipeline(settings=_settings(allow_barge_in=False))
    assert disabled.barge_in("nope")["ok"] is False


async def test_speech_started_triggers_barge_in() -> None:
    started: list[bool] = []
    pipeline, bus = _pipeline(on_speech_started=lambda: started.append(True))
    generation = pipeline.generation
    pipeline.speech_started()
    assert pipeline.generation == generation + 1
    assert started == [True]

    quiet, _ = _pipeline(settings=_settings(allow_barge_in=False))
    before = quiet.generation
    quiet.speech_started()
    assert quiet.generation == before


async def test_barge_in_drops_audio_synthesised_for_a_stale_generation() -> None:
    class SlowTTS(ScriptedTTS):
        name = "slow"

        async def synthesize(self, text: str, *, language: str = "bn", voice: str | None = None) -> SynthesisResult:
            await asyncio.sleep(0.15)
            return await super().synthesize(text, language=language, voice=voice)

    pipeline, bus = _pipeline(tts=SlowTTS())
    task = asyncio.create_task(pipeline.speak("একটু ধরো"))
    await asyncio.sleep(0.03)
    pipeline.barge_in("interrupt")
    result = await task
    assert result.ok is False and result.error == "interrupted"
    assert result.audio_bytes is None and result.audio_path is None


async def test_pipeline_handles_voice_verbs() -> None:
    pipeline, bus = _pipeline()
    transcript_reply = await pipeline.handle_message({"type": "voice.transcribe", "text": "hey star, volume komao"})
    assert transcript_reply["ok"] is True
    assert transcript_reply["transcript"]["text"].startswith("<redacted:"), "retention is off by default"
    assert transcript_reply["transcript"]["banglish"] is True

    audio_reply = await pipeline.handle_message(
        {"type": "voice.transcribe", "audio_base64": base64.b64encode(b"raw-audio").decode()}
    )
    assert audio_reply["ok"] is True and audio_reply["transcript"]["provider"] == "scripted"

    assert (await pipeline.handle_message({"type": "voice.transcribe"}))["ok"] is False
    assert (await pipeline.handle_message({"type": "voice.transcribe", "audio_base64": "@@@@"}))["ok"] is False

    speak_reply = await pipeline.handle_message({"type": "voice.synthesize", "text": "ঠিক আছে বন্ধু"})
    assert speak_reply["ok"] is True and speak_reply["synthesis"]["voice"].startswith("bn-")
    assert (await pipeline.handle_message({"type": "voice.synthesize"}))["ok"] is False

    assert (await pipeline.handle_message({"type": "voice.barge_in"}))["ok"] is True
    assert (await pipeline.handle_message({"type": "voice.speech_started"}))["ok"] is True
    status = await pipeline.handle_message({"type": "voice.status"})
    assert status["ok"] is True and status["generation"] >= 2
    enrol = await pipeline.handle_message(
        {"type": "voice.enrol", "speaker_id": "nilanjan", "audio_base64": base64.b64encode(sine_wav(400)).decode()}
    )
    assert enrol["ok"] in {True, False} and enrol["speaker_id"] == "nilanjan"
    unknown = await pipeline.handle_message({"type": "voice.wat"})
    assert unknown["ok"] is False and "unknown voice verb" in unknown["error"]


async def test_pipeline_health_and_startup() -> None:
    pipeline, bus = _pipeline(settings=_settings(speaker_verification=True))
    await pipeline.startup()
    health = pipeline.health()
    assert health["status"] in {"ok", "degraded"}
    assert health["detail"]["keep_transcripts"] is False
    assert health["detail"]["speaker"]["name"] == "profile"
    assert isinstance(pipeline.speaker, NumpySpeakerVerifier)
    assert "agent.ready" in _kinds(bus)
    await pipeline.aclose()
    assert pipeline.speaking is False


def test_voice_settings_defaults_are_private_and_bengali_first() -> None:
    settings = Settings()
    assert settings.voice.keep_transcripts is False
    assert settings.voice.allow_barge_in is True
    assert settings.voice.auto_speak is False
    assert settings.voice.default_response_language == "bn"
    assert settings.voice.languages == ("bn", "en")
    assert settings.voice.tts_voice.startswith("bn-")
