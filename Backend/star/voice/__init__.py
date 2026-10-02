"""STAR 2.0 voice layer — STT + TTS + Bengali/English routing + barge-in.

Reuses (never rewrites) the existing engines: ``Backend/voice/tts.VoiceEngine``
for neural Bengali/English synthesis and the ``Backend/bridge.py::VoiceListener``
dual bn-IN/en-IN disambiguation heuristic (lifted into
:func:`Backend.star.voice.language.prefer_english` as a pure function).
"""

from __future__ import annotations

from Backend.star.voice.base import AudioInput, STTProvider, SynthesisResult, TTSProvider, Transcript
from Backend.star.voice.language import (
    BANGLISH_LEXICON,
    LanguageProfile,
    detect_language,
    normalize_text,
    prefer_english,
    response_language,
    stt_locales,
    strip_wake_word,
    tts_voice_for,
)
from Backend.star.voice.pipeline import VoicePipeline, build_voice_pipeline
from Backend.star.voice.speaker import NullSpeakerVerifier, NumpySpeakerVerifier, SpeakerMatch, SpeakerVerifier
from Backend.star.voice.stt import BrowserSTT, FileSTT, GoogleDualSTT, NullSTT, ScriptedSTT, STTChain, WhisperSTT, build_stt
from Backend.star.voice.tts import EdgeTTSAdapter, NullTTS, ScriptedTTS, build_tts, sine_wav

__all__ = [
    "BANGLISH_LEXICON",
    "AudioInput",
    "BrowserSTT",
    "EdgeTTSAdapter",
    "FileSTT",
    "GoogleDualSTT",
    "LanguageProfile",
    "NullSpeakerVerifier",
    "NullSTT",
    "NullTTS",
    "NumpySpeakerVerifier",
    "STTChain",
    "STTProvider",
    "ScriptedSTT",
    "ScriptedTTS",
    "SpeakerMatch",
    "SpeakerVerifier",
    "SynthesisResult",
    "TTSProvider",
    "Transcript",
    "VoicePipeline",
    "WhisperSTT",
    "build_stt",
    "build_tts",
    "build_voice_pipeline",
    "detect_language",
    "normalize_text",
    "prefer_english",
    "response_language",
    "sine_wav",
    "stt_locales",
    "strip_wake_word",
    "tts_voice_for",
]
