"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — DETERMINISTIC COMMAND ROUTER v1.0
═══════════════════════════════════════════════════════════════════════════════

  ONE single source of truth for every PC-control command.

  Why this exists (root causes it fixes):
    1. Previously the fuzzy dataset matcher could hijack commands and execute
       actions stored in *training samples* (wrong deltas / wrong actions —
       "ulto palto kaj").
    2. Keyword gaps ("khulo" vs "kholo", "gaan" vs "gon", "unmute" missing…)
       meant many valid commands fell through and got a generic "I heard you"
       reply with no action.
    3. Two competing intent engines (knowledge orchestrator + offline intent)
       disagreed with each other.

  Design:
    • Strict, word-boundary-anchored imperative patterns → questions like
      "volume komanor jonno ki korbo" can NEVER trigger an action.
    • Always answers with REAL values returned by the tool (volume %, time…).
    • Handles Bengali script, Banglish transliterations and English.
    • Pure & deterministic — no network, no LLM, <1ms.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..tools.registry import execute_tool

# ─────────────────────────────────────────────────────────────────────────────
#  Normalization helpers
# ─────────────────────────────────────────────────────────────────────────────

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

_WAKE_PATTERNS = [
    r"^\s*(?:hey |ok |oi |aye )?(?:star|স্টার|staj)\b[!,.]*\s*",
    r"^\s*(?:শোনো|শোন|shono|shun|suno|bollo|বলো|বল|bolo|hey|হেই)\s+",
]

# Filler words/phrases that pollute search queries (songs, google…)
_FILLERS = [
    "please", "plz", "bhai", "dada", "bondhu", "bandhu", "বন্ধু", "ভাই", "দাদা",
    "amar jonno", "amar jonne", "amar jonyo", "amake diye", "amake",
    "আমার জন্য", "আমাকে", "একটা", "একটি", "ekta", "ekti", "kono", "কোনো",
    "ektu", "একটু", "zara", "জরা", "জারা",
    "valo", "bhalo", "ভালো", "ভাল", "sundor", "shundor", "সুন্দর", "darun", "দারুণ",
    "kichu", "kichu", "কিছু", "na", "ar", "and", "the", "a", "an", "some",
    "lagbe", "lagchhe", "lagche", "chai", "chao", "dorkar",
]


def _normalize(text: str) -> str:
    """Lowercase, convert Bengali digits, strip punctuation and wake words.

    NOTE: Bengali combining marks (matras, U+0981–U+09FF) are NOT ``\\w`` in
    Python's re, so the whole Bengali block is kept explicitly — otherwise
    "ভলিউম" would be stripped to "ভল উম".
    """
    t = (text or "").strip().lower().translate(_BN_DIGITS)
    t = re.sub(r"[^\w\s%+\-*/x×÷.\u0980-\u09FF]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    for pat in _WAKE_PATTERNS:
        t = re.sub(pat, "", t).strip()
    return t.strip(" ,.-")


def _has_word(text: str, words) -> bool:
    """Whole-word (or whole multi-word-phrase) match — the anti-false-positive guard."""
    for w in words:
        if " " in w:
            if w in text:
                return True
        elif re.search(rf"(?:^| ){re.escape(w)}(?: |$)", text):
            return True
    return False


def _strip_fillers(query: str) -> str:
    q = query
    for f in _FILLERS:
        q = re.sub(rf"(?:^|\s){re.escape(f)}(?:\s|$)", " ", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip(" ,.-")


def _first_number(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*%?", text)
    return int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
#  Vocabularies (Bengali script + Banglish + English)
# ─────────────────────────────────────────────────────────────────────────────

VOL_WORDS = ["volume", "sound", "awaj", "awaz", "awajj", "shobdo", "sobdo",
             "আওয়াজ", "আওযাজ", "ভলিউম", "সাউন্ড", "শব্দ", "শব্দটা", "ভলিউমটা"]
UP_WORDS = ["up", "barao", "barie", "bariye", "bari", "barie dao", "barie de",
            "banao", "increase", "raise", "high", "higher", "loud", "louder",
            "full", "max", "জোরে", "জোরালো", "উঁচু", "বাড়াও", "বাড়িয়ে", "বাড়া", "বাড়াও", "বাড়িয়ে"]
DOWN_WORDS = ["down", "komao", "komie", "komiye", "koma", "komo", "kamie",
              "kamu", "decrease", "reduce", "lower", "low", "soft", "kome",
              "কমিয়ে", "কমাও", "কমা", "নিচু", "কমি"]
MUTE_WORDS = ["mute", "chup", "chupkoro", "silent", "silence", "মিউট", "চুপ",
              "শব্দ বন্ধ", "আওয়াজ বন্ধ", "bandho koro awaj", "awaj bondho"]
UNMUTE_WORDS = ["unmute", "chalu koro awaj", "awaj chalu", "un mute", "আনমিউট",
                "মিউট খোলো", "khule dao awaj", "awaj on"]

BRIGHT_WORDS = ["brightness", "bright", "braitnes", "aloo", "alo", "light",
                "ujjol", "উজ্জ্বল", "আলো", "ব্রাইটনেস", "স্ক্রিনের আলো",
                "স্ক্রিন আলো", "display er alo", "display"]
# NOTE: bare "screen" deliberately NOT a brightness trigger (belongs to vision).

OPEN_WORDS = ["open", "khulun", "kholo", "khulo", "khol", "khul", "khule dao",
              "khule de", "khule dao", "khuley", "start", "launch", "on koro",
              "on kore dao", "chalao", "chalu", "chalu koro", "run",
              "ওপেন", "খোলো", "খুলো", "খোল", "খুল", "খুলে দাও", "খুলে দও",
              "চালু করো", "চালু"]
CLOSE_WORDS = ["close", "bondho", "bandho", "band", "off koro", "off kore dao",
               "kill", "exit", "quit", "terminate", "shutdown the app",
               "বন্ধ", "বন্ধ করো", "বন্ধ করে দাও", "বন্ধ করে দও", "কেটে দাও",
               "বন্ধ কর"]

APP_MAP: Dict[str, List[str]] = {
    "chrome": ["chrome", "krom", "গুগল ক্রোম", "ক্রোম", "google chrome", "browser", "ব্রাউজার"],
    "notepad": ["notepad", "নোটপ্যাড", "note pad"],
    "calculator": ["calculator", "calc", "ক্যালকুলেটর", "হিসাব করার"],
    "vscode": ["vscode", "vs code", "code editor", "ভিএস কোড", "ভিস কোড"],
    "explorer": ["explorer", "files", "file manager", "my computer", "ফাইল", "মাই কম্পিউটার"],
    "whatsapp": ["whatsapp", "হোয়াটসঅ্যাপ", "ওয়াটসঅ্যাপ", "হোয়াটসঅ্যাপ্প"],
    "facebook": ["facebook", "fb", "ফেসবুক"],
    "github": ["github", "গিটহাব"],
    "gmail": ["gmail", "মেইল", "ইমেইল", "জিমেইল"],
    "chatgpt": ["chatgpt", "চ্যাটজিপিটি", "চ্যাট জিপিটি"],
    "spotify": ["spotify", "স্পটিফাই"],
    "netflix": ["netflix", "নেটফ্লিক্স"],
    "cmd": ["cmd", "terminal", "command prompt", "powershell", "টার্মিনাল"],
    "paint": ["paint", "পেইন্ট", "mspaint"],
    "settings": ["settings", "setting", "সেটিংস", "সেটিং"],
    "youtube": ["youtube", "ইউটিউব", "ইউটিউবে", "ইউটুব", "yt"],
    "google": ["google", "গুগল"],
}
CLOSE_APP_MAP: Dict[str, List[str]] = {
    "chrome": ["chrome", "krom", "ক্রোম", "browser", "ব্রাউজার"],
    "notepad": ["notepad", "নোটপ্যাড"],
    "calculator": ["calculator", "calc", "ক্যালকুলেটর"],
    "vscode": ["vscode", "vs code", "ভিএস কোড"],
    "whatsapp": ["whatsapp", "হোয়াটসঅ্যাপ", "ওয়াটসঅ্যাপ"],
    "spotify": ["spotify", "স্পটিফাই"],
    "taskmgr": ["taskmgr", "task manager", "টাস্ক ম্যানেজার"],
}

SONG_WORDS = ["gaan", "gan", "gon", "gana", "gaana", "song", "songs", "music", "sangeet",
              "গান", "গানটা", "গণ", "সঙ্গীত", "মিউজিক", "গান্তা"]
PLAY_WORDS = ["play", "chalao", "chalaw", "bajao", "bajye", "shonao", "sonao",
              "shonaw", "sunao", "lagao", "on koro", "chalu koro", "chalu",
              "shunbo", "sunbo", "shunte chai", "sunte chai", "shuno", "suno",
              "চালাও", "বাজাও", "শোনাও", "শোনাউ", "বাজায় দাও", "চালিয়ে দাও",
              "চালু করো", "চালু", "শুনবো", "শুনতে চাই", "শোনো"]

# ─────────────────────────────────────────────────────────────────────────────
#  YouTube playback control vocabularies
# ─────────────────────────────────────────────────────────────────────────────

YT_PAUSE_WORDS = [
    "pause", "pose", "poz", "pauz", "pause koro", "pose koro", "pose kore dao", "pause kore dao",
    "stop", "stop koro", "thamo", "thamao", "thamo na", "rokho", "rok", "ruk", "ruko",
    "video pause", "video pose", "video ta pause", "video ta pose", "video ta pose koro",
    "video ta pause koro", "video pose koro", "video pause koro", "video stop", "video stop koro",
    "gaan pause", "gan pause", "gaan pose", "gan pose",
    "থামো", "থামাও", "পজ", "পজ করো", "পোজ", "পোজ করো", "পাউস", "রোকো", "রোখো", "থাম",
    "স্টপ", "স্টপ করো", "ভিডিও পজ", "ভিডিও পোজ", "ভিডিওটা পজ করো", "ভিডিওটা পোজ করো",
    "ভিডিও পজ করো", "ভিডিও পোজ করো", "ভিডিও থামাও", "ভিডিওটা থামাও", "গান থামাও", "গানটা থামাও",
    "বন্ধ করো ভিডিও", "ভিডিও স্টপ", "ভিডিও স্টপ করো"
]
YT_RESUME_WORDS = [
    "resume", "play koro", "abar chalao", "chalu koro", "play", "ple", "ple koro",
    "আবার চালাও", "প্লে করো", "প্লে", "রিজিউম", "আবার চালু করো",
    "ভিডিও চালাও", "chaliye dao", "চালিয়ে দাও", "chalu", "চালু করো"
]
YT_NEXT_WORDS = [
    "next", "next koro", "nekst", "nekest", "porer ta", "porer video", "skip", "skip koro",
    "age jao", "next video", "porer ta chalao", "porer gan", "porer gaan",
    "পরেরটা", "নেক্সট", "নেক্সট করো", "পরের ভিডিও", "স্কিপ",
    "স্কিপ করো", "পরেরটা চালাও", "আগে যাও", "পরের গান"
]
YT_PREV_WORDS = [
    "previous", "prev", "ager ta", "ager video", "ager ta chalao", "back", "back koro",
    "pichoner ta", "pechhone jao", "ager gan", "ager gaan",
    "আগেরটা", "আগের ভিডিও", "প্রিভিয়াস", "ব্যাক", "পেছনেরটা",
    "আগেরটা চালাও", "পেছনে যাও", "আগের গান"
]
YT_SPEED_WORDS = [
    "speed", "2x", "1.5x", "1.25x", "0.5x", "0.75x", "double speed",
    "speed barao", "speed komao", "speed koro", "doruto", "dhruto",
    "normal speed", "speed normal", "1x", "two x", "fast koro", "slow koro",
    "স্পিড", "দ্রুত", "স্পিড বাড়াও", "স্পিড কমাও", "ডবল স্পিড",
    "নর্মাল স্পিড", "স্পিড নর্মাল", "ফাস্ট করো", "স্লো করো"
]
YT_HD_WORDS = [
    "hd", "1080p", "720p", "480p", "1440p", "4k", "2k", "eichdi", "eich di",
    "quality", "quality barao", "quality koro", "high quality",
    "full hd", "hd koro", "quality high koro", "clear koro",
    "এইচডি", "এইচডি করো", "কোয়ালিটি", "কোয়ালিটি বাড়াও",
    "হাই কোয়ালিটি", "ক্লিয়ার করো", "ফুল এইচডি", "পরিষ্কার করো"
]
YT_FULLSCREEN_WORDS = [
    "fullscreen", "full screen", "fulskrin", "ful skrin", "boro koro", "boro kore dao",
    "boro kore de", "puro screen", "pura screen", "boro screen", "max screen",
    "ফুলস্ক্রিন", "বড় করো", "বড় করে দাও", "পুরো স্ক্রিন", "পূর্ণ পর্দা", "বড় পর্দা"
]
YT_CLOSE_TAB_WORDS = [
    "youtube bondho", "youtube off", "youtube close", "youtube band",
    "yt close", "yt bondho", "yt off", "video bondho", "video close", "video off",
    "ইউটিউব বন্ধ", "ইউটিউব অফ", "ইউটিউব ক্লোজ",
    "ভিডিও বন্ধ", "ভিডিও ক্লোজ", "ভিডিও অফ"
]
YT_FORWARD_WORDS = [
    "forward", "forward koro", "samne jao", "samne", "aage jao",
    "skip koro age", "age skip", "10 second samne", "ten second forward",
    "ফরওয়ার্ড", "সামনে যাও", "সামনে", "এগিয়ে যাও", "১০ সেকেন্ড সামনে"
]
YT_REWIND_WORDS = [
    "rewind", "rewind koro", "pechone jao", "pichone jao",
    "pichone", "pechone", "10 second pichone", "piche jao",
    "রিওয়াইন্ড", "পেছনে যাও", "পেছনে", "পিছনে যাও", "১০ সেকেন্ড পেছনে"
]
YT_MUTE_VID_WORDS = [
    "video mute", "youtube mute", "yt mute", "player mute", "mute video",
    "ভিডিও মিউট", "ইউটিউব মিউট"
]
YT_CAPTION_WORDS = [
    "caption", "captions", "subtitle", "subtitles", "sub",
    "ক্যাপশন", "সাবটাইটেল", "সাব"
]
YT_TRENDING_WORDS = [
    "trending", "trend", "ki cholche", "ki trending",
    "viral", "popular", "jeta trending", "trending video",
    "ট্রেন্ডিং", "ট্রেন্ড", "কী চলছে", "কি চলছে",
    "ভাইরাল", "জনপ্রিয়"
]
YT_ANALYSE_WORDS = [
    "analyse", "analyze", "analysis", "youtube analyse",
    "youtube analysis", "yt analyse",
    "অ্যানালাইসিস", "বিশ্লেষণ", "ইউটিউব অ্যানালাইসিস"
]
YT_THEATER_WORDS = [
    "theater mode", "theatre mode", "cinema mode", "theatre", "theater",
    "থিয়েটার মোড", "সিনেমা মোড", "থিয়েটার"
]
YT_MINIPLAYER_WORDS = [
    "mini player", "miniplayer", "choto player", "choto video",
    "মিনিপ্লেয়ার", "মিনি প্লেয়ার", "ছোট প্লেয়ার"
]
YT_RESTART_WORDS = [
    "shuru theke", "beginning", "first theke", "restart", "start theke", "shurute jao",
    "প্রথম থেকে", "শুরু থেকে", "রিস্টার্ট", "শুরুতে যাও", "আবার শুরু করো", "shuru koro"
]
YT_PLAYER_VOL_WORDS = [
    "youtube sound", "youtube er sound", "youtube volume", "youtube er volume",
    "yt sound", "yt volume", "ভিডিওর সাউন্ড", "ভিডিওর ভলিউম", "প্লেয়ারের সাউন্ড",
    "ভিডিওর আওয়াজ", "প্লেয়ারের আওয়াজ"
]
YT_POSITION_WORDS = [
    "majhe", "majhkhan", "majhe jao", "majhe chalao", "middle", "half", "50%",
    "মাঝে", "মাঝখানে", "মাঝামাঝি", "মাঝখানে যাও"
]
YT_HISTORY_WORDS = [
    "history", "watch history", "ager video gulo",
    "ager jeta dekhchilam", "last video", "recently watched",
    "হিস্টোরি", "ওয়াচ হিস্টোরি", "আগের ভিডিওগুলো",
    "আগের যেটা দেখছিলাম", "লাস্ট ভিডিও", "আগের দেখা ভিডিও"
]
YT_LOOP_WORDS = [
    "loop", "repeat", "barbar", "bar bar", "loop koro", "repeat koro", "loop this", "repeat video",
    "লুপ", "রিপিট", "বারবার চালাও", "বার বার চালাও", "লুপ করো", "পুনরায় চালাও", "রিপিট করো"
]
YT_NEXT_CHAPTER_WORDS = [
    "next chapter", "porer chapter", "porer porbo", "porer part", "chapter skip",
    "পরের চ্যাপ্টার", "পরের পর্ব", "পরের পার্ট", "নেক্সট চ্যাপ্টার", "চ্যাপ্টার স্কিপ"
]
YT_PREV_CHAPTER_WORDS = [
    "previous chapter", "prev chapter", "ager chapter", "ager porbo", "ager part",
    "আগের চ্যাপ্টার", "আগের পর্ব", "আগের পার্ট", "প্রিভিয়াস চ্যাপ্টার"
]
YT_COPY_URL_WORDS = [
    "copy url", "copy link", "url copy", "link copy", "video link", "share link",
    "ভিডিও লিংক", "লিংক কপি", "ইউআরএল কপি", "শেয়ার লিংক", "ভিডিও লিংক কপি"
]
YT_SCROLL_DOWN_WORDS = [
    "scroll down", "niche scroll", "niche jao", "comment dekhao", "comments dekhao",
    "comment dekho", "comments dekho",
    "নিচে যাও", "নিচে স্ক্রোল", "কমেন্ট দেখাও", "কমেন্টস দেখাও", "কমেন্ট পড়ো"
]
YT_SCROLL_UP_WORDS = [
    "scroll up", "opore scroll", "opore jao", "উপরে যাও", "উপরে ওঠো", "ভিডিওতে ফিরে যাও"
]
YT_SCROLL_TOP_WORDS = [
    "top e jao", "ekdom opore", "একদম উপরে", "শুরুতে যাও পেজের"
]
YT_END_WORDS = [
    "video sesh", "end of video", "seshe jao", "ekdom seshe", "ভিডিও শেষ করো",
    "শেষে যাও", "একদম শেষে", "ভিডিও শেষ"
]
YT_SEARCH_BAR_WORDS = [
    "search bar", "search box", "সার্চ বার", "সার্চ বক্স", "সার্চ বার খোলো"
]


SCREENSHOT_WORDS = ["screenshot", "screen shot", "screshot", "skrinsot",
                    "স্ক্রিনশট", "স্ক্রিন শট", "ছবি তোলো", "chobi tolo", "capture"]

VISION_PATTERNS = [
    r"\bscreen\s*(?:dekho|dek|poro|porho|padho|parho)\b",
    r"\bdekho\s*screen\b", r"\bscreen\s*e\s*kis?\s*(ache|dekhtecho|dekho)\b",
    r"\bporde?\s*(?:dekho|poro|e ki)\b", r"\bskreen\s*dekho\b",
    r"\b(?:read|see|look at)\s+(?:the\s+)?screen\b",
    r"\bwhat(?:'s| is)?\s+on\s+(?:my\s+|the\s+)?screen\b",
    r"\bscreen\s*e\s*ki\b", r"\bki\s*dekhtecho\b", r"\bocr\b",
    r"স্ক্রিন\s*(?:দেখো|পড়ো)", r"পর্দা\s*(?:দেখো|পড়ো|য় কি)",
    r"স্ক্রিনে?\s*কিস?\s*আছে", r"কি\s*দেখতেছো", r"কী\s*দেখছো",
]

LOCK_WORDS = ["lock pc", "lock screen", "lock computer", "pc lock", "screen lock",
              "পিসি লক", "স্ক্রিন লক", "লক করো", "কম্পিউটার লক"]

FOLDER_WORDS = {
    "downloads": ["download", "downloads", "ডাউনলোড"],
    "documents": ["document", "documents", "ডকুমেন্ট"],
    "desktop": ["desktop", "ডেস্কটপ"],
    "pictures": ["picture", "pictures", "photos", "ছবির ফোল্ডার", "পিকচার"],
    "music": ["music folder", "গানের ফোল্ডার"],
    "videos": ["video folder", "ভিডিও ফোল্ডার"],
}

STATUS_WORDS = ["time", "somoy", "somoe", "kota baje", "koto baje", "koyta baje",
                "date", "tarikh", "battery", "battary", "charge", "cpu", "ram",
                "memory usage", "status", "ghori", "ঘড়ি", "কটা বাজে", "কয়টা বাজে",
                "কটা বাজে", "সময়", "তারিখ", "ব্যাটারি", "চার্জ", "কতক্ষণ"]
QUESTION_HINTS = ["what time", "current time", "how much battery", "battery status"]

REMEMBER_WORDS = ["mone rakho", "mone rakhis", "mone rekho", "remember that",
                  "remember", "মনে রাখো", "মনে রাখিস", "মনে রেখো"]
RECALL_WORDS = ["amar somporke", "amar simporke", "amar pochondo", "amar ki pochondo",
                "amake cheno", "what do you know about me", "know about me",
                "remember about me", "আমার সম্পর্কে", "আমার পছন্দ", "আমার প্রিয়",
                "আমাকে চেনো"]

AGENT_TRIGGERS = ["nije nije koro", "nijei koro", "autonomous", "agent chai",
                  "computer agent", "desktop agent", "multi task", "multitask",
                  "step by step koro", "নিজে নিজে", "অটোনোমাস", "এজেন্ট",
                  "একাই সব"]
# Multi-step hint: "X ar tarpor Y" / "X then Y"
_MULTI_STEP = re.compile(r"\b(?:then|tarpor|tarpore|er por|porer step e|তারপর|এরপর)\b")

QUESTION_GUARDS = [
    r"\bki\s+korbo\b", r"\bki\s+koro\b", r"\bki\s+kori\b", r"\bki\s+bhabe\b",
    r"\bkivabe\b", r"\bkeno\b", r"\bhow\s+(?:to|do|can)\b", r"\bwhy\b",
    r"\bki\s+jonno\b", r"\bjonno\s+ki\s+korbo\b",
    r"কীভাবে", r"কেন", r"কি\s*করবো", r"কী\s*করব",
]

# ─────────────────────────────────────────────────────────────────────────────
#  Result helpers — every reply reports REAL tool values
# ─────────────────────────────────────────────────────────────────────────────


def _res(tool: str, args: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    return {"tool": tool, "args": args, "result": result}


def _tool_val(result: Dict[str, Any], key: str, default: Any = None) -> Any:
    inner = result.get("result", {})
    if isinstance(inner, dict):
        return inner.get(key, default)
    return default


# ─────────────────────────────────────────────────────────────────────────────
#  Individual intent handlers (each returns dict result or None)
# ─────────────────────────────────────────────────────────────────────────────


def _route_volume(text: str) -> Optional[Dict[str, Any]]:
    if not _has_word(text, VOL_WORDS):
        return None
    actions: List[Dict[str, Any]] = []
    num = _first_number(text)

    if _has_word(text, UNMUTE_WORDS):
        res = execute_tool("set_mute", mute=False)
        actions.append(_res("set_mute", {"mute": False}, res))
        return {"response": "আওয়াজ আবার চালু করে দিয়েছি বন্ধু! এখন সব স্পষ্ট শুনতে পাবে।",
                "actions": actions, "source": "command_router"}

    if _has_word(text, MUTE_WORDS):
        res = execute_tool("set_mute", mute=True)
        actions.append(_res("set_mute", {"mute": True}, res))
        return {"response": "সাউন্ড পুরোপুরি মিউট করে দিয়েছি। আবার শুনতে চাইলে শুধু বলো!",
                "actions": actions, "source": "command_router"}

    if "full" in text or "max" in text or "100" in text.split():
        res = execute_tool("set_volume", level=100)
        actions.append(_res("set_volume", {"level": 100}, res))
        v = _tool_val(res, "current_volume", 100)
        return {"response": f"ভলিউম ফুল {v}% করে দিয়েছি বন্ধু! এবার জমে উঠে শোনো।",
                "actions": actions, "source": "command_router"}

    if _has_word(text, UP_WORDS):
        delta = num if num is not None else 15
        res = execute_tool("adjust_volume", delta=delta)
        actions.append(_res("adjust_volume", {"delta": delta}, res))
        v = _tool_val(res, "current_volume", "?")
        return {"response": f"ভলিউম বাড়িয়ে {v}% করে দিয়েছি বন্ধু! আর বাড়াতে চাইলে এক ফোঁটায় বলো।",
                "actions": actions, "source": "command_router"}

    if _has_word(text, DOWN_WORDS):
        delta = -num if num is not None else -15
        res = execute_tool("adjust_volume", delta=delta)
        actions.append(_res("adjust_volume", {"delta": delta}, res))
        v = _tool_val(res, "current_volume", "?")
        return {"response": f"ভলিউম কমিয়ে {v}% করে দিয়েছি বন্ধু। এখন কি আরাম লাগছে?",
                "actions": actions, "source": "command_router"}

    if num is not None:
        level = max(0, min(100, num))
        res = execute_tool("set_volume", level=level)
        actions.append(_res("set_volume", {"level": level}, res))
        from ..agent_bridge import get_agent_bridge
        try:
            get_agent_bridge().set_preference("volume.level", str(level))
        except Exception:
            pass
        return {"response": f"ভলিউম একদম তোমার কথামতো {level}% এ সেট করে দিয়েছি এবং মনে রেখেছি!",
                "actions": actions, "source": "command_router"}

    # just "volume" mentioned but no verb/number → not a command; let chat handle it
    return None


def _route_brightness(text: str) -> Optional[Dict[str, Any]]:
    if not _has_word(text, BRIGHT_WORDS):
        return None
    actions = []
    num = _first_number(text)

    if _has_word(text, UP_WORDS):
        delta = num if num is not None else 20
        res = execute_tool("adjust_brightness", delta=delta)
        actions.append(_res("adjust_brightness", {"delta": delta}, res))
        b = _tool_val(res, "current_brightness", "?")
        return {"response": f"স্ক্রিনের ব্রাইটনেস বাড়িয়ে {b}% করে দিয়েছি বন্ধু, এবার সব পরিষ্কার দেখা যাবে।",
                "actions": actions, "source": "command_router"}
    if _has_word(text, DOWN_WORDS) or "dim" in text:
        delta = -num if num is not None else -20
        res = execute_tool("adjust_brightness", delta=delta)
        actions.append(_res("adjust_brightness", {"delta": delta}, res))
        b = _tool_val(res, "current_brightness", "?")
        return {"response": f"ব্রাইটনেস কমিয়ে {b}% করে দিয়েছি বন্ধু, চোখে আরাম লাগবে।",
                "actions": actions, "source": "command_router"}
    if num is not None:
        level = max(0, min(100, num))
        res = execute_tool("set_brightness", level=level)
        actions.append(_res("set_brightness", {"level": level}, res))
        return {"response": f"স্ক্রিনের ব্রাইটনেস {level}% করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}
    return None


def _find_app(text: str, mapping: Dict[str, List[str]]) -> Optional[str]:
    for app, aliases in mapping.items():
        for a in aliases:
            if a in text:
                return app
    return None


def _route_apps(text: str) -> Optional[Dict[str, Any]]:
    is_open = _has_word(text, OPEN_WORDS)
    is_close = _has_word(text, CLOSE_WORDS)
    if not (is_open or is_close):
        return None

    if is_close:
        app = _find_app(text, CLOSE_APP_MAP)
        if app:
            res = execute_tool("close_application", app_name=app)
            return {"response": f"{app.capitalize()} বন্ধ করে দিয়েছি বন্ধু!",
                    "actions": [_res("close_application", {"app_name": app}, res)],
                    "source": "command_router"}
        # "bondho koro" with no app found → let it fall through, don't misfire

    if is_open:
        app = _find_app(text, APP_MAP)
        if app == "youtube":
            return None  # handled by the smarter YouTube router (search support)
        if app:
            res = execute_tool("launch_application", app_name=app)
            return {"response": f"তোমার জন্য {app.capitalize()} খুলে দিয়েছি বন্ধু!",
                    "actions": [_res("launch_application", {"app_name": app}, res)],
                    "source": "command_router"}

        # Folder shortcuts ("downloads khulo")
        for folder, aliases in FOLDER_WORDS.items():
            if any(a in text for a in aliases):
                res = execute_tool("open_special_folder", folder=folder)
                return {"response": f"তোমার {folder.capitalize()} ফোল্ডারটি খুলে দিয়েছি বন্ধু!",
                        "actions": [_res("open_special_folder", {"folder": folder}, res)],
                        "source": "command_router"}
    return None


def _route_youtube_control(text: str) -> Optional[Dict[str, Any]]:
    """Route YouTube playback-control commands: play/pause, next, prev, speed,
    quality, fullscreen, close, forward, rewind, mute, captions, trending,
    analytics, and history.

    Must run BEFORE the YouTube search router so that "pause koro" does not
    accidentally try to search YouTube for the word "pause".
    """
    actions: List[Dict[str, Any]] = []

    # ── Close YouTube tab ────────────────────────────────────────────────
    if any(w in text for w in YT_CLOSE_TAB_WORDS):
        res = execute_tool("youtube_close_tab")
        actions.append(_res("youtube_close_tab", {}, res))
        return {"response": "YouTube ট্যাব বন্ধ করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── YouTube analyse ──────────────────────────────────────────────────
    if _has_word(text, YT_ANALYSE_WORDS) and any(w in text for w in ["youtube", "ইউটিউব", "yt"]):
        res = execute_tool("youtube_analyse")
        msg = _tool_val(res, "message") or "YouTube analysis সম্পন্ন!"
        actions.append(_res("youtube_analyse", {}, res))
        return {"response": msg, "actions": actions, "source": "command_router"}

    # ── Trending ─────────────────────────────────────────────────────────
    if _has_word(text, YT_TRENDING_WORDS):
        # Detect category
        cat = "default"
        if _has_word(text, SONG_WORDS + ["music", "মিউজিক", "গান"]):
            cat = "music"
        elif _has_word(text, ["gaming", "game", "গেম", "গেমিং"]):
            cat = "gaming"
        elif _has_word(text, ["news", "খবর", "সংবাদ"]):
            cat = "news"
        elif _has_word(text, ["sports", "খেলা", "স্পোর্টস"]):
            cat = "sports"
        res = execute_tool("youtube_get_trending", category=cat)
        msg = _tool_val(res, "message") or "ট্রেন্ডিং ভিডিও দেখা গেছে!"
        actions.append(_res("youtube_get_trending", {"category": cat}, res))
        return {"response": msg, "actions": actions, "source": "command_router"}

    # ── History ───────────────────────────────────────────────────────────
    if _has_word(text, YT_HISTORY_WORDS):
        # "ager video ta chalao" / "last video chalao" → play from history
        if _has_word(text, PLAY_WORDS + ["chalao", "চালাও", "bajao", "বাজাও", "play"]):
            res = execute_tool("youtube_play_last")
            msg = _tool_val(res, "message") or "আগের ভিডিও চালিয়ে দিয়েছি!"
            actions.append(_res("youtube_play_last", {}, res))
            return {"response": msg, "actions": actions, "source": "command_router"}
        # "history kholo" / "history dekho" → open history page or show list
        if _has_word(text, OPEN_WORDS + ["dekho", "দেখো", "দেখাও", "dekhao"]):
            res = execute_tool("youtube_open_history_page")
            actions.append(_res("youtube_open_history_page", {}, res))
            return {"response": "তোমার YouTube Watch History পেজ খুলে দিয়েছি বন্ধু!",
                    "actions": actions, "source": "command_router"}
        # bare "history" → show local history list
        res = execute_tool("youtube_get_history", limit=5)
        history = _tool_val(res, "history") or []
        if history:
            lines = [f"{i+1}. {h['title']}" for i, h in enumerate(history[:5])]
            msg = "তোমার সাম্প্রতিক YouTube history:\n" + "\n".join(lines)
        else:
            msg = _tool_val(res, "message") or "কোনো history নেই।"
        actions.append(_res("youtube_get_history", {"limit": 5}, res))
        return {"response": msg, "actions": actions, "source": "command_router"}

    # ── The following controls only make sense when a YouTube video is playing ──
    # We check for YouTube-context words or just allow bare playback commands
    _yt_context = any(w in text for w in
                      ["youtube", "ইউটিউব", "yt", "video", "ভিডিও", "গান", "gaan", "gan"])

    # ── Pause / Stop ─────────────────────────────────────────────────────
    if _has_word(text, YT_PAUSE_WORDS):
        res = execute_tool("youtube_play_pause")
        actions.append(_res("youtube_play_pause", {}, res))
        return {"response": "ভিডিও পজ করে দিয়েছি বন্ধু! আবার চালাতে বলো।",
                "actions": actions, "source": "command_router"}

    # ── Resume / Play (bare, no search query) ────────────────────────────
    if _has_word(text, YT_RESUME_WORDS) and (_yt_context or len(text.split()) <= 2):
        res = execute_tool("youtube_play_pause")
        actions.append(_res("youtube_play_pause", {}, res))
        return {"response": "ভিডিও আবার চালু করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    has_time = bool(re.search(r"\d+\s*(?:sec|second|min|minute|সেকেন্ড|মিনিট)", text))

    # ── Next Chapter ─────────────────────────────────────────────────────
    if _has_word(text, YT_NEXT_CHAPTER_WORDS):
        res = execute_tool("youtube_next_chapter")
        actions.append(_res("youtube_next_chapter", {}, res))
        return {"response": "পরের চ্যাপ্টারে চলে গেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Previous Chapter ─────────────────────────────────────────────────
    if _has_word(text, YT_PREV_CHAPTER_WORDS):
        res = execute_tool("youtube_prev_chapter")
        actions.append(_res("youtube_prev_chapter", {}, res))
        return {"response": "আগের চ্যাপ্টারে ফিরে গেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Next video ────────────────────────────────────────────────────────
    if _has_word(text, YT_NEXT_WORDS) and not has_time:
        res = execute_tool("youtube_next")
        actions.append(_res("youtube_next", {}, res))
        return {"response": "পরের ভিডিও চালিয়ে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Previous video ───────────────────────────────────────────────────
    if _has_word(text, YT_PREV_WORDS) and not has_time:
        res = execute_tool("youtube_previous")
        actions.append(_res("youtube_previous", {}, res))
        return {"response": "আগের ভিডিওতে ফিরে গেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Speed control ────────────────────────────────────────────────────
    if _has_word(text, YT_SPEED_WORDS):
        # Extract speed value
        speed_match = re.search(r"(\d+\.?\d*)\s*x", text)
        if speed_match:
            speed = float(speed_match.group(1))
        elif "double" in text or "ডবল" in text:
            speed = 2.0
        elif _has_word(text, ["normal", "নর্মাল"]):
            speed = 1.0
        elif _has_word(text, UP_WORDS + ["barao", "বাড়াও", "doruto", "দ্রুত"]):
            speed = 2.0
        elif _has_word(text, DOWN_WORDS + ["komao", "কমাও"]):
            speed = 0.5
        else:
            speed = 2.0
        res = execute_tool("youtube_set_speed", speed=speed)
        actual = _tool_val(res, "speed", speed)
        actions.append(_res("youtube_set_speed", {"speed": speed}, res))
        return {"response": f"ভিডিওর স্পিড {actual}x করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── HD / Quality ─────────────────────────────────────────────────────
    if _has_word(text, YT_HD_WORDS):
        quality_match = re.search(r"(\d{3,4})p", text)
        if quality_match:
            quality = quality_match.group(0)
        elif "4k" in text or "2160" in text:
            quality = "2160p"
        elif "2k" in text or "1440" in text:
            quality = "1440p"
        elif "full hd" in text or "ফুল এইচডি" in text or "1080" in text:
            quality = "1080p"
        else:
            quality = "1080p"
        res = execute_tool("youtube_set_quality", quality=quality)
        actions.append(_res("youtube_set_quality", {"quality": quality}, res))
        return {"response": f"ভিডিওর কোয়ালিটি {quality} করে দিয়েছি বন্ধু! এবার স্পষ্ট দেখতে পাবে।",
                "actions": actions, "source": "command_router"}

    # ── Fullscreen ────────────────────────────────────────────────────────
    if _has_word(text, YT_FULLSCREEN_WORDS):
        res = execute_tool("youtube_fullscreen")
        actions.append(_res("youtube_fullscreen", {}, res))
        return {"response": "ফুলস্ক্রিন করে দিয়েছি বন্ধু! বড় পর্দায় উপভোগ করো।",
                "actions": actions, "source": "command_router"}

    # ── Restart from beginning ───────────────────────────────────────────
    if _has_word(text, YT_RESTART_WORDS):
        res = execute_tool("youtube_restart")
        actions.append(_res("youtube_restart", {}, res))
        return {"response": "ভিডিও একদম শুরু থেকে চালিয়ে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Theater mode ─────────────────────────────────────────────────────
    if _has_word(text, YT_THEATER_WORDS):
        res = execute_tool("youtube_theater_mode")
        actions.append(_res("youtube_theater_mode", {}, res))
        return {"response": "YouTube থিয়েটার মোড টগল করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Miniplayer mode ──────────────────────────────────────────────────
    if _has_word(text, YT_MINIPLAYER_WORDS):
        res = execute_tool("youtube_miniplayer")
        actions.append(_res("youtube_miniplayer", {}, res))
        return {"response": "YouTube মিনিপ্লেয়ার টগল করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── YouTube in-player volume ─────────────────────────────────────────
    if _has_word(text, YT_PLAYER_VOL_WORDS):
        is_up = _has_word(text, UP_WORDS + ["barao", "বাড়াও", "বাড়িয়ে"])
        delta = 15 if is_up else -15
        res = execute_tool("youtube_adjust_player_volume", delta=delta)
        actions.append(_res("youtube_adjust_player_volume", {"delta": delta}, res))
        msg = "YouTube ভিডিওর সাউন্ড বাড়িয়ে দিয়েছি!" if is_up else "YouTube ভিডিওর সাউন্ড কমিয়ে দিয়েছি!"
        return {"response": msg, "actions": actions, "source": "command_router"}

    # ── Jump to position / middle ────────────────────────────────────────
    if _has_word(text, YT_POSITION_WORDS):
        res = execute_tool("youtube_seek_to_position", percent=50)
        actions.append(_res("youtube_seek_to_position", {"percent": 50}, res))
        return {"response": "ভিডিওর মাঝামাঝি জায়গায় চলে গেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Forward / Seek ahead (seconds or minutes) ────────────────────────
    is_browser_nav = any(w in text for w in ["browser", "ব্রাউজার", "page", "পেজ"])
    if _has_word(text, YT_FORWARD_WORDS) and not is_browser_nav:
        min_match = re.search(r"(\d+)\s*(?:min|minute|মিনিট)", text)
        sec_match = re.search(r"(\d+)\s*(?:sec|second|সেকেন্ড)", text)
        if min_match:
            mins = int(min_match.group(1))
            secs = mins * 60
            unit_str = f"{mins} মিনিট"
        elif sec_match:
            secs = int(sec_match.group(1))
            unit_str = f"{secs} সেকেন্ড"
        else:
            secs = 10
            unit_str = "10 সেকেন্ড"

        res = execute_tool("youtube_seek_by_time", seconds=secs, direction="forward")
        actions.append(_res("youtube_seek_by_time", {"seconds": secs, "direction": "forward"}, res))
        return {"response": f"{unit_str} সামনে এগিয়ে গেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Rewind / Seek back (seconds or minutes) ──────────────────────────
    if _has_word(text, YT_REWIND_WORDS) and not is_browser_nav:
        min_match = re.search(r"(\d+)\s*(?:min|minute|মিনিট)", text)
        sec_match = re.search(r"(\d+)\s*(?:sec|second|সেকেন্ড)", text)
        if min_match:
            mins = int(min_match.group(1))
            secs = mins * 60
            unit_str = f"{mins} মিনিট"
        elif sec_match:
            secs = int(sec_match.group(1))
            unit_str = f"{secs} সেকেন্ড"
        else:
            secs = 10
            unit_str = "10 সেকেন্ড"

        res = execute_tool("youtube_seek_by_time", seconds=secs, direction="backward")
        actions.append(_res("youtube_seek_by_time", {"seconds": secs, "direction": "backward"}, res))
        return {"response": f"{unit_str} পেছনে গেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Video mute (YouTube player mute, not system) ─────────────────────
    if _has_word(text, YT_MUTE_VID_WORDS):
        res = execute_tool("youtube_mute_video")
        actions.append(_res("youtube_mute_video", {}, res))
        return {"response": "YouTube ভিডিও মিউট করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Captions / Subtitles ──────────────────────────────────────────────
    if _has_word(text, YT_CAPTION_WORDS) and _yt_context:
        res = execute_tool("youtube_toggle_captions")
        actions.append(_res("youtube_toggle_captions", {}, res))
        return {"response": "ক্যাপশন/সাবটাইটেল টগল করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Loop / Repeat video ──────────────────────────────────────────────
    if _has_word(text, YT_LOOP_WORDS):
        res = execute_tool("youtube_toggle_loop")
        actions.append(_res("youtube_toggle_loop", {}, res))
        return {"response": "ভিডিও লুপ মোড টগল করে দিয়েছি বন্ধু! এখন এটা বারবার চলতে থাকবে।",
                "actions": actions, "source": "command_router"}


    # ── Copy Video Link / URL ────────────────────────────────────────────
    if _has_word(text, YT_COPY_URL_WORDS):
        res = execute_tool("youtube_copy_url")
        url = _tool_val(res, "url", "")
        actions.append(_res("youtube_copy_url", {}, res))
        if url:
            return {"response": f"ভিডিওর লিংক ক্লিপবোর্ডে কপি করে নিয়েছি: {url}",
                    "actions": actions, "source": "command_router"}
        return {"response": "ভিডিওর লিংক ক্লিপবোর্ডে কপি করে দিয়েছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Scroll Comments / Description ────────────────────────────────────
    if _has_word(text, YT_SCROLL_TOP_WORDS):
        res = execute_tool("youtube_scroll_top")
        actions.append(_res("youtube_scroll_top", {}, res))
        return {"response": "একদম উপরে ভিডিও প্লেয়ারে চলে গেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    if _has_word(text, YT_SCROLL_DOWN_WORDS):
        res = execute_tool("youtube_scroll_down")
        actions.append(_res("youtube_scroll_down", {}, res))
        return {"response": "নিচে স্ক্রোল করেছি বন্ধু! কমেন্টস দেখতে পারো।",
                "actions": actions, "source": "command_router"}

    if _has_word(text, YT_SCROLL_UP_WORDS):
        res = execute_tool("youtube_scroll_up")
        actions.append(_res("youtube_scroll_up", {}, res))
        return {"response": "উপরে স্ক্রোল করে আবার ভিডিওতে ফিরে এসেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Skip to End of Video ─────────────────────────────────────────────
    if _has_word(text, YT_END_WORDS):
        res = execute_tool("youtube_seek_to_end")
        actions.append(_res("youtube_seek_to_end", {}, res))
        return {"response": "ভিডিওর একদম শেষে চলে গেছি বন্ধু!",
                "actions": actions, "source": "command_router"}

    # ── Focus YouTube Search Bar ─────────────────────────────────────────
    if _has_word(text, YT_SEARCH_BAR_WORDS):
        res = execute_tool("youtube_focus_search")
        actions.append(_res("youtube_focus_search", {}, res))
        return {"response": "YouTube সার্চ বার সিলেক্ট করে দিয়েছি, এখন টাইপ করতে পারো!",
                "actions": actions, "source": "command_router"}

    return None


def _route_youtube(text: str) -> Optional[Dict[str, Any]]:
    yt_aliases = ["youtube", "ইউটিউব", "ইউটুব", "ইউটিউপ", "youtub", "youtybe", "youtyb", "ytub"]
    has_yt = any(w in text for w in yt_aliases) or bool(re.search(r"\byt\b", text))
    if not has_yt:
        return None

    # Check for Channel search: "youtube channel X kholo" / "X channel dekhao"
    chan_match = re.search(r"(?:channel|চ্যানেল)\s+(.+?)(?:\s+(?:kholo|dekhao|open|খোল|খোলো|দেখাও)|$)", text)
    if chan_match and any(w in text for w in ["channel", "চ্যানেল"]):
        c_name = _strip_fillers(chan_match.group(1)).strip()
        if c_name and c_name not in yt_aliases:
            res = execute_tool("youtube_search_channel", channel_name=c_name)
            return {"response": f"YouTube চ্যানেল '{c_name}' খুলে দিয়েছি বন্ধু!",
                    "actions": [_res("youtube_search_channel", {"channel_name": c_name}, res)],
                    "source": "command_router"}

    # "youtube e X chalao / search / khujo"  OR  "X youtube e chalao"  OR  "youtube e chalao X"
    m = (re.search(r"(?:youtube|ইউটিউবে?|ইউটুব|ইউটিউপ|youtub|youtybe|youtyb|\byt\b)\s*(?:e|te)?\s+(?:search|khujo|khunjo|chalao|bajao|play|খোঁজ|চালাও|বাজাও)\s+(?:kor\w*\s+)?(.+)", text)
         or re.search(r"(.+?)\s+(?:search|chalao|bajao|play|চালাও|বাজাও)\s+(?:on\s+|in\s+)?(?:youtube|ইউটিউবে?|ইউটুব|ইউটিউপ|youtub|youtybe|youtyb|\byt\b)\b", text)
         or re.search(r"(?:youtube|ইউটিউবে?|ইউটুব|ইউটিউপ|youtub|youtybe|youtyb|\byt\b)\s*(?:e|te)?\s+(.+?)\s+(?:search|khujo|khunjo|chalao|bajao|play|খোঁজ|চালাও|বাজাও)(?:\s+kor\w*|\s+dao)?$", text)
         or re.search(r"(?:play|search|chalao|bajao)\s+(.+?)\s+(?:on|in)\s+(?:youtube|ইউটিউবে?|youtub|\byt\b)\b", text))

    if m:
        q = _strip_fillers(m.group(1))
        q = re.sub(r"\b" + r"\b|\b".join(map(re.escape, SONG_WORDS + PLAY_WORDS)) + r"\b", " ", q)
        q = q.strip()
        if q:
            is_play = _has_word(text, PLAY_WORDS) or not _has_word(text, ["search", "khujo", "khunjo", "খোঁজ"])
            res = execute_tool("search_web", query=q, target="youtube", play=is_play)
            # Record to local history
            try:
                from ..tools.youtube.history import record_youtube_play
                url = _tool_val(res, "url") or f"https://www.youtube.com/results?search_query={q}"
                record_youtube_play(url=url, title=q, query=q)
            except Exception:
                pass
            verb = "চালিয়ে" if is_play else "খুঁজে"
            return {"response": f"ইউটিউবে তোমার জন্য '{q}' {verb} দিয়েছি বন্ধু! উপভোগ করো।",
                    "actions": [_res("search_web", {"query": q, "target": "youtube"}, res)],
                    "source": "command_router"}
    # bare youtube — but if the user is asking to PLAY something (song), let the
    # smarter song router build a proper search query instead.
    if _has_word(text, PLAY_WORDS):
        return None
    res = execute_tool("launch_application", app_name="youtube")
    return {"response": "ইউটিউব খুলে দিয়েছি বন্ধু! পছন্দের ভিডিও দেখে নাও।",
            "actions": [_res("launch_application", {"app_name": "youtube"}, res)],
            "source": "command_router"}



def _route_song(text: str) -> Optional[Dict[str, Any]]:
    has_song = _has_word(text, SONG_WORDS)
    has_play = _has_word(text, PLAY_WORDS)
    play_prefix_match = re.match(r"^(?:play|chalaw|chalao|bajao|shonao|চালাও|বাজাও|শোনাও)\s+(.+)$", text)
    if not ((has_song and has_play) or (play_prefix_match and not any(app in text for app in APP_MAP))):
        return None

    if play_prefix_match and not (has_song and has_play):
        q = play_prefix_match.group(1)
    else:
        q = text

    q = re.sub(r"\b(?:youtube|ইউটিউবে?|ইউটুব|yt)\b", " ", q)
    q = re.sub(r"\b" + r"\b|\b".join(map(re.escape, SONG_WORDS + PLAY_WORDS)) + r"\b", " ", q)
    q = _strip_fillers(q)
    q = re.sub(r"\s*\b(?:e|te|er|ke|ta|ti|ta)\b\s*", " ", q).strip()
    if len(q) < 2:
        from ..agent_bridge import get_agent_bridge
        try:
            q = get_agent_bridge().preferred_str("youtube.query", "bangla top hit songs")
        except Exception:
            q = "bangla top hit songs"
    res = execute_tool("search_web", query=q, target="youtube", play=True)
    # Record to local history
    try:
        from ..tools.youtube.history import record_youtube_play
        url = _tool_val(res, "url") or f"https://www.youtube.com/results?search_query={q}"
        record_youtube_play(url=url, title=q, query=q)
    except Exception:
        pass
    return {"response": f"ইউটিউবে তোমার জন্য '{q}' চালিয়ে দিয়েছি বন্ধু! মজে শোনো।",
            "actions": [_res("search_web", {"query": q, "target": "youtube"}, res)],
            "source": "command_router"}


# ─────────────────────────────────────────────────────────────────────────────
#  Web Browser Control & Intelligent Search
# ─────────────────────────────────────────────────────────────────────────────

WEB_NEW_TAB_WORDS = ["new tab", "notun tab", "notun tab kholo", "নতুন ট্যাব", "নতুন ট্যাব খোলো"]
WEB_CLOSE_TAB_WORDS = ["tab close", "close tab", "tab bondho", "ট্যাব বন্ধ", "ট্যাব বন্ধ করো", "ট্যাব কেটে দাও"]
WEB_REOPEN_TAB_WORDS = ["reopen tab", "restore tab", "বন্ধ ট্যাব খোলো", "ট্যাব রিস্টোর", "আগের ট্যাব খোলো"]
WEB_RELOAD_WORDS = ["reload", "refresh", "page reload", "page refresh", "রিলোড", "রিলোড করো", "রিফ্রেশ", "রিফ্রেশ করো"]
WEB_BACK_WORDS = ["page back", "browser back", "browser e pichone", "আগের পেজে যাও", "পেছনের পেজে যাও"]
WEB_FORWARD_WORDS = ["page forward", "browser forward", "browser e samne", "পরের পেজে যাও", "সামনের পেজে যাও"]
WEB_ZOOM_IN_WORDS = ["zoom in", "zoom koro", "zoom barao", "জুম করো", "জুম বাড়াও", "জুম বাড়াও", "লেখা বড় করো", "লেখা বড় করো"]
WEB_ZOOM_OUT_WORDS = ["zoom out", "zoom komao", "জুম কমাও", "লেখা ছোট করো"]
WEB_ZOOM_RESET_WORDS = ["zoom reset", "normal zoom", "reset zoom", "নর্মাল জুম", "জুম রিসেট"]

SEARCH_INDICATORS = [
    "search", "khujo", "khunjo", "khujte", "khuje", "google", "গুগল",
    "সার্চ", "খোঁজো", "খোঁজ", "খুঁজতে", "খুঁজে", "উইকিপিডিয়া", "উইকিপিডিয়া",
    "wikipedia", "web", "internet", "ইন্টারনেট", "find out", "look up"
]


def _route_web_control(text: str) -> Optional[Dict[str, Any]]:
    """Route web browser control commands: tabs, navigation, reload, zoom, URLs."""
    # Direct website opening: e.g. "website kholo google.com" or "facebook.com kholo"
    domain_match = re.search(r"\b([a-zA-Z0-9-]+\.(?:com|org|net|edu|gov|io|ai|co|in|bd|dev))\b", text)
    if domain_match and any(w in text for w in ["kholo", "open", "jao", "খোলো", "খোল", "যাও", "website", "ওয়েবসাইট"]):
        domain = domain_match.group(1)
        res = execute_tool("web_open_url", url=domain)
        return {"response": f"ওয়েবসাইট '{domain}' ব্রাউজারে খুলে দিয়েছি বন্ধু!",
                "actions": [_res("web_open_url", {"url": domain}, res)],
                "source": "command_router"}

    # New tab
    if _has_word(text, WEB_NEW_TAB_WORDS):
        res = execute_tool("web_new_tab")
        return {"response": "ব্রাউজারে একটি নতুন ট্যাব খুলে দিয়েছি বন্ধু!",
                "actions": [_res("web_new_tab", {}, res)], "source": "command_router"}

    # Close tab (skip if this is clearly a YouTube-specific command already handled)
    if _has_word(text, WEB_CLOSE_TAB_WORDS):
        res = execute_tool("web_close_tab")
        return {"response": "ব্রাউজার ট্যাবটি বন্ধ করে দিয়েছি বন্ধু!",
                "actions": [_res("web_close_tab", {}, res)], "source": "command_router"}

    # Reopen tab
    if _has_word(text, WEB_REOPEN_TAB_WORDS):
        res = execute_tool("web_reopen_tab")
        return {"response": "আগের বন্ধ করা ট্যাবটি পুনরায় খুলে দিয়েছি বন্ধু!",
                "actions": [_res("web_reopen_tab", {}, res)], "source": "command_router"}

    # Reload / Refresh
    if _has_word(text, WEB_RELOAD_WORDS):
        res = execute_tool("web_reload_page")
        return {"response": "ওয়েব পেজটি রিফ্রেশ করে দিয়েছি বন্ধু!",
                "actions": [_res("web_reload_page", {}, res)], "source": "command_router"}

    # Browser Back
    if _has_word(text, WEB_BACK_WORDS):
        res = execute_tool("web_navigate_back")
        return {"response": "ব্রাউজারের আগের পেজে ফিরে গেছি বন্ধু!",
                "actions": [_res("web_navigate_back", {}, res)], "source": "command_router"}

    # Browser Forward
    if _has_word(text, WEB_FORWARD_WORDS):
        res = execute_tool("web_navigate_forward")
        return {"response": "ব্রাউজারের পরের পেজে চলে গেছি বন্ধু!",
                "actions": [_res("web_navigate_forward", {}, res)], "source": "command_router"}

    # Zoom In
    if _has_word(text, WEB_ZOOM_IN_WORDS):
        res = execute_tool("web_zoom_in")
        return {"response": "ওয়েব পেজ জুম ইন করে বড় করে দিয়েছি বন্ধু!",
                "actions": [_res("web_zoom_in", {}, res)], "source": "command_router"}

    # Zoom Out
    if _has_word(text, WEB_ZOOM_OUT_WORDS):
        res = execute_tool("web_zoom_out")
        return {"response": "ওয়েব পেজ জুম আউট করে দিয়েছি বন্ধু!",
                "actions": [_res("web_zoom_out", {}, res)], "source": "command_router"}

    # Zoom Reset
    if _has_word(text, WEB_ZOOM_RESET_WORDS):
        res = execute_tool("web_zoom_reset")
        return {"response": "ওয়েব পেজের জুম ১০০% নর্মাল রিসেট করে দিয়েছি!",
                "actions": [_res("web_zoom_reset", {}, res)], "source": "command_router"}

    return None


def _route_search(text: str) -> Optional[Dict[str, Any]]:
    """Professional natural-language web search router with conversational noise removal."""
    # If text is solely an app launch request, let _route_apps handle it
    if text.strip() in ["google", "google kholo", "open google", "গুগল", "গুগল খোলো"]:
        return None

    has_search_hint = any(w in text for w in SEARCH_INDICATORS)
    if not has_search_hint:
        return None

    from ..tools.web.search import extract_clean_search_query
    q, engine = extract_clean_search_query(text)
    if not q or len(q) < 2:
        return None

    # If YouTube was requested
    if engine == "youtube":
        res = execute_tool("search_web", query=q, target="youtube", play=False)
        return {
            "response": f"ইউটিউবে তোমার জন্য '{q}' সার্চ করে দিয়েছি বন্ধু!",
            "actions": [_res("search_web", {"query": q, "target": "youtube"}, res)],
            "source": "command_router"
        }

    # Always attempt to fetch a direct factual summary so Star Assistant speaks the answer aloud
    quick_ans = ""
    try:
        ans_res = execute_tool("web_quick_answer", query=q)
        inner = ans_res.get("result") if isinstance(ans_res.get("result"), dict) else ans_res
        if inner.get("success") and inner.get("answer"):
            quick_ans = inner["answer"].strip()
    except Exception:
        pass

    # Open search in browser so the user can inspect more details at their convenience
    res = execute_tool("web_search", query=q, engine=engine, open_browser=True)
    engine_name = "উইকিপিডিয়া" if engine == "wikipedia" else "গুগল"

    has_bengali_chars = bool(re.search(r"[\u0980-\u09FF]", text))
    banglish_markers = [
        "kore", "bolo", "bol", "khujte", "bolchi", "bolsi", "somporke",
        "niye", "nia", "koro", "kor", "dao", "dekhao", "kothay", "kivabe",
        "ki", "jeno", "khujo", "khunjo", "amake", "amar"
    ]
    has_banglish = any(re.search(rf"\b{w}\b", text.lower()) for w in banglish_markers)
    is_pure_english = (not has_bengali_chars) and (not has_banglish) and any(
        text.lower().startswith(w) for w in ["search for", "google for", "look up", "find out about", "search web for"]
    )

    if is_pure_english:
        if quick_ans:
            sep = "" if quick_ans.endswith((".", "!", "?")) else "."
            reply = f"{quick_ans}{sep} I've also opened the page in your browser so you can check it out later!"
        else:
            reply = f"I've searched for '{q}' on {engine.capitalize()} and opened it in your browser for you!"
    else:
        if quick_ans:
            sep = "" if quick_ans.endswith(("।", ".", "!", "?")) else "।"
            reply = f"{quick_ans}{sep} তোমার দেখার সুবিধার্থে ব্রাউজারে পেজটি খুলে দিয়েছি বন্ধু, তুমি পরে আরও বিস্তারিত দেখে নিতে পারো!"
        elif engine == "wikipedia":
            reply = f"উইকিপিডিয়াতে তোমার জন্য '{q}' সার্চ করে ব্রাউজারে খুলে দিয়েছি বন্ধু, তুমি সেখান থেকে দেখে নাও!"
        else:
            reply = f"গুগলে তোমার জন্য '{q}' সার্চ করে ব্রাউজারে খুলে দিয়েছি বন্ধু, তুমি সেখান থেকে দেখে নাও!"

    return {
        "response": reply,
        "actions": [_res("web_search", {"query": q, "engine": engine}, res)],
        "source": "command_router"
    }



def _route_screenshot(text: str) -> Optional[Dict[str, Any]]:
    if not _has_word(text, SCREENSHOT_WORDS):
        return None
    res = execute_tool("take_screenshot")
    return {"response": "তোমার পুরো স্ক্রিনের স্ক্রিনশট নিয়ে পিকচার্স ফোল্ডারে সেভ করে দিয়েছি বন্ধু!",
            "actions": [_res("take_screenshot", {}, res)], "source": "command_router"}


def _route_vision(text: str) -> Optional[Dict[str, Any]]:
    for pat in VISION_PATTERNS:
        if re.search(pat, text):
            break
    else:
        return None
    res = execute_tool("see_screen", query="*")
    msg = ""
    ok = bool(res.get("success")) and bool(res.get("result", {}).get("success"))
    if ok:
        msg = res.get("result", {}).get("message", "") or ""
    if msg.strip() not in ("", "Screen analyzed", "[]"):
        preview = msg[:250]
        return {"response": f"আমি তোমার স্ক্রিন পড়েছি বন্ধু! পর্দায় যা দেখতে পেয়েছি: {preview}",
                "actions": [_res("see_screen", {"query": "*"}, res)], "source": "command_router"}
    return {"response": "স্ক্রিন স্ক্যান করেছি বন্ধু, তবে পড়ার মতো স্পষ্ট কোনো টেক্সট পাইনি।",
            "actions": [_res("see_screen", {"query": "*"}, res)], "source": "command_router"}


def _route_lock(text: str) -> Optional[Dict[str, Any]]:
    if not _has_word(text, LOCK_WORDS):
        return None
    res = execute_tool("lock_workstation")
    return {"response": "কম্পিউটার স্ক্রিন লক করে দিয়েছি বন্ধু! ফিরে এলে দেখা হবে।",
            "actions": [_res("lock_workstation", {}, res)], "source": "command_router"}


def _route_folder(text: str) -> Optional[Dict[str, Any]]:
    if "folder" not in text and "ফোল্ডার" not in text:
        return None
    for folder, aliases in FOLDER_WORDS.items():
        if any(a in text for a in aliases):
            res = execute_tool("open_special_folder", folder=folder)
            return {"response": f"তোমার {folder.capitalize()} ফোল্ডারটি খুলে দিয়েছি বন্ধু!",
                    "actions": [_res("open_special_folder", {"folder": folder}, res)],
                    "source": "command_router"}
    return None


def _route_status(text: str) -> Optional[Dict[str, Any]]:
    if not (_has_word(text, STATUS_WORDS) or any(q in text for q in QUESTION_HINTS)):
        return None
    # Question guard: "battery keno kharap korche" / "ram kivabe barabo" are
    # QUESTIONS about the topic, not status requests.
    if any(re.search(q, text) for q in QUESTION_GUARDS):
        return None
    res = execute_tool("get_system_status")
    st = res.get("result", {})
    if not isinstance(st, dict):
        st = {}
    return {"response": (f"এখন সময় {st.get('time', 'জানি না')}, ব্যাটারি চার্জ {st.get('battery', '?')}, "
                         f"আর CPU লোড প্রায় {st.get('cpu_usage', '?')}।"),
            "actions": [_res("get_system_status", {}, res)], "source": "command_router"}


_MATH_OPS = {"+": "+", "-": "-", "*": "*", "x": "*", "×": "*", "/": "/", "÷": "/"}


def _route_math(text: str) -> Optional[Dict[str, Any]]:
    word_ops = text
    for bn, sym in [("গুণ", "*"), ("ভাগ", "/"), ("যোগ", "+"), ("বিয়োগ", "-"),
                    ("gun", "*"), ("gunno", "*"), ("guna", "*"),
                    ("bhag", "/"), ("vag", "/"), ("bhaag", "/"),
                    ("jog", "+"), ("biyog", "-"),
                    ("minus", "-"), ("plus", "+"), ("into", "*")]:
        word_ops = word_ops.replace(bn, f" {sym} ")
    m = re.search(r"(\d+(?:\.\d+)?)\s*([\+\-\*\/x×÷])\s*(\d+(?:\.\d+)?)", word_ops)
    if not m:
        return None
    op = _MATH_OPS.get(m.group(2), m.group(2))
    expr = f"{m.group(1)} {op} {m.group(3)}"
    res = execute_tool("calculate_math", expression=expr)
    ans = _tool_val(res, "result")
    if ans is None:
        return None
    return {"response": f"হিসাব করে দেখলাম বন্ধু, উত্তর হলো {ans}!",
            "actions": [_res("calculate_math", {"expression": expr}, res)],
            "source": "command_router"}


def _route_memory(text: str, original: str) -> Optional[Dict[str, Any]]:
    if any(p in text for p in REMEMBER_WORDS):
        fact = re.sub(r"\b(?:mone rakho|mone rakhis|mone rekho|remember that|remember|মনে রাখো|মনে রাখিস|মনে রেখো)\b",
                      "", original, flags=re.IGNORECASE).strip(" :,-")
        if fact:
            from ..agent_bridge import get_agent_bridge
            get_agent_bridge().remember(fact, kind="fact")
            return {"response": f"আমি তোমার এই কথাটি পরম যত্নে মনে রাখলাম বন্ধু: '{fact}'।",
                    "actions": [], "source": "command_router"}
    if any(p in text for p in RECALL_WORDS):
        from ..agent_bridge import get_agent_bridge
        ctx = get_agent_bridge().get_memory_context(original)
        if ctx:
            facts = "\n".join(l.strip("- ") for l in ctx.splitlines() if l.strip().startswith("-"))
            if facts.strip():
                return {"response": f"তোমার সম্পর্কে আমি যা মনে রেখেছি বন্ধু:\n{facts}",
                        "actions": [], "source": "command_router"}
    return None


def _route_agent_goal(text: str, original: str) -> Optional[Dict[str, Any]]:
    """Route genuine multi-step / autonomous goals to the ReAct agent."""
    explicit = any(t in text for t in AGENT_TRIGGERS)
    multi = bool(_MULTI_STEP.search(text)) and _has_word(
        text, ["koro", "kor", "kore dao", "khulo", "kholo", "chalao", "bajao",
               "barao", "komao", "de", "dao", "করো", "খোলো", "চালাও", "বাজাও"])
    if not (explicit or multi):
        return None

    goal = re.sub(
        r"\b(?:nije nije|nijei|autonomously|autonomous|agent|computer agent|desktop agent|"
        r"step by step|multitask|multi task|নিজে নিজে|অটোনোমাস|এজেন্ট দিয়ে|এজেন্ট)\b",
        " ", original, flags=re.IGNORECASE).strip(" ,.-")
    if not goal:
        goal = "Take a screenshot"
    res = execute_tool("execute_autonomous_goal", goal=goal)
    ok = bool(res.get("success")) and bool(res.get("result", {}).get("success"))
    if ok:
        steps = res.get("result", {}).get("steps_count", 1)
        return {"response": f"নিজে নিজে কাজটি সম্পন্ন করেছি বন্ধু! '{goal}' — মোট {steps} টি ধাপে সফল হয়েছে।",
                "actions": [_res("execute_autonomous_goal", {"goal": goal}, res)],
                "source": "command_router (agent)"}
    err = res.get("result", {}).get("error") or res.get("error") or "অজানা সমস্যা"
    return {"response": f"কম্পিউটার এজেন্ট কাজটি করতে গিয়ে একটু সমস্যায় পড়েছে: {err}",
            "actions": [_res("execute_autonomous_goal", {"goal": goal}, res)],
            "source": "command_router (agent)"}


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry
# ─────────────────────────────────────────────────────────────────────────────

# (handler, takes_original_text) — order matters: cheapest & safest first.
# NOTE: the agent (multi-step) router runs BEFORE single-action routers so that
# "youtube khule gaan chalao tarpor volume 30 koro" is executed as ONE agent
# episode instead of just the last single action.
_ROUTER_ORDER = [
    (_route_memory, True),              # remember/recall — pure words, zero side effects
    (_route_agent_goal, True),          # multi-step goals — must beat single actions
    (_route_volume, False),
    (_route_brightness, False),
    (_route_screenshot, False),         # BEFORE vision ("screenshot" must not hit vision)
    (_route_vision, False),
    (_route_lock, False),
    (_route_folder, False),
    (_route_youtube_control, False),    # YT playback control — BEFORE YouTube search
    (_route_youtube, False),
    (_route_song, False),
    (_route_web_control, False),        # Web browser tabs, zoom, reload, URLs
    (_route_search, False),             # Professional web search with query cleaner
    (_route_apps, False),
    (_route_status, False),
    (_route_math, False),
]


def route_command(user_text: str) -> Optional[Dict[str, Any]]:
    """Deterministic command router.

    Returns the provider-style result dict on a confident command match,
    or ``None`` so the caller can fall through to chat engines.
    """
    if not user_text or not user_text.strip():
        return None

    text = _normalize(user_text)
    if not text:
        return None

    for handler, needs_original in _ROUTER_ORDER:
        try:
            result = handler(text, user_text) if needs_original else handler(text)
        except Exception:
            continue
        if result:
            result.setdefault("intent", "command")
            return result
    return None
