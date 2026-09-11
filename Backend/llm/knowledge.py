"""
═══════════════════════════════════════════════════════════════════════════════
  ★ STAR ASSISTANT — ULTRA HUMAN-TYPE COMPANION KNOWLEDGE ENGINE v3.0 ★
═══════════════════════════════════════════════════════════════════════════════

  A deeply human-like semantic companion brain that understands:
    • Intent (what you WANT, not just what you SAID)
    • Emotion (how you FEEL right now)
    • Context (what we talked about 30 seconds ago)
    • Entities (names, numbers, apps, times)
    • Code-switching (Bengali ↔ Banglish ↔ English mid-sentence)
    • Paraphrase (10 different ways to ask the same thing)
    • Time (morning greeting vs midnight comfort)
    • Typos (because humans make them)
    • Personality (warm, witty, caring — never robotic)

  Architecture:
    Layer 0 → Preprocessing & Normalization (typo-fix, stem, transliterate)
    Layer 1 → Intent Classification (40+ intent patterns)
    Layer 2 → Emotion / Mood Detection (Bengali + Banglish sentiment)
    Layer 3 → Entity Extraction (apps, numbers, times, names)
    Layer 4 → Hierarchical Search Pipeline
               Exact → N-gram → BM25 Semantic → Fuzzy → LLM Fallback
    Layer 5 → Response Variation & Personalization
    Layer 6 → Proactive Context (time-of-day, system state, memory)
    Layer 7 → Tool Execution & Action Orchestration
    Layer 8 → Conversation Memory (sliding window + topic tracking)

  Performance target: <25ms for 5,000+ samples on commodity hardware.
═══════════════════════════════════════════════════════════════════════════════
"""

import json
import re
import math
import time
import random
import difflib
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict, deque
from typing import List, Dict, Any, Optional, Tuple, Set
from dataclasses import dataclass, field

from ..config import PROJECT_ROOT
from ..tools.registry import execute_tool


# ═══════════════════════════════════════════════════════════════════════════════
#  DATASET PATHS
# ═══════════════════════════════════════════════════════════════════════════════

DATASET_PATHS = [
    PROJECT_ROOT / "data" / "train.jsonl",
    PROJECT_ROOT / "data" / "train_all.jsonl",
    PROJECT_ROOT / "data" / "bengali_conversations.jsonl",
    PROJECT_ROOT / "data" / "action_dialogues.jsonl",
    PROJECT_ROOT / "use and throw" / "train.jsonl",
    PROJECT_ROOT / "use and throw" / "train_all.jsonl",
]


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER 0 — MASSIVE BANGLISH ↔ BENGALI ↔ ENGLISH CONCEPT MAP
# ═══════════════════════════════════════════════════════════════════════════════

BANGLISH_CONCEPTS: Dict[str, str] = {
    # ── Weather & Environment ──
    "weather": "আবহাওয়া", "abohawa": "আবহাওয়া", "abohaoa": "আবহাওয়া",
    "abohar": "আবহাওয়া", "risti": "বৃষ্টি", "brishti": "বৃষ্টি",
    "bristi": "বৃষ্টি", "rain": "বৃষ্টি", "rodro": "রোদ", "rod": "রোদ",
    "sun": "রোদ", "cloud": "মেঘ", "megh": "মেঘ", "meg": "মেঘ",
    "garam": "গরম", "gorom": "গরম", "hot": "গরম",
    "thanda": "ঠান্ডা", "thana": "ঠান্ডা", "cold": "ঠান্ডা",
    "shita": "শীত", "sheet": "শীত", "winter": "শীত",
    "summer": "গ্রীষ্ম", "grishmo": "গ্রীষ্ম",
    "batash": "বাতাস", "hawa": "বাতাস", "wind": "বাতাস",
    "storm": "ঝড়", "jhor": "ঝড়", "kalboishakhi": "কালবৈশাখী",

    # ── Battery & Power ──
    "battery": "ব্যাটারি", "batteri": "ব্যাটারি", "battary": "ব্যাটারি",
    "charge": "চার্জ", "charj": "চার্জ", "charging": "চার্জ",
    "power": "পাওয়ার", "low": "কম", "full": "পুরো",

    # ── Relationships & People ──
    "friend": "বন্ধু", "bondhu": "বন্ধু", "bondh": "বন্ধু",
    "bhai": "ভাই", "bro": "ভাই", "brother": "ভাই",
    "sister": "বোন", "bon": "বোন", "apa": "আপু",
    "mama": "মামা", "mamu": "মামু", "chacha": "চাচা",
    "baba": "বাবা", "father": "বাবা", "ma": "মা", "mother": "মা",
    "amma": "আম্মা", "bhaiya": "ভাইয়া", "dada": "দাদা",
    "didi": "দিদি", "girlfriend": "গার্লফ্রেন্ড", "gf": "গার্লফ্রেন্ড",
    "boyfriend": "বয়ফ্রেন্ড", "bf": "বয়ফ্রেন্ড",
    "crush": "ক্রাশ", "love": "ভালোবাসা", "valobasha": "ভালোবাসা",
    "premik": "প্রেমিক", "premika": "প্রেমিকা",

    # ── Feelings & Emotions ──
    "bhalo": "ভালো", "valo": "ভালো", "bhaloi": "ভালোই",
    "kharap": "খারাপ", "kharab": "খারাপ",
    "mon": "মন", "mone": "মনে", "moner": "মনের",
    "khusi": "খুশি", "khushi": "খুশি", "happy": "খুশি",
    "dukho": "দুঃখ", "dukkho": "দুঃখ", "sad": "দুঃখ",
    "kosto": "কষ্ট", "koshto": "কষ্ট", "pain": "কষ্ট",
    "rag": "রাগ", "angry": "রাগ", "gossa": "রাগ", "gossha": "রাগ",
    "voy": "ভয়", "fear": "ভয়", "bhoy": "ভয়",
    "tension": "টেনশন", "tenshun": "টেনশন",
    "stress": "স্ট্রেস", "worry": "চিন্তা", "chinta": "চিন্তা",
    "lonely": "একাকী", "ekaki": "একাকী", "ekla": "একা",
    "bored": "বোরিং", "boring": "বিরক্তিকর", "birkto": "বিরক্ত",
    "excited": "উত্তেজিত", "uttijito": "উত্তেজিত",
    "shanti": "শান্তি", "peace": "শান্তি", "relax": "আরাম",
    "aram": "আরাম", "comfort": "স্বস্তি", "swosti": "স্বস্তি",
    "ghum": "ঘুম", "sleep": "ঘুম", "sleepy": "ঘুমঘুম",
    "ghumghum": "ঘুমঘুম", "tired": "ক্লান্ত", "klanto": "ক্লান্ত",
    "matha": "মাথা", "betha": "ব্যথা", "byatha": "ব্যথা",
    "mathabatha": "মাথাব্যথা", "headache": "মাথাব্যথা",
    "pet": "পেট", "peth": "পেট", "stomach": "পেট",
    "jwor": "জ্বর", "fever": "জ্বর", "buk": "বুক",
    "heart": "হৃদয়", "hridoy": "হৃদয়",

    # ── Time & Schedule ──
    "somoy": "সময়", "time": "সময়", "koyta": "কয়টা", "koita": "কয়টা",
    "baje": "বাজে", "bajche": "বাজছে", "ekhon": "এখন", "now": "এখন",
    "ajke": "আজ", "aaj": "আজ", "today": "আজ",
    "kal": "কাল", "tomorrow": "আগামীকাল", "agami": "আগামী",
    "gatakal": "গতকাল", "yesterday": "গতকাল",
    "shokal": "সকাল", "morning": "সকাল", "sokal": "সকাল",
    "dupur": "দুপুর", "noon": "দুপুর", "afternoon": "বিকাল",
    "bikal": "বিকাল", "shondha": "সন্ধ্যা", "sandhya": "সন্ধ্যা",
    "evening": "সন্ধ্যা", "raat": "রাত", "rat": "রাত", "night": "রাত",
    "din": "দিন", "day": "দিন", "saptah": "সপ্তাহ", "week": "সপ্তাহ",
    "mash": "মাস", "month": "মাস", "bochor": "বছর", "year": "বছর",

    # ── Device Controls ──
    "alo": "আলো", "light": "আলো", "brightness": "ব্রাইটনেস",
    "bright": "ব্রাইটনেস", "screen": "স্ক্রিন",
    "sound": "ভলিউম", "volume": "ভলিউম", "awaj": "ভলিউম",
    "awaz": "ভলিউম", "saund": "ভলিউম", "saound": "ভলিউম",
    "soundta": "ভলিউম", "volumeta": "ভলিউম", "shobdo": "শব্দ",
    "mute": "মিউট", "chup": "চুপ", "silent": "নীরব",

    # ── Actions & Verbs ──
    "chalu": "খোলো", "kholo": "খোলো", "open": "খোলো",
    "launch": "খোলো", "start": "শুরু", "shuru": "শুরু",
    "chalao": "চালাও", "bajao": "চালাও", "play": "চালাও",
    "bondho": "বন্ধ", "close": "বন্ধ", "stop": "থামাও",
    "thamao": "থামাও", "thamo": "থামো",
    "dim": "কমিয়ে", "komao": "কমিয়ে", "komie": "কমিয়ে",
    "komano": "কমিয়ে", "komiye": "কমিয়ে", "reduce": "কমিয়ে",
    "barao": "বাড়িয়ে", "barie": "বাড়িয়ে", "barano": "বাড়িয়ে",
    "bariye": "বাড়িয়ে", "increase": "বাড়িয়ে",
    "de": "দাও", "dao": "দাও", "give": "দাও", "bolo": "বলো",
    "bol": "বল", "tell": "বলো", "show": "দেখাও", "dekhao": "দেখাও",
    "koro": "করো", "do": "করো", "help": "সাহায্য", "sahajjo": "সাহায্য",
    "search": "খোঁজো", "khojo": "খোঁজো", "find": "খুঁজো",
    "khuj": "খুঁজো", "khunje": "খুঁজে",

    # ── Applications ──
    "chrome": "ক্রোম", "browser": "ক্রোম", "google": "গুগল",
    "calculator": "ক্যালকুলেটর", "calc": "ক্যালকুলেটর", "hisab": "হিসাব",
    "notepad": "নোটপ্যাড", "note": "নোট",
    "code": "ভিএস কোড", "vscode": "ভিএস কোড", "vs": "ভিএস",
    "youtube": "ইউটিউব", "yt": "ইউটিউব",
    "spotify": "স্পটিফাই", "music": "গান", "gaan": "গান",
    "song": "গান", "gan": "গান", "gana": "গান",
    "whatsapp": "হোয়াটসঅ্যাপ", "fb": "ফেসবুক", "facebook": "ফেসবুক",
    "instagram": "ইনস্টাগ্রাম", "insta": "ইনস্টাগ্রাম",
    "telegram": "টেলিগ্রাম", "paint": "পেইন্ট",
    "taskmgr": "টাস্ক ম্যানেজার", "explorer": "ফাইল",
    "vlc": "ভিএলসি", "zoom": "জুম", "teams": "টিমস",

    # ── Knowledge & Queries ──
    "ki": "কী", "keno": "কেন", "kivabe": "কীভাবে", "kothay": "কোথায়",
    "ke": "কে", "kon": "কোন", "koto": "কত", "meaning": "অর্থ",
    "orth": "অর্থ", "mane": "মানে", "artha": "অর্থ",
    "jano": "জানো", "jan": "জান", "know": "জানো",
    "shikhte": "শিখতে", "learn": "শেখো", "porashona": "পড়াশোনা",
    "study": "পড়াশোনা", "exam": "পরীক্ষা", "porikkha": "পরীক্ষা",

    # ── Greetings & Social ──
    "hello": "হ্যালো", "hi": "হাই", "hey": "হেই",
    "salam": "সালাম", "assalamu": "সালাম", "nomoskar": "নমস্কার",
    "kemon": "কেমন", "achho": "আছো", "acho": "আছো",
    "khobor": "খবর", "dhonnobad": "ধন্যবাদ", "thanks": "ধন্যবাদ",
    "thank": "ধন্যবাদ", "sorry": "দুঃখিত", "maf": "মাফ", "forgive": "ক্ষমা",

    # ── Food & Daily Life ──
    "khabar": "খাবার", "food": "খাবার", "eat": "খাও",
    "kheye": "খেয়ে", "khai": "খাই", "khele": "খেলো",
    "pani": "পানি", "jol": "জল", "water": "পানি",
    "cha": "চা", "tea": "চা", "coffee": "কফি",
    "bhat": "ভাত", "rice": "ভাত", "ruti": "রুটি",
    "restaurant": "রেস্তোরাঁ", "hotel": "হোটেল",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  STOPWORDS (Bengali + Banglish + English fillers)
# ═══════════════════════════════════════════════════════════════════════════════

STOPWORDS: Set[str] = {
    # Banglish
    "tumi", "amar", "amake", "ami", "ki", "koro", "korcho", "korchi",
    "ta", "na", "ekhon", "bhai", "dada", "bolo", "aro", "eto", "je",
    "ar", "ei", "oi", "tai", "hobe", "hoyeche", "ache", "achhe",
    "theke", "kor", "kore", "korte", "parbo", "parbe", "pari",
    "bolche", "to", "toke", "tore", "amra", "tomra",
    "apni", "apnar", "tar", "tader", "amader", "tomader", "shob",
    "kicu", "kichu", "kono", "kon", "ekta", "eita", "oita", "eta",
    "ota", "jodi", "tahole", "bole", "bolte", "bolchi", "bolcho",
    "jacchi", "jachi", "jabe", "jabo", "geche", "giye", "gelo",
    "hoy", "hocche", "hoye", "thakbe", "thakle", "thak",
    "thake", "thaki", "thakbo", "diye", "niye", "peye",
    "kheye", "reye", "boye", "soye", "dhore", "mone", "jone",
    # English
    "the", "a", "an", "is", "am", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
    "shall", "must", "need", "dare", "ought", "used", "to",
    "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how",
    "all", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "just", "because", "but", "and", "or", "if",
    "while", "about", "up", "down", "it", "its", "i", "me", "my",
    "we", "our", "you", "your", "he", "him", "his", "she", "her",
    "they", "them", "their", "this", "that", "these", "those",
    "what", "which", "who", "whom", "please", "kindly",
    # Bengali
    "তুমি", "আমি", "আমার", "আমাকে", "কী", "কি", "করো", "করছো",
    "করছি", "না", "এখন", "ভাই", "দাদা", "বলো", "বলছে", "আর",
    "এই", "ওই", "তাই", "হবে", "হয়েছে", "আছে", "থেকে", "করে",
    "করতে", "পারবো", "পারবে", "পারি", "তো", "তোকে", "তোরে",
    "আমরা", "তোমরা", "আপনি", "আপনার", "তার", "তাদের", "আমাদের",
    "তোমাদের", "সব", "কিছু", "কোনো", "একটা", "এটা", "ওটা",
    "যদি", "তাহলে", "বলে", "বলতে", "বলছি", "বলছো", "যাচ্ছি",
    "যাবে", "যাবো", "গেছে", "গিয়ে", "গেলো", "হয়", "হচ্ছে",
    "হয়ে", "থাকবে", "থাকলে", "থাক", "থাকে", "থাকি", "থাকবো",
    "দিয়ে", "নিয়ে", "পেয়ে", "খেয়ে", "মনে", "জেনে",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DialogueSample:
    """Rich representation of a single training dialogue pair."""
    raw_user: str
    norm_user: str
    tokens: Set[str]
    content_tokens: Set[str]
    bigrams: Set[str]
    trigrams: Set[str]
    assistant_reply: str
    intent: str = "general"
    emotion: str = "neutral"
    entities: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationTurn:
    """Single turn in conversation memory."""
    timestamp: float
    user_text: str
    assistant_text: str
    intent: str
    emotion: str
    confidence: float


@dataclass
class MatchResult:
    """Structured match result with full metadata."""
    response: str
    confidence: float
    intent: str
    emotion: str
    source: str
    actions: List[Dict[str, Any]]
    matched_sample: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER 1 — INTENT CLASSIFICATION ENGINE (40+ intents)
# ═══════════════════════════════════════════════════════════════════════════════

INTENT_PATTERNS: Dict[str, List[str]] = {
    "greeting": [
        "hello", "hi", "hey", "হ্যালো", "হাই", "হেই", "সালাম",
        "আসসালামু", "নমস্কার", "সুপ্রভাত", "শুভ সন্ধ্যা", "গুড মর্নিং",
        "good morning", "good evening", "good night", "sup", "wassup",
        "yo", "kemn aso", "kemon aso", "kemon acho", "কেমন আছো",
        "কেমন আছ", "kemon achho", "ki khobor", "কী খবর", "ki obostha",
    ],
    "farewell": [
        "bye", "goodbye", "বিদায়", "আসি", "চলি", "যাই",
        "good night", "শুভ রাত্রি", "shubho ratri", "tata", "cya",
        "see you", "আবার দেখা হবে", "rakh", "আচ্ছা রাখি",
    ],
    "gratitude": [
        "ধন্যবাদ", "thanks", "thank you", "shukria", "dhonnobad",
        "অনেক ধন্যবাদ", "many thanks", "tumi best", "তুমি সেরা",
        "awesome", "great job", "valo korecho", "ভালো করেছো",
    ],
    "apology": [
        "sorry", "দুঃখিত", "maf koro", "মাফ করো", "ভুল হয়ে গেছে",
        "ami bhul korechi", "আমি ভুল করেছি", "forgive", "ক্ষমা",
    ],
    "identity_query": [
        "তুমি কে", "tumi ke", "ke tumi", "কে তুমি", "তোমার নাম",
        "tomar nam", "your name", "who are you", "ki tumi",
        "তুমি কী", "what are you", "তোমার পরিচয়",
    ],
    "capability_query": [
        "কী করতে পারো", "ki korte paro", "what can you do",
        "তোমার ক্ষমতা", "ki ki paro", "কী কী পারো",
        "help me", "সাহায্য করো", "sahajjo koro",
    ],
    "open_app": [
        "খোলো", "kholo", "চালু করো", "chalu koro", "launch",
        "open", "চালাও", "chalao", "start", "শুরু করো",
    ],
    "close_app": [
        "বন্ধ করো", "bondho koro", "close", "kill", "থামাও",
        "thamao", "বন্ধ", "bondho", "shut", "exit",
    ],
    "volume_up": [
        "ভলিউম বাড়াও", "volume barao", "sound barie", "আওয়াজ বাড়াও",
        "volume up", "শব্দ বাড়াও", "আওয়াজ বাড়িয়ে", "volume bariye",
    ],
    "volume_down": [
        "ভলিউম কমাও", "volume komao", "sound komie", "আওয়াজ কমাও",
        "volume down", "শব্দ কমাও", "আওয়াজ কমিয়ে", "volume komiye",
    ],
    "mute": [
        "মিউট", "mute", "চুপ", "chup", "শব্দ বন্ধ", "sound off",
        "silent", "নীরব", "আওয়াজ বন্ধ",
    ],
    "brightness_up": [
        "আলো বাড়াও", "brightness barao", "bright barie", "স্ক্রিন বাড়াও",
        "brightness up", "আলো বাড়িয়ে", "alo bariye",
    ],
    "brightness_down": [
        "আলো কমাও", "brightness komao", "dim", "স্ক্রিন কমাও",
        "brightness down", "আলো কমিয়ে", "alo komiye",
    ],
    "time_query": [
        "কয়টা বাজে", "koyta baje", "what time", "সময় কত",
        "somoy koto", "time koto", "কটা বাজে", "koita baje",
    ],
    "date_query": [
        "আজ কত তারিখ", "aj koto tarikh", "what date", "today date",
        "আজকের তারিখ", "kon din", "কোন দিন",
    ],
    "battery_query": [
        "ব্যাটারি কত", "battery koto", "charge koto", "চার্জ কত",
        "battery status", "ব্যাটারি স্ট্যাটাস",
    ],
    "weather_query": [
        "আবহাওয়া কেমন", "abohawa kemon", "weather", "বৃষ্টি হবে",
        "brishti hobe", "আজকে গরম", "আজকে ঠান্ডা", "weather report",
    ],
    "music_play": [
        "গান চালাও", "gaan chalao", "play music", "গান বাজাও",
        "gaan bajao", "play song", "গান শোনাও", "music play",
    ],
    "web_search": [
        "সার্চ করো", "search koro", "খুঁজো", "khunje de",
        "google e khojo", "গুগলে খোঁজো", "search for",
    ],
    "youtube_search": [
        "ইউটিউবে", "youtube e", "yt te", "ইউটিউবে খোঁজো",
        "youtube search", "ইউটিউবে সার্চ",
    ],
    "joke_request": [
        "জোক বলো", "joke bolo", "মজা করো", "moja koro",
        "funny", "হাসাও", "hasao", "tell joke", "রসিকতা",
    ],
    "story_request": [
        "গল্প বলো", "golpo bolo", "tell story", "একটা গল্প",
        "ekta golpo", "story shonao", "গল্প শোনাও",
    ],
    "motivation": [
        "মোটিভেশন", "motivation", "অনুপ্রেরণা", "inspire",
        "উৎসাহ দাও", "utsaho dao", "হতাশ", "hopeless",
        "হার মানতে চাই", "give up", "পারছি না", "pari na",
    ],
    "sadness": [
        "মন খারাপ", "mon kharap", "মন খুব খারাপ", "কষ্ট হচ্ছে",
        "kosto hocche", "কাঁদছি", "kadchi", "একা লাগছে",
        "ekla lagche", "দুঃখ", "dukho", "sad",
    ],
    "anger": [
        "রাগ হচ্ছে", "rag hocche", "গোসসা", "gossa", "বিরক্ত",
        "birkto", "মাথা গরম", "matha gorom", "angry",
    ],
    "love_talk": [
        "ভালোবাসি", "valobashi", "i love you", "প্রেম", "prem",
        "crush", "ক্রাশ", "মনে পড়ে", "mone pore", "miss",
    ],
    "health_query": [
        "মাথা ব্যথা", "matha betha", "জ্বর", "jwor", "অসুস্থ",
        "oshustho", "sick", "পেট ব্যথা", "pet betha", "fever",
    ],
    "food_query": [
        "খাবার", "khabar", "কী খাবো", "ki khabo", "খিদে",
        "khide", "hungry", "রেসিপি", "recipe", "রান্না", "ranna",
    ],
    "math_query": [
        "হিসাব", "hisab", "ক্যালকুলেটর", "calculator", "যোগ",
        "বিয়োগ", "গুণ", "ভাগ", "math", "calculate",
    ],
    "system_status": [
        "সিস্টেম", "system", "স্ট্যাটাস", "status", "কেমন চলছে",
        "kemon cholche", "pc status", "কম্পিউটার",
    ],
    "screenshot": [
        "screenshot", "স্ক্রিনশট", "স্ক্রিন শট", "ছবি তোলো",
    ],
    "lock_pc": [
        "lock pc", "পিসি লক", "স্ক্রিন লক", "লক করো",
    ],
    "compliment": [
        "তুমি সেরা", "tumi sera", "you are best", "অসাধারণ",
        "osadharon", "amazing", "wonderful", "চমৎকার", "chomotkar",
    ],
    "insult": [
        "বোকা", "boka", "stupid", "idiot", "গাধা", "gadha",
        "useless", "কাজের না", "kajer na", "ফালতু", "faltu",
    ],
    "philosophy": [
        "জীবনের মানে", "jiboner mane", "meaning of life",
        "মৃত্যু", "mrittu", "death", "ঈশ্বর", "god", "আল্লাহ",
        "allah", "ভগবান", "কেন আছি", "keno achi",
    ],
    "study_help": [
        "পড়াশোনা", "porashona", "study", "পরীক্ষা", "porikkha",
        "exam", "পড়তে", "porte", "শেখাও", "shekhao",
    ],
    "translation": [
        "অনুবাদ", "onubad", "translate", "মানে কী", "mane ki",
        "meaning", "অর্থ", "orth", "ইংরেজিতে", "english e",
    ],
    "reminder": [
        "মনে করিয়ে", "mone koriye", "remind", "রিমাইন্ড",
        "ভুলে যাই", "bhule jai", "alarm", "অ্যালার্ম",
    ],
    "news_query": [
        "খবর", "khobor", "news", "সংবাদ", "shongbad",
        "আজকের খবর", "ajker khobor", "headline",
    ],
    "sleep_talk": [
        "ঘুম আসছে না", "ghum asche na", "can't sleep", "insomnia",
        "ঘুম পাচ্ছে", "ghum pacche", "sleepy", "শুয়ে পড়",
    ],
}


def classify_intent(text: str) -> str:
    """Classify user intent from normalized text using weighted pattern matching."""
    if not text:
        return "general"

    t = text.lower().strip()
    scores: Dict[str, float] = defaultdict(float)

    for intent, patterns in INTENT_PATTERNS.items():
        for pattern in patterns:
            p = pattern.lower()
            if p in t:
                scores[intent] += 3.0 * (len(p) / max(len(t), 1))
            p_tokens = set(p.split())
            t_tokens = set(t.split())
            overlap = p_tokens & t_tokens
            if overlap:
                scores[intent] += 1.5 * (len(overlap) / max(len(p_tokens), 1))

    if not scores:
        return "general"

    best_intent = max(scores, key=scores.get)
    if scores[best_intent] < 0.5:
        return "general"
    return best_intent


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER 2 — EMOTION / MOOD DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

EMOTION_LEXICON: Dict[str, Tuple[str, float]] = {
    "খুশি": ("happy", 0.8), "khushi": ("happy", 0.8), "happy": ("happy", 0.7),
    "আনন্দ": ("happy", 0.9), "anondo": ("happy", 0.9),
    "হাসি": ("happy", 0.6), "hashi": ("happy", 0.6),
    "মজা": ("happy", 0.7), "moja": ("happy", 0.7),
    "দুঃখ": ("sad", 0.8), "dukho": ("sad", 0.8), "sad": ("sad", 0.7),
    "কষ্ট": ("sad", 0.9), "kosto": ("sad", 0.9), "কান্না": ("sad", 0.9),
    "কাঁদছি": ("sad", 0.9), "kadchi": ("sad", 0.9),
    "মন খারাপ": ("sad", 0.85), "mon kharap": ("sad", 0.85),
    "একাকী": ("sad", 0.7), "ekaki": ("sad", 0.7), "lonely": ("sad", 0.7),
    "একা": ("sad", 0.6), "ekla": ("sad", 0.6),
    "রাগ": ("angry", 0.8), "rag": ("angry", 0.8), "angry": ("angry", 0.7),
    "গোসসা": ("angry", 0.8), "gossa": ("angry", 0.8),
    "বিরক্ত": ("angry", 0.6), "birkto": ("angry", 0.6),
    "ভয়": ("fear", 0.8), "voy": ("fear", 0.8), "fear": ("fear", 0.7),
    "ভয়ঙ্কর": ("fear", 0.9), "bhoyonkor": ("fear", 0.9),
    "টেনশন": ("anxious", 0.7), "tension": ("anxious", 0.7),
    "স্ট্রেস": ("anxious", 0.7), "stress": ("anxious", 0.7),
    "চিন্তা": ("anxious", 0.6), "chinta": ("anxious", 0.6),
    "ঘুম": ("tired", 0.5), "ghum": ("tired", 0.5), "sleepy": ("tired", 0.6),
    "ক্লান্ত": ("tired", 0.7), "klanto": ("tired", 0.7), "tired": ("tired", 0.7),
    "ভালোবাসা": ("love", 0.9), "valobasha": ("love", 0.9),
    "love": ("love", 0.8), "ভালোবাসি": ("love", 0.9),
    "miss": ("love", 0.6), "মনে পড়ে": ("love", 0.7),
    "উত্তেজিত": ("excited", 0.8), "excited": ("excited", 0.8),
    "অসাধারণ": ("excited", 0.7), "amazing": ("excited", 0.7),
    "বোরিং": ("bored", 0.6), "boring": ("bored", 0.6),
    "শান্তি": ("calm", 0.7), "shanti": ("calm", 0.7), "peace": ("calm", 0.7),
    "আরাম": ("calm", 0.6), "aram": ("calm", 0.6), "relax": ("calm", 0.6),
}


def detect_emotion(text: str) -> Tuple[str, float]:
    """Detect dominant emotion and intensity from text."""
    if not text:
        return "neutral", 0.0

    t = text.lower()
    emotion_scores: Dict[str, float] = defaultdict(float)

    for phrase, (emotion, intensity) in EMOTION_LEXICON.items():
        if " " in phrase and phrase in t:
            emotion_scores[emotion] += intensity * 1.5

    tokens = set(t.split())
    for word, (emotion, intensity) in EMOTION_LEXICON.items():
        if " " not in word and word in tokens:
            emotion_scores[emotion] += intensity

    for word, (emotion, intensity) in EMOTION_LEXICON.items():
        if " " not in word and len(word) > 3 and word in t:
            emotion_scores[emotion] += intensity * 0.5

    if not emotion_scores:
        return "neutral", 0.0

    dominant = max(emotion_scores, key=emotion_scores.get)
    intensity = min(emotion_scores[dominant], 1.0)
    return dominant, intensity


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER 3 — ENTITY EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

APP_ENTITY_MAP: Dict[str, List[str]] = {
    "chrome": ["chrome", "ক্রোম", "গুগল ক্রোম", "browser", "ব্রাউজার"],
    "notepad": ["notepad", "নোটপ্যাড", "নোট", "note"],
    "calc": ["calculator", "ক্যালকুলেটর", "হিসাব", "calc", "hisab"],
    "paint": ["paint", "পেইন্ট"],
    "whatsapp": ["whatsapp", "হোয়াটসঅ্যাপ", "হোয়াটস"],
    "code": ["vs code", "ভিএস কোড", "vscode", "কোড"],
    "spotify": ["spotify", "স্পটিফাই"],
    "vlc": ["vlc", "ভিএলসি", "মিডিয়া প্লেয়ার"],
    "taskmgr": ["task manager", "টাস্ক ম্যানেজার", "taskmgr"],
    "explorer": ["file explorer", "ফাইল", "এক্সপ্লোরার", "explorer"],
    "youtube": ["youtube", "ইউটিউব", "yt"],
    "facebook": ["facebook", "ফেসবুক", "fb"],
    "instagram": ["instagram", "ইনস্টাগ্রাম", "insta"],
    "telegram": ["telegram", "টেলিগ্রাম"],
    "zoom": ["zoom", "জুম"],
    "teams": ["teams", "টিমস", "microsoft teams"],
    "word": ["word", "ওয়ার্ড", "ms word"],
    "excel": ["excel", "এক্সেল"],
    "powerpoint": ["powerpoint", "পাওয়ারপয়েন্ট", "ppt"],
}


def extract_entities(text: str) -> Dict[str, Any]:
    """Extract structured entities: apps, numbers, time references."""
    entities: Dict[str, Any] = {}
    t = text.lower()

    for app_name, keywords in APP_ENTITY_MAP.items():
        if any(k in t for k in keywords):
            entities["app"] = app_name
            break

    numbers = re.findall(r'\b\d+(?:\.\d+)?\b', text)
    if numbers:
        entities["numbers"] = [float(n) for n in numbers]

    bn_digits = re.findall(r'[০-৯]+', text)
    if bn_digits:
        bn_map = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
        entities["bn_numbers"] = [int(d.translate(bn_map)) for d in bn_digits]

    # Time references
    time_patterns = [
        (r'(\d{1,2}):(\d{2})\s*(am|pm)?', "clock_time"),
        (r'(\d{1,2})\s*টা\s*(\d{1,2})?', "bn_time"),
    ]
    for pattern, etype in time_patterns:
        m = re.search(pattern, t, re.IGNORECASE)
        if m:
            entities["time_ref"] = {"type": etype, "raw": m.group()}
            break

    pct = re.findall(r'(\d+)%', text)
    if pct:
        entities["percentage"] = int(pct[0])

    return entities


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER 0 (cont.) — ADVANCED NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

BANGLISH_TYPO_FIXES: Dict[str, str] = {
    "bhaloo": "bhalo", "valoo": "valo", "kemonn": "kemon",
    "achoo": "acho", "achhoo": "achho", "korchho": "korcho",
    "bolchoo": "bolcho", "bollo": "bolo",
    "thakboo": "thakbo", "jaboo": "jabo", "khaboo": "khabo",
    "ghumoo": "ghum", "khusii": "khushi",
    "tenshun": "tension", "tensn": "tension", "voluem": "volume",
    "volum": "volume", "brigthness": "brightness", "brighness": "brightness",
    "calcualtor": "calculator", "calclator": "calculator",
    "chrrome": "chrome", "chorme": "chrome", "youtbe": "youtube",
    "youtub": "youtube", "facebok": "facebook", "faceboo": "facebook",
    "instagrm": "instagram", "whtsapp": "whatsapp", "whatapp": "whatsapp",
    "notepd": "notepad", "notpad": "notepad",
    "abohwa": "abohawa", "abohaa": "abohawa", "weathe": "weather",
    "battry": "battery", "battrey": "battery", "chareg": "charge",
    "plzz": "please", "plz": "please", "pls": "please",
    "bhaia": "bhaiya", "bhaai": "bhai", "dadaa": "dada",
}

WAKE_WORD_PATTERN = re.compile(
    r"^(?:"
    r"hey star|star|স্টার|ও স্টার|এই স্টার|"
    r"বন্ধু|ও বন্ধু|বন্ধু রে|"
    r"bhai|bhaiya|ভাই|ভাইয়া|ও ভাই|"
    r"dada|দাদা|দাদা রে|"
    r"please|plz|plzz|pls|একটু|ektu|zara|জরা|"
    r"shono|suno|শোনো|শুনো|শোন|"
    r"areh|আরে|ওরে|ওই|"
    r"hello|hi|hey|হ্যালো|হাই|"
    r"dear|প্রিয়|"
    r"assistant|অ্যাসিস্ট্যান্ট|"
    r"jarvis|alexa|siri"
    r")\s*[,.!?\-—]?\s*",
    re.IGNORECASE
)

TRAILING_FILLER_PATTERN = re.compile(
    r"\s*[,.!?\-—]?\s*(?:"
    r"bhai|bhaiya|ভাই|ভাইয়া|dada|দাদা|bondhu|বন্ধু|"
    r"please|plz|plzz|pls|প্লিজ|na|না|to|তো|ektu|একটু|"
    r"re|রে|shono|শোনো|bolo|বলো"
    r")+\s*$",
    re.IGNORECASE
)

PUNCTUATION_PATTERN = re.compile(r"[!?,.:;\"\'\(\)\[\]{}—\-_/\\@#$%^&*+=<>~`]")
MULTISPACE_PATTERN = re.compile(r"\s+")


def stem_bengali_suffix(word: str) -> str:
    """Stem common Bengali definitive articles and case suffixes."""
    if word in ("কয়টা", "একটা", "কোনটা", "কোন্টা"):
        return word
    for sfx in ("গুলো", "গুলি", "খানা", "খানি", "টা", "টি"):
        if word.endswith(sfx) and len(word) > len(sfx) + 1:
            return word[:-len(sfx)]
    return word


def normalize_dialogue(text: str) -> str:
    """
    Ultra-normalization pipeline:
      1. Strip wake words & filler prefixes
      2. Strip trailing conversational tag words
      3. Fix common typos
      4. Strip punctuation
      5. Lowercase
      6. Apply concept mapping (Banglish/Bengali/English → canonical)
      7. Bengali suffix stemming (e.g. মনটা -> মন, ক্রোমটা -> ক্রোম)
      8. Collapse whitespace
    """
    if not text:
        return ""

    t = text.strip()
    # Strip wake words and prefix fillers
    t = WAKE_WORD_PATTERN.sub("", t)
    # Strip trailing fillers (e.g., 'to bhai plzz')
    t = TRAILING_FILLER_PATTERN.sub("", t)

    t_lower = t.lower()
    for typo, fix in BANGLISH_TYPO_FIXES.items():
        t_lower = re.sub(r'\b' + re.escape(typo) + r'\b', fix, t_lower)

    t = PUNCTUATION_PATTERN.sub(" ", t_lower)
    t = MULTISPACE_PATTERN.sub(" ", t).strip()

    tokens = t.split()
    mapped = []
    for tok in tokens:
        # Check direct concept map
        m = BANGLISH_CONCEPTS.get(tok, tok)
        # Apply Bengali suffix stemming if in Bengali script
        m = stem_bengali_suffix(m)
        mapped.append(BANGLISH_CONCEPTS.get(m, m))

    return " ".join(mapped)


def extract_content_tokens(text: str) -> Set[str]:
    """Extract substantive keywords excluding stopwords and short tokens."""
    return {w for w in text.split() if w not in STOPWORDS and len(w) > 1}


def generate_ngrams(tokens: List[str], n: int) -> Set[str]:
    """Generate word-level n-grams."""
    if len(tokens) < n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER 5 — RESPONSE VARIATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

RESPONSE_VARIATIONS: Dict[str, List[str]] = {
    "greeting_morning": [
        "সুপ্রভাত! নতুন দিন, নতুন সম্ভাবনা! ☀️ তোমার দিনটা অসাধারণ হোক!",
        "গুড মর্নিং! উঠে পড়েছো? আজকের দিনটা তোমার জন্য স্পেশাল! 🌅",
        "সুপ্রভাত বন্ধু! চা খেয়েছো? নাকি আগে আমাকে হ্যালো বলবে? 😄☕",
    ],
    "greeting_evening": [
        "শুভ সন্ধ্যা! সারাদিন কেমন কাটলো? 🌆",
        "সন্ধ্যা হয়ে গেছে! একটু রেস্ট নাও, তুমি অনেক পরিশ্রম করেছো! 🌇",
        "গুড ইভনিং! চায়ের কাপ হাতে নিয়ে বলো, কী খবর? ☕🌙",
    ],
    "greeting_night": [
        "এত রাতে জেগে আছো? ঘুমানোর সময় হয়ে গেছে! 🌙",
        "শুভ রাত্রি! আজকের দিনটা কেমন ছিল? ঘুমানোর আগে বলো! 😴",
        "রাত অনেক হয়েছে! শরীরের যত্ন নাও, কাল আবার নতুন দিন! 🌃",
    ],
    "greeting_default": [
        "হ্যালো! তোমাকে দেখে ভালো লাগলো! 😊 কী করতে পারি?",
        "হাই বন্ধু! বলো, আজ কী প্ল্যান? 🎯",
        "হেই! আমি আছি তোমার জন্য! কী দরকার? 💫",
    ],
    "sadness": [
        "আমি জানি এখন কষ্ট হচ্ছে। কিন্তু মনে রাখো, এই সময়টাও কেটে যাবে। আমি আছি তোমার পাশে। 💙",
        "মন খারাপ? এসো, কথা বলি। কান্না পেলে কাঁদো, লজ্জা নেই। আমি শুনছি। 🤗",
        "তোমার কষ্টটা আমি বুঝতে পারি। তুমি একা নও, আমি সবসময় আছি। 🌈",
    ],
    "motivation": [
        "শোনো, তুমি যে এতদূর এসেছো, এটাই প্রমাণ তুমি পারো! হার মানা তোমার জন্য না! 🔥",
        "প্রতিটা সফল মানুষ একদিন ভেবেছিল 'আমি পারবো না'। কিন্তু তারা থামেনি। তুমিও থেমো না! 💪",
        "আজকের কষ্ট কালকের শক্তি। তুমি যতটা ভাবছো, তার চেয়ে অনেক বেশি শক্তিশালী! 🌟",
    ],
    "joke_request": [
        "একটা জোক শোনো — প্রোগ্রামার কেন চশমা পরে? কারণ সে C# (সি শার্প) দেখতে পায় না! 😂",
        "বলো তো, WiFi আর ভালোবাসার মধ্যে মিল কী? দুটোই না পেলে জীবন অচল! 😄📶",
        "শিক্ষক: তোমার হোমওয়ার্ক কই? ছাত্র: স্যার, WiFi ছিল না। শিক্ষক: হোমওয়ার্ক তো খাতায়! ছাত্র: হোমওয়ার্ক তো আপলোড করতে হয়! 😆",
    ],
    "fallback": [
        "হুম, বুঝতে পারছি! একটু বিস্তারিত বলো, আমি সাহায্য করতে চাই! 🤔",
        "আচ্ছা, ইন্টারেস্টিং! আরেকটু বলো তো? 😊",
        "ঠিক আছে, আমি শুনছি! কী করতে চাও? 💬",
    ],
}


def get_varied_response(category: str, base_response: str = "") -> str:
    """Get a varied response to avoid robotic repetition."""
    variations = RESPONSE_VARIATIONS.get(category, [])
    if variations:
        return random.choice(variations)
    return base_response


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER 6 — PROACTIVE CONTEXT ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def get_time_context() -> Dict[str, Any]:
    """Get current time-based context for proactive responses."""
    now = datetime.now()
    hour = now.hour

    if 5 <= hour < 12:
        period = "morning"
        greeting_cat = "greeting_morning"
        mood_hint = "fresh"
    elif 12 <= hour < 17:
        period = "afternoon"
        greeting_cat = "greeting_default"
        mood_hint = "active"
    elif 17 <= hour < 21:
        period = "evening"
        greeting_cat = "greeting_evening"
        mood_hint = "relaxed"
    else:
        period = "night"
        greeting_cat = "greeting_night"
        mood_hint = "sleepy"

    return {
        "hour": hour,
        "period": period,
        "greeting_category": greeting_cat,
        "mood_hint": mood_hint,
        "is_late_night": hour >= 1 or hour < 5,
        "is_early_morning": 5 <= hour < 7,
        "weekday": now.strftime("%A"),
        "date_str": now.strftime("%d %B %Y"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER 8 — CONVERSATION MEMORY MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationMemory:
    """
    Sliding-window conversation memory with topic tracking.
    Remembers recent context to handle follow-ups naturally.
    """

    def __init__(self, max_turns: int = 20, topic_window: int = 5):
        self.history: deque = deque(maxlen=max_turns)
        self.topic_window = topic_window
        self.topic_counts: Counter = Counter()
        self.last_intent: str = "general"
        self.last_emotion: str = "neutral"
        self.turn_count: int = 0

    def add_turn(self, user_text: str, assistant_text: str,
                 intent: str, emotion: str, confidence: float):
        """Record a conversation turn."""
        self.history.append(ConversationTurn(
            timestamp=time.time(),
            user_text=user_text,
            assistant_text=assistant_text,
            intent=intent,
            emotion=emotion,
            confidence=confidence,
        ))
        self.topic_counts[intent] += 1
        self.last_intent = intent
        self.last_emotion = emotion
        self.turn_count += 1

    def get_recent_context(self, n: int = 3) -> List[Dict[str, str]]:
        """Get last n turns as context for LLM."""
        recent = list(self.history)[-n:]
        context = []
        for turn in recent:
            context.append({"role": "user", "content": turn.user_text})
            context.append({"role": "assistant", "content": turn.assistant_text})
        return context

    def get_dominant_topic(self) -> str:
        """Get the most discussed topic recently."""
        recent = list(self.history)[-self.topic_window:]
        if not recent:
            return "general"
        counts = Counter(t.intent for t in recent)
        return counts.most_common(1)[0][0]

    def get_emotional_trend(self) -> str:
        """Detect emotional trajectory over recent turns."""
        recent = list(self.history)[-self.topic_window:]
        if len(recent) < 2:
            return self.last_emotion

        emotions = [t.emotion for t in recent]
        sad_count = sum(1 for e in emotions if e in ("sad", "anxious", "fear"))
        happy_count = sum(1 for e in emotions if e in ("happy", "excited", "love"))

        if sad_count >= 3:
            return "deeply_sad"
        elif sad_count >= 2:
            return "getting_sad"
        elif happy_count >= 3:
            return "very_happy"
        return self.last_emotion

    def is_followup(self, query: str, intent: str) -> bool:
        """Detect if current query is a follow-up to previous context."""
        if not self.history:
            return False

        last = self.history[-1]
        time_gap = time.time() - last.timestamp

        if time_gap < 60 and intent == last.intent:
            return True
        if len(query.split()) <= 3 and time_gap < 120:
            return True
        return False

    def clear(self):
        """Reset memory."""
        self.history.clear()
        self.topic_counts.clear()
        self.turn_count = 0


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ENGINE — ULTRA HUMAN-TYPE COMPANION KNOWLEDGE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class CompanionKnowledgeEngine:
    """
    Ultra human-type companion brain with:
      • 8-layer processing pipeline
      • Semantic + fuzzy + n-gram hybrid search
      • BM25-inspired scoring with IDF weighting
      • Intent-aware response routing
      • Emotion-adaptive tone
      • Conversation memory with follow-up detection
      • Proactive time-aware context
      • Response variation to avoid repetition
      • Automatic tool orchestration
      • Graceful LLM fallback with few-shot exemplars
    """

    def __init__(self, jsonl_path: Optional[Path] = None):
        self.samples: List[DialogueSample] = []
        self.idf_cache: Dict[str, float] = {}
        self.intent_index: Dict[str, List[int]] = defaultdict(list)
        self.memory = ConversationMemory(max_turns=30, topic_window=5)
        self.time_context = get_time_context()
        self._load_time = 0.0
        self._response_history: deque = deque(maxlen=50)

        self.load()

    # ─── Data Loading ────────────────────────────────────────────────────────

    def load(self):
        """Load and index all dialogue samples with full enrichment."""
        start = time.time()
        self.samples.clear()
        self.intent_index.clear()
        seen_norms: Set[str] = set()

        for path in DATASET_PATHS:
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            msgs = data.get("messages", [])
                            u, a = "", ""
                            for m in msgs:
                                role = m.get("role")
                                if role == "user":
                                    u = m.get("content", "").strip()
                                elif role == "assistant":
                                    a = m.get("content", "").strip()

                            if not (u and a):
                                continue

                            norm_u = normalize_dialogue(u)
                            if not norm_u or norm_u in seen_norms:
                                continue
                            seen_norms.add(norm_u)

                            token_list = norm_u.split()
                            tokens = set(token_list)
                            content = extract_content_tokens(norm_u)
                            bigrams = generate_ngrams(token_list, 2)
                            trigrams = generate_ngrams(token_list, 3)
                            intent = classify_intent(norm_u)
                            emotion, _ = detect_emotion(norm_u)
                            entities = extract_entities(norm_u)

                            sample = DialogueSample(
                                raw_user=u,
                                norm_user=norm_u,
                                tokens=tokens,
                                content_tokens=content,
                                bigrams=bigrams,
                                trigrams=trigrams,
                                assistant_reply=a,
                                intent=intent,
                                emotion=emotion,
                                entities=entities,
                            )
                            idx = len(self.samples)
                            self.samples.append(sample)
                            self.intent_index[intent].append(idx)

                        except Exception:
                            continue
            except Exception as e:
                print(f"[UltraEngine] Error loading {path.name}: {e}")

        self._build_idf_cache()
        self._load_time = time.time() - start
        print(
            f"[UltraEngine] [OK] Loaded {len(self.samples)} samples "
            f"with {len(self.intent_index)} intents "
            f"in {self._load_time:.3f}s"
        )

    def _build_idf_cache(self):
        """Build inverse document frequency cache for semantic scoring."""
        if not self.samples:
            return

        N = len(self.samples)
        doc_freq: Counter = Counter()

        for sample in self.samples:
            for token in sample.content_tokens:
                doc_freq[token] += 1

        self.idf_cache = {
            token: math.log((N - freq + 0.5) / (freq + 0.5) + 1.0)
            for token, freq in doc_freq.items()
        }

    # ─── Layer 4: Hierarchical Search Pipeline ──────────────────────────────

    def _score_bm25(self, query_content: Set[str],
                    sample: DialogueSample, avg_dl: float = 5.0) -> float:
        """BM25-inspired scoring for semantic relevance."""
        if not query_content or not sample.content_tokens:
            return 0.0

        k1, b = 1.5, 0.75
        dl = len(sample.content_tokens)
        score = 0.0

        for token in query_content & sample.content_tokens:
            tf = 1
            idf = self.idf_cache.get(token, 1.0)
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * dl / avg_dl)
            score += idf * (numerator / denominator)

        return score

    def _score_ngram_overlap(self, query_bigrams: Set[str],
                             query_trigrams: Set[str],
                             sample: DialogueSample) -> float:
        """Score based on n-gram overlap for phrase-level matching."""
        bi_overlap = len(query_bigrams & sample.bigrams)
        tri_overlap = len(query_trigrams & sample.trigrams)
        total_q = len(query_bigrams) + len(query_trigrams)
        if total_q == 0:
            return 0.0
        return (bi_overlap * 0.4 + tri_overlap * 0.6) / total_q

    def _score_fuzzy(self, norm_q: str, sample: DialogueSample) -> float:
        """SequenceMatcher fuzzy ratio."""
        return difflib.SequenceMatcher(None, norm_q, sample.norm_user).ratio()

    def _score_token_overlap(self, query_tokens: Set[str],
                             sample: DialogueSample) -> float:
        """Jaccard token overlap."""
        if not query_tokens or not sample.tokens:
            return 0.0
        intersection = query_tokens & sample.tokens
        union = query_tokens | sample.tokens
        return len(intersection) / len(union) if union else 0.0

    def _search_pipeline(self, query: str,
                         min_confidence: float = 0.65) -> Optional[MatchResult]:
        """
        Full hierarchical search:
          Pass 1: Exact match
          Pass 2: Intent-filtered BM25 + n-gram + fuzzy hybrid
          Pass 3: Full-scan fuzzy fallback
        """
        norm_q = normalize_dialogue(query)
        if not norm_q:
            return None

        token_list = norm_q.split()
        q_tokens = set(token_list)
        q_content = extract_content_tokens(norm_q)
        q_bigrams = generate_ngrams(token_list, 2)
        q_trigrams = generate_ngrams(token_list, 3)
        q_intent = classify_intent(norm_q)

        best_score = 0.0
        best_sample: Optional[DialogueSample] = None

        # Pass 1: Exact match
        for sample in self.samples:
            if norm_q == sample.norm_user:
                return self._build_result(sample, 1.0, "exact_match")

        # Pass 2: Intent-filtered hybrid search
        candidate_indices = set(self.intent_index.get(q_intent, []))
        candidate_indices.update(self.intent_index.get("general", []))

        for idx in candidate_indices:
            sample = self.samples[idx]

            if q_content and sample.content_tokens:
                if not (q_content & sample.content_tokens):
                    continue
            elif q_content != sample.content_tokens:
                continue

            bm25 = self._score_bm25(q_content, sample)
            ngram = self._score_ngram_overlap(q_bigrams, q_trigrams, sample)
            fuzzy = self._score_fuzzy(norm_q, sample)
            token_ov = self._score_token_overlap(q_tokens, sample)

            bm25_norm = min(bm25 / 5.0, 1.0)

            combined = (
                bm25_norm * 0.30 +
                ngram * 0.25 +
                fuzzy * 0.25 +
                token_ov * 0.20
            )

            if sample.intent == q_intent and q_intent != "general":
                combined += 0.08

            if combined > best_score:
                best_score = combined
                best_sample = sample

        # Pass 3: Full-scan fuzzy fallback
        if best_score < min_confidence and len(self.samples) <= 10000:
            for sample in self.samples:
                if q_content and sample.content_tokens:
                    if not (q_content & sample.content_tokens):
                        continue

                fuzzy = self._score_fuzzy(norm_q, sample)
                token_ov = self._score_token_overlap(q_tokens, sample)
                combined = fuzzy * 0.6 + token_ov * 0.4

                if combined > best_score:
                    best_score = combined
                    best_sample = sample

        if best_sample and best_score >= min_confidence:
            return self._build_result(best_sample, best_score, "hybrid_search")

        return None

    def _build_result(self, sample: DialogueSample, confidence: float,
                      source: str) -> MatchResult:
        """Build a MatchResult with action detection and variation."""
        actions = self._detect_and_run_actions(sample.norm_user)

        response = sample.assistant_reply
        if response in self._response_history:
            varied = self._get_intent_variation(sample.intent, sample.emotion)
            if varied:
                response = varied

        self._response_history.append(response)

        return MatchResult(
            response=response,
            confidence=confidence,
            intent=sample.intent,
            emotion=sample.emotion,
            source=f"ultra_engine ({source}, score={confidence:.3f})",
            actions=actions,
            matched_sample=sample.raw_user,
        )

    def _get_intent_variation(self, intent: str, emotion: str) -> Optional[str]:
        """Get a dynamic response variation matching intent and emotion."""
        key = f"{intent}_{emotion}"
        if key in RESPONSE_VARIATIONS:
            return random.choice(RESPONSE_VARIATIONS[key])
        if intent in RESPONSE_VARIATIONS:
            return random.choice(RESPONSE_VARIATIONS[intent])
        return None

    # ─── Layer 7: Action Execution Orchestrator ─────────────────────────────

    def _detect_and_run_actions(self, norm_q: str) -> List[Dict[str, Any]]:
        """
        Execute matching tool intents safely.
        Returns log of executed tools with status.
        """
        executed = []
        text = norm_q.lower()
        entities = extract_entities(text)
        
        # Check both numbers and Bengali numerals
        delta_val = None
        if "numbers" in entities and entities["numbers"]:
            delta_val = int(entities["numbers"][0])
        elif "bn_numbers" in entities and entities["bn_numbers"]:
            delta_val = int(entities["bn_numbers"][0])

        # 1. Application Launch / Close
        app_name = entities.get("app")
        if app_name:
            is_open = any(act in text for act in [
                "খোল", "kholo", "launch", "open", "চালু", "চালাও", "start"
            ])
            is_close = any(act in text for act in [
                "বন্ধ", "bondho", "close", "kill", "stop", "exit"
            ])

            if is_open:
                try:
                    res = execute_tool("launch_application", app_name=app_name)
                    executed.append({
                        "tool": "launch_application",
                        "args": {"app_name": app_name},
                        "result": res
                    })
                except Exception as e:
                    executed.append({"tool": "launch_application", "error": str(e)})
            elif is_close:
                try:
                    res = execute_tool("close_application", app_name=app_name)
                    executed.append({
                        "tool": "close_application",
                        "args": {"app_name": app_name},
                        "result": res
                    })
                except Exception as e:
                    executed.append({"tool": "close_application", "error": str(e)})

        # 2. Web Search (Google / YouTube)
        is_search = any(w in text for w in ["সার্চ", "search", "খোঁজো", "খুঁজে", "find"])
        if is_search:
            target = "youtube" if any(yt in text for yt in [
                "youtube", "ইউটিউব", "গান", "ভিডিও", "video"
            ]) else "google"
            clean_q = re.sub(
                r"(?:ইউটিউব(?:\s*e)?|ইউটিউবে?|গুগল(?:\s*e)?|গুগলে?|গান|সার্চ(?:\s*করো)?|খোঁজো|খুঁজে দাও|search(?:\s*kor)?|find|google|youtube)\s*",
                "", text, flags=re.IGNORECASE
            ).strip()
            clean_q = re.sub(r"\s*(?:search|সার্চ|খোঁজো|koro|করো)$", "", clean_q, flags=re.IGNORECASE).strip()
            if clean_q:
                try:
                    res = execute_tool("search_web", query=clean_q, target=target)
                    executed.append({
                        "tool": "search_web",
                        "args": {"query": clean_q, "target": target},
                        "result": res
                    })
                except Exception as e:
                    executed.append({"tool": "search_web", "error": str(e)})

        # 3. Volume Control
        if any(v in text for v in [
            "volume", "sound", "awaj", "সাউন্ড", "ভলিউম", "আওয়াজ", "শব্দ"
        ]):
            delta = delta_val if delta_val is not None else 15
            if any(inc in text for inc in [
                "barie", "barao", "বাড়াও", "বাড়িয়ে", "increase", "up"
            ]):
                try:
                    res = execute_tool("adjust_volume", delta=delta)
                    executed.append({
                        "tool": "adjust_volume",
                        "args": {"delta": delta},
                        "result": res
                    })
                except Exception as e:
                    executed.append({"tool": "adjust_volume", "error": str(e)})
            elif any(dec in text for dec in [
                "komie", "komao", "কমাও", "কমিয়ে", "reduce", "down"
            ]):
                try:
                    res = execute_tool("adjust_volume", delta=-delta)
                    executed.append({
                        "tool": "adjust_volume",
                        "args": {"delta": -delta},
                        "result": res
                    })
                except Exception as e:
                    executed.append({"tool": "adjust_volume", "error": str(e)})
            elif any(m in text for m in ["mute", "মিউট", "চুপ", "silent"]):
                try:
                    res = execute_tool("set_mute", mute=True)
                    executed.append({
                        "tool": "set_mute",
                        "args": {"mute": True},
                        "result": res
                    })
                except Exception as e:
                    executed.append({"tool": "set_mute", "error": str(e)})

        # 4. Brightness Control
        if any(b in text for b in ["brightness", "alo", "আলো", "ব্রাইটনেস"]):
            delta = delta_val if delta_val is not None else 20
            if any(inc in text for inc in [
                "barie", "barao", "বাড়াও", "বাড়িয়ে", "full", "increase"
            ]):
                try:
                    res = execute_tool("adjust_brightness", delta=delta)
                    executed.append({
                        "tool": "adjust_brightness",
                        "args": {"delta": delta},
                        "result": res
                    })
                except Exception as e:
                    executed.append({"tool": "adjust_brightness", "error": str(e)})
            elif any(dec in text for dec in [
                "komie", "komao", "কমাও", "কমিয়ে", "dim", "reduce"
            ]):
                try:
                    res = execute_tool("adjust_brightness", delta=-delta)
                    executed.append({
                        "tool": "adjust_brightness",
                        "args": {"delta": -delta},
                        "result": res
                    })
                except Exception as e:
                    executed.append({"tool": "adjust_brightness", "error": str(e)})

        # 5. Screenshot
        if any(w in text for w in ["screenshot", "স্ক্রিনশট", "স্ক্রিন শট"]):
            try:
                res = execute_tool("take_screenshot")
                executed.append({"tool": "take_screenshot", "args": {}, "result": res})
            except Exception as e:
                executed.append({"tool": "take_screenshot", "error": str(e)})

        # 6. Lock PC
        if any(w in text for w in ["lock pc", "পিসি লক", "স্ক্রিন লক", "লক করো"]):
            try:
                res = execute_tool("lock_workstation")
                executed.append({"tool": "lock_workstation", "args": {}, "result": res})
            except Exception as e:
                executed.append({"tool": "lock_workstation", "error": str(e)})

        # 7. System Status
        if any(s in text for s in [
            "battery", "charge", "ব্যাটারি", "চার্জ",
            "koyta baje", "কয়টা বাজে", "সময়", "status", "সিস্টেম"
        ]):
            try:
                res = execute_tool("get_system_status")
                executed.append({
                    "tool": "get_system_status",
                    "args": {},
                    "result": res
                })
            except Exception as e:
                executed.append({"tool": "get_system_status", "error": str(e)})

        return executed

    # ─── Public Query Interface ──────────────────────────────────────────────

    def find_match(self, query: str,
                   min_confidence: float = 0.65) -> Optional[Dict[str, Any]]:
        """
        Find high-confidence matching response using multi-stage pipeline
        with transliteration fallback and conversation memory update.
        """
        if not self.samples or not query:
            return None

        # Direct search
        match = self._search_pipeline(query, min_confidence)

        # Transliteration fallback (Bengali → Roman)
        if not match and any('\u0980' <= c <= '\u09ff' for c in query):
            try:
                from star.brain.translit import bn_to_roman
                roman_q = bn_to_roman(query)
                if roman_q and roman_q != query:
                    match = self._search_pipeline(roman_q, min_confidence)
            except ImportError:
                pass
            except Exception:
                pass

        # If no dataset match, but an action was detected and successfully executed
        if not match:
            norm_q = normalize_dialogue(query)
            if norm_q:
                actions = self._detect_and_run_actions(norm_q)
                if actions:
                    intent = classify_intent(norm_q)
                    emotion, _ = detect_emotion(norm_q)
                    tool_name = actions[0].get("tool", "")
                    args = actions[0].get("args", {})
                    if tool_name == "search_web":
                        q_arg = args.get("query", "")
                        target = args.get("target", "web")
                        if target == "youtube":
                            resp = f"ইউটিউবে তোমার জন্য '{q_arg}' সার্চ করে চালিয়ে দিয়েছি বন্ধু!"
                        else:
                            resp = f"গুগলে তোমার জন্য '{q_arg}' সার্চ করে দিয়েছি বন্ধু!"
                    elif tool_name == "launch_application":
                        app = args.get("app_name", "অ্যাপ")
                        resp = f"তোমার জন্য {app.capitalize()} খুলে দিয়েছি বন্ধু!"
                    elif tool_name == "close_application":
                        app = args.get("app_name", "অ্যাপ")
                        resp = f"{app.capitalize()} বন্ধ করে দিয়েছি বন্ধু!"
                    elif tool_name == "adjust_volume":
                        resp = "ভলিউম ঠিকঠাক অ্যাডজাস্ট করে দিলাম বন্ধু!"
                    elif tool_name == "adjust_brightness":
                        resp = "স্ক্রিনের ব্রাইটনেস তোমার পছন্দমতো সেট করে দিয়েছি বন্ধু!"
                    elif tool_name == "take_screenshot":
                        resp = "পুরো স্ক্রিনের স্ক্রিনশট নিয়ে পিকচার্স ফোল্ডারে সেভ করে দিয়েছি বন্ধু!"
                    elif tool_name == "lock_workstation":
                        resp = "কম্পিউটার স্ক্রিন লক করে দিয়েছি বন্ধু!"
                    elif tool_name == "get_system_status":
                        res_val = actions[0].get("result", {}).get("result", {})
                        resp = f"এখন সময় {res_val.get('time')}, ব্যাটারি চার্জ {res_val.get('battery')}।"
                    else:
                        resp = "কাজটি সম্পন্ন করে দিয়েছি বন্ধু!"

                    match = MatchResult(
                        response=resp,
                        confidence=1.0,
                        intent=intent,
                        emotion=emotion,
                        source="ultra_engine (action_orchestrator, score=1.000)",
                        actions=actions,
                        matched_sample=query
                    )

        # Special conversational intents when dataset doesn't have exact phrase
        if not match:
            norm_q = normalize_dialogue(query)
            if norm_q:
                intent = classify_intent(norm_q)
                if intent == "joke_request":
                    joke = random.choice(RESPONSE_VARIATIONS.get("joke_request", ["একটা মজার কথা বলি বন্ধু!"]))
                    match = MatchResult(
                        response=joke,
                        confidence=0.95,
                        intent="joke_request",
                        emotion="happy",
                        source="ultra_engine (varied_response, score=0.950)",
                        actions=[],
                        matched_sample=query
                    )
                elif intent == "sadness":
                    comfort = random.choice(RESPONSE_VARIATIONS.get("sadness", ["মন খারাপ করো না বন্ধু, আমি আছি তোমার পাশে।"]))
                    match = MatchResult(
                        response=comfort,
                        confidence=0.92,
                        intent="sadness",
                        emotion="sad",
                        source="ultra_engine (varied_response, score=0.920)",
                        actions=[],
                        matched_sample=query
                    )
                elif intent == "motivation":
                    moti = random.choice(RESPONSE_VARIATIONS.get("motivation", ["তুমি পারবে বন্ধু, হার মেনো না!"]))
                    match = MatchResult(
                        response=moti,
                        confidence=0.92,
                        intent="motivation",
                        emotion="neutral",
                        source="ultra_engine (varied_response, score=0.920)",
                        actions=[],
                        matched_sample=query
                    )

        if match:
            self.memory.add_turn(
                user_text=query,
                assistant_text=match.response,
                intent=match.intent,
                emotion=match.emotion,
                confidence=match.confidence,
            )
            return {
                "response": match.response,
                "actions": match.actions,
                "intent": match.intent,
                "emotion": match.emotion,
                "confidence": match.confidence,
                "source": match.source,
                "matched_sample": match.matched_sample,
            }

        return None

    def get_few_shot_exemplars(self, query: str,
                               top_k: int = 3) -> List[Dict[str, str]]:
        """Retrieve top-k most relevant conversation pairs for LLM context."""
        if not self.samples:
            return []

        norm_q = normalize_dialogue(query)
        q_tokens = set(norm_q.split())
        q_content = extract_content_tokens(norm_q)

        scored = []
        for sample in self.samples:
            if q_content and sample.content_tokens:
                if not (q_content & sample.content_tokens):
                    continue

            ratio = self._score_fuzzy(norm_q, sample)
            overlap = self._score_token_overlap(q_tokens, sample)
            score = (ratio * 0.5) + (overlap * 0.5)

            if score >= 0.50:
                scored.append((score, sample))

        scored.sort(key=lambda x: x[0], reverse=True)

        exemplars = []
        for _, sample in scored[:top_k]:
            exemplars.append({"role": "user", "content": sample.raw_user})
            exemplars.append({"role": "assistant", "content": sample.assistant_reply})
        return exemplars

    # ─── Proactive Helpers ───────────────────────────────────────────────────

    def get_proactive_greeting(self) -> str:
        """Generate time-appropriate greeting."""
        ctx = get_time_context()
        return get_varied_response(ctx["greeting_category"])

    def get_memory_summary(self) -> Dict[str, Any]:
        """Return summary of current conversation state."""
        return {
            "turn_count": self.memory.turn_count,
            "last_intent": self.memory.last_intent,
            "last_emotion": self.memory.last_emotion,
            "dominant_topic": self.memory.get_dominant_topic(),
            "emotional_trend": self.memory.get_emotional_trend(),
            "history_size": len(self.memory.history),
        }

    def reset_conversation(self):
        """Clear conversation memory."""
        self.memory.clear()
        self._response_history.clear()


# ═══════════════════════════════════════════════════════════════════════════════
#  SINGLETON ACCESSOR
# ═══════════════════════════════════════════════════════════════════════════════

_engine_instance: Optional[CompanionKnowledgeEngine] = None


def get_companion_engine() -> CompanionKnowledgeEngine:
    """Get or create the singleton companion engine instance."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = CompanionKnowledgeEngine()
    return _engine_instance


# ═══════════════════════════════════════════════════════════════════════════════
#  DIAGNOSTIC / TEST HARNESS
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print("═" * 70)
    print("  ★ STAR ULTRA COMPANION ENGINE — DIAGNOSTIC MODE ★")
    print("═" * 70)

    engine = get_companion_engine()

    test_inputs = [
        "Hey star ektu brightness komie dao",
        "Amar mon kharap bondhu, bhalo lagche na",
        "Chrome kholo to bhai plzz",
        "koyta baje ekhon bolo na?",
        "একটা জোক বলো",
        "youtube e Rabindra Sangeet search koro",
        "battery koto ache?",
        "ভলিউম ২০ বাড়াও",
    ]

    for i, test in enumerate(test_inputs, 1):
        print(f"\n─── Test {i} ─────────────────────────────────")
        print(f"👤 User      : {test}")
        res = engine.find_match(test)
        if res:
            print(f"🎯 Intent    : {res['intent'].upper()}")
            print(f"💭 Emotion   : {res['emotion'].upper()}")
            print(f"📊 Confidence: {res['confidence']:.2%}")
            print(f"🤖 Response  : {res['response']}")
            if res['actions']:
                print(f"⚙️  Actions   : {res['actions']}")
            print(f"🔍 Source    : {res['source']}")
        else:
            print("❌ No high-confidence match — fallback to LLM required")

    print("\n" + "═" * 70)
    print(f"📝 Memory Summary: {engine.get_memory_summary()}")
    print(f"🌅 Proactive Greeting: {engine.get_proactive_greeting()}")
    print("═" * 70)
