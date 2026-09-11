"""
Text-to-Speech (TTS) Engine for Star Assistant.
Uses free Microsoft Edge Neural Voices (high-fidelity Bengali/English)
with offline Windows SAPI5 fallback.
"""

import os
import asyncio
import hashlib
import re
from pathlib import Path
from typing import Optional

from ..config import AUDIO_CACHE_DIR, TTS_VOICE, TTS_RATE, TTS_PITCH


def clean_for_speech(text: str) -> str:
    """Prepare text for crystal-clear, natural neural speech synthesis."""
    if not text:
        return ""
    # Remove code blocks and backticks
    t = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    t = re.sub(r'`.*?`', '', t)
    # Remove markdown asterisks, hashes, bullets, brackets
    t = re.sub(r'[*_~#>]', '', t)
    t = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', t)
    # Remove UI glyphs and weird symbols
    t = re.sub(r'[●▸◆■▶◀✔✓✕✖★☆✦✧\U00010000-\U0010ffff]', '', t)
    # Normalize pauses
    t = re.sub(r'\s*\n+\s*', '। ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


class VoiceEngine:
    """Free Neural Speech Synthesizer."""

    def __init__(self, voice: str = TTS_VOICE):
        self.voice = voice
        self.rate = TTS_RATE
        self.pitch = TTS_PITCH
        self.cache_dir = Path(AUDIO_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> Optional[str]:
        """Convert text to speech and return path to saved .mp3 file."""
        if not text or not text.strip():
            return None

        # Clean text to remove asterisks, markdown, and emojis for smooth voice
        spoken_text = clean_for_speech(text)
        if not spoken_text:
            spoken_text = text.strip()

        has_bengali = any('\u0980' <= ch <= '\u09ff' for ch in spoken_text)
        voice = self.voice if has_bengali else "en-IN-NeerjaExpressiveNeural"

        # Hash text + voice settings for instant disk cache hit
        key = f"{spoken_text}_{voice}_{self.rate}_{self.pitch}".encode("utf-8")
        h = hashlib.md5(key).hexdigest()
        out_file = self.cache_dir / f"tts_{h}.mp3"

        if out_file.exists() and out_file.stat().st_size > 0:
            return str(out_file)

        try:
            import edge_tts

            async def _run():
                communicate = edge_tts.Communicate(
                    text=spoken_text,
                    voice=voice,
                    rate=self.rate,
                    pitch=self.pitch
                )
                await communicate.save(str(out_file))

            # Run in event loop safely across threads
            try:
                asyncio.run(_run())
            except RuntimeError:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                new_loop.run_until_complete(_run())
                new_loop.close()

            if out_file.exists() and out_file.stat().st_size > 0:
                return str(out_file)

        except Exception as e:
            print(f"[TTS] Edge-TTS error: {e}. Attempting offline SAPI fallback...")
            return self._offline_fallback(text, out_file)

        return None

    def _offline_fallback(self, text: str, target_file: Path) -> Optional[str]:
        """Offline fallback using Windows SAPI5 / pyttsx3."""
        try:
            import pyttsx3
            engine = pyttsx3.init()
            wav_file = target_file.with_suffix(".wav")
            engine.save_to_file(text, str(wav_file))
            engine.runAndWait()
            if wav_file.exists() and wav_file.stat().st_size > 0:
                return str(wav_file)
        except Exception as e:
            print(f"[TTS] Offline fallback failed: {e}")
        return None


_engine_instance = None


def get_voice_engine() -> VoiceEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = VoiceEngine()
    return _engine_instance


def synthesize_speech(text: str) -> Optional[str]:
    """Top-level helper to synthesize text into audio."""
    return get_voice_engine().synthesize(text)
