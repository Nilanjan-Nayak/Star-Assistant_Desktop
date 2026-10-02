"""Bengali / Banglish / English language routing.

Three things live here and nowhere else:

1. **detection** — Bengali script, romanised Bengali ("Banglish"), English, and
   code-switched mixes, with a confidence score.
2. **normalisation** — Bengali digits → ASCII, wake-word stripping, filler trim.
3. **policy** — which language Star *answers* in, which STT locales to query and
   which TTS voice to use.

The dual bn-IN/en-IN disambiguation heuristic is lifted (as a pure function) from
``Backend/bridge.py::VoiceListener._prefer_english`` so the Qt class stays
untouched and the logic becomes unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "BANGLISH_LEXICON",
    "BENGALI_DIGITS",
    "LanguageProfile",
    "detect_language",
    "normalize_text",
    "prefer_english",
    "response_language",
    "stt_locales",
    "strip_wake_word",
    "tts_voice_for",
]

_BN_START: Final[int] = 0x0980
_BN_END: Final[int] = 0x09FF
BENGALI_DIGITS: Final[str] = "০১২৩৪৫৬৭৮৯"
_BN_DIGIT_TRANS: Final[dict[int, str]] = str.maketrans(BENGALI_DIGITS, "0123456789")

# Romanised Bengali function words / verbs. Weighted: verbs and particles are the
# strongest signal, because English tech words ("volume", "play") appear in both.
BANGLISH_LEXICON: Final[dict[str, float]] = {
    # verbs / imperatives
    "koro": 1.0, "kore": 0.8, "kori": 0.8, "kor": 0.9, "korona": 0.9, "kholo": 1.0,
    "khulo": 1.0, "bondho": 0.9, "bonddho": 0.9, "barao": 1.0, "baraoo": 1.0,
    "koman": 1.0, "komao": 1.0, "dakho": 1.0, "dekho": 0.9, "dekh": 0.8, "shono": 1.0,
    "shun": 0.8, "suno": 0.9, "bolo": 0.8, "bollo": 0.9, "bol": 0.7, "chalu": 0.8,
    "bandh": 0.7, "de": 0.5, "dao": 0.8, "dilam": 0.8, "niye": 0.6, "niete": 0.7,
    "rakho": 0.9, "rakhte": 0.8, "mone": 0.7, "khoj": 0.8, "khuje": 0.8, "batao": 0.8,
    "batao": 0.8, "shonao": 1.0, "chalao": 1.0, "chalaao": 1.0, "lagao": 0.9,
    "muchi": 0.6, "mochao": 0.9, "badhao": 0.9, "baro": 0.6, "kom": 0.5,
    # pronouns / particles
    "amar": 1.0, "amake": 1.0, "amader": 1.0, "tomar": 1.0, "tumi": 1.0, "apni": 1.0,
    "ekta": 0.9, "ekti": 0.9, "kono": 0.8, "kichu": 0.8, "kivabe": 1.0, "keno": 1.0,
    "kobe": 1.0, "kothay": 1.0, "ke": 0.4, "ki": 0.4, "je": 0.4, "na": 0.3, "ar": 0.3,
    "aaj": 0.9, "aj": 0.7, "kal": 0.8, "ekhon": 1.0, "porer": 0.8, "ager": 0.8,
    "valo": 0.8, "bhalo": 0.8, "kharap": 0.9, "sundor": 0.8, "shundor": 0.8,
    "darun": 0.9, "dharun": 0.9, "please": 0.0, "ektu": 1.0, "ekdom": 0.9,
    "bhai": 0.8, "dada": 0.8, "bondhu": 1.0, "bandhu": 1.0, "gaan": 1.0, "gaon": 0.9,
    "chobi": 1.0, "chhobi": 1.0, "screen": 0.0, "porda": 1.0, "alo": 0.8,
    "andhar": 1.0, "alor": 0.8, "sound": 0.0, "awaz": 1.0, "aawaz": 1.0,
    # copula / common phrases
    "acho": 1.0, "achhe": 1.0, "ache": 0.9, "aache": 0.9, "nei": 0.9, "naki": 0.9,
    "kemon": 1.0, "kemn": 1.0, "obostha": 1.0, "avostha": 1.0, "thik": 0.7, "thikachhe": 1.0,
    "accha": 0.8, "acha": 0.8, "tai": 0.7, "are": 0.3, "arre": 0.7, "ore": 0.6,
    "besh": 0.7, "maja": 0.9, "moja": 0.9, "kaj": 0.8, "kaaj": 0.8, "somoy": 0.9,
    "shomoy": 0.9, "din": 0.6, "rat": 0.7, "raat": 0.7, "bari": 0.8, "ghor": 0.8,
    "khabar": 0.9, "khabor": 0.9, "pani": 0.9, "jal": 0.7, "cha": 0.6, "coffee": 0.0,
    "hoy": 0.7, "hoye": 0.7, "geche": 0.9, "gelo": 0.8, "kora": 0.8, "hobe": 0.9,
    "lagbe": 1.0, "lagchhe": 1.0, "lagche": 0.9, "chai": 0.8, "chao": 0.8,
    "dorkar": 1.0, "para": 0.5, "parbe": 1.0, "parben": 1.0, "jan": 0.4,
    "jani": 0.6, "bolto": 1.0, "bolna": 1.0, "shunte": 0.9, "dekhte": 0.9,
}

# Common English words — used by ``prefer_english`` (from Backend/bridge.py).
_EN_WORDS: Final[frozenset[str]] = frozenset(
    """open launch close play search volume sound mute unmute brightness how are you who what is
    your my name calculate screenshot lock the a an please can could would tell me time date today
    tomorrow weather news stop start set increase decrease take read screen song music video on for
    to hello hi hey thanks thank good morning night remember remind when where why which do does did
    i we it this that and""".split()
)

_WAKE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*(?:hey |ok |oi |aye |o )?(?:star|স্টার|staj|স্টর)\b[!,.।]*\s*", re.IGNORECASE),
    re.compile(r"^\s*(?:শোনো|শোন|shono|shun|suno|বলো|বল|bolo|hey|হেই)\s+", re.IGNORECASE),
)

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-zA-Z']+")


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    """Result of language detection."""

    code: str                      # "bn" | "en" | "mixed"
    script: str                    # "bengali" | "latin" | "mixed"
    banglish: bool                 # romanised Bengali
    confidence: float              # 0..1
    bengali_chars: int = 0
    latin_chars: int = 0
    matched_tokens: tuple[str, ...] = ()

    @property
    def is_bengali(self) -> bool:
        return self.code == "bn" or (self.code == "mixed" and self.script != "latin")

    @property
    def spoken_form(self) -> str:
        """Human label used in the HUD/console."""
        if self.banglish:
            return "Banglish"
        return {"bn": "Bengali", "en": "English", "mixed": "Mixed"}.get(self.code, self.code)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "script": self.script,
            "banglish": self.banglish,
            "confidence": round(self.confidence, 3),
            "spoken_form": self.spoken_form,
            "matched_tokens": list(self.matched_tokens),
        }


def _counts(text: str) -> tuple[int, int]:
    bengali = sum(1 for ch in text if _BN_START <= ord(ch) <= _BN_END)
    latin = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    return bengali, latin


def _banglish_score(text: str) -> tuple[float, tuple[str, ...]]:
    lowered = text.lower()
    score = 0.0
    hits: list[str] = []
    for word in _WORD_RE.findall(lowered):
        weight = BANGLISH_LEXICON.get(word)
        if weight and weight > 0:
            score += weight
            hits.append(word)
    return score, tuple(dict.fromkeys(hits))


def detect_language(text: str) -> LanguageProfile:
    """Classify a transcript/query. Pure, deterministic, <1 ms."""
    text = (text or "").strip()
    if not text:
        return LanguageProfile(code="en", script="latin", banglish=False, confidence=0.0)

    bengali, latin = _counts(text)
    score, hits = _banglish_score(text)

    if bengali and not latin:
        return LanguageProfile("bn", "bengali", False, min(1.0, 0.7 + bengali / 40.0), bengali, latin, hits)
    if bengali and latin:
        dominant = "bengali" if bengali >= latin else "latin"
        confidence = min(1.0, (min(bengali, latin) / max(1, max(bengali, latin))) + 0.5)
        return LanguageProfile("mixed", "mixed", score >= 2.0, confidence, bengali, latin, hits)
    if score >= 1.6 and hits:
        # Romanised Bengali: "volume ta ektu barao", "gaan chalaao"
        confidence = min(1.0, 0.45 + score / 6.0)
        return LanguageProfile("bn", "latin", True, confidence, bengali, latin, hits)
    if score >= 0.8 and len(_WORD_RE.findall(text)) <= 3:
        return LanguageProfile("bn", "latin", True, 0.5, bengali, latin, hits)
    english_hits = sum(1 for word in _WORD_RE.findall(text.lower()) if word in _EN_WORDS)
    confidence = min(1.0, 0.5 + english_hits / 8.0) if latin else 0.3
    return LanguageProfile("en", "latin", False, confidence, bengali, latin, hits)


# Leading tokens that are wake words, not content. Deliberately excludes plain
# greetings ("hello", "hi") — those ARE the message when nothing follows them.
_WAKE_TOKENS: Final[frozenset[str]] = frozenset(
    """hey heyy o oi aye ok okay star stars স্টার স্টর staj shono shun suno শোনো শোন
    bolo bol বলো বল shon হেই ওই আয় শুন""".split()
)


def strip_wake_word(text: str, *, max_tokens: int = 3) -> str:
    """Remove leading wake words ("hey star, …", "শোনো স্টার …") without eating content.

    Token-based (not regex-based) so mixed scripts work: "হেই star, ভলিউম বাড়াও"
    → "ভলিউম বাড়াও". A message that is *only* a wake word is left alone.
    """
    cleaned = (text or "").strip()
    for _ in range(max(0, max_tokens)):
        match = re.match(r"^\s*([^\s,.-]+)[\s,.-]+(.+)$", cleaned, flags=re.DOTALL)
        if match is None:
            break
        head, rest = match.group(1), match.group(2)
        if head.lower().strip("।!?:") not in _WAKE_TOKENS:
            break
        if not rest.strip():
            break
        cleaned = rest.strip()
    return cleaned.strip(" ,.-।!?\t")


def normalize_text(text: str, *, strip_wake: bool = True) -> str:
    """Bengali digits → ASCII, collapse whitespace, optional wake-word strip."""
    cleaned = (text or "").translate(_BN_DIGIT_TRANS)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if strip_wake:
        cleaned = strip_wake_word(cleaned)
    return cleaned.strip()


def prefer_english(transcript_en: str, transcript_bn: str) -> bool:
    """Decide between parallel bn-IN and en-IN recognitions.

    Extracted verbatim in behaviour from ``Backend/bridge.py::VoiceListener._prefer_english``:
    the bn recognizer often transliterates English into Bengali script, so English
    wins when it contains ≥2 common English words (or is a 1–2 word command).
    """
    words = _WORD_RE.findall((transcript_en or "").lower())
    if not words:
        return False
    hits = sum(1 for word in words if word in _EN_WORDS)
    if hits >= 2:
        return True
    return len(words) <= 2 and words[0] in _EN_WORDS


def response_language(profile: LanguageProfile, *, default: str = "bn") -> str:
    """Which language Star answers in.

    Bengali or Banglish → Bengali. English → English. Mixed → the user's configured
    default (Bengali by default: Star is a Bengali-first companion).
    """
    if profile.code == "bn":
        return "bn"
    if profile.code == "en":
        return "en"
    return default if default in {"bn", "en"} else "bn"


def stt_locales(profile: LanguageProfile | None = None) -> tuple[str, ...]:
    """Recognition locales to query, best first."""
    if profile is None:
        return ("bn-IN", "en-IN")
    if profile.code == "en" and not profile.banglish:
        return ("en-IN", "bn-IN")
    return ("bn-IN", "en-IN")


_BN_VOICES: Final[tuple[str, ...]] = (
    "bn-IN-BashkarNeural",
    "bn-IN-TanishaaNeural",
    "bn-BD-PradeepNeural",
    "bn-BD-NabanitaNeural",
)
_EN_VOICES: Final[tuple[str, ...]] = (
    "en-IN-NeerjaExpressiveNeural",
    "en-IN-NeerjaNeural",
    "en-GB-SoniaNeural",
)


def tts_voice_for(language: str, *, preferred: str = "") -> str:
    """Pick a neural voice for the response language, honouring the user's default."""
    if preferred and preferred.startswith(("bn", "en")):
        matches = "bn" if language == "bn" else "en"
        if preferred.startswith(matches):
            return preferred
    return _BN_VOICES[0] if language == "bn" else _EN_VOICES[0]


def voice_chain(language: str) -> tuple[str, ...]:
    """Fallback chain for a language (same order the existing VoiceEngine uses)."""
    return _BN_VOICES if language == "bn" else _EN_VOICES
