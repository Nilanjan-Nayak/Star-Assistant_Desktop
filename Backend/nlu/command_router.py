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
    "now", "ekhon", "এখন", "joldi", "তাড়াতাড়ি", "প্লিজ", "tomar", "মজে",
    "lagbe", "lagchhe", "lagche", "chai", "chao", "dorkar", "sunbo", "shonbo",
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
            "full", "max", "জোরে", "জোরালো", "উঁচু", "বাড়াও", "বাড়িয়ে", "বাড়া"]
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
              "shonaw", "sunao", "lagao", "on koro", "chalu koro",
              "চালাও", "বাজাও", "শোনাও", "শোনাউ", "বাজায় দাও", "চালিয়ে দাও"]

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


def _route_youtube(text: str) -> Optional[Dict[str, Any]]:
    if not any(w in text for w in ["youtube", "ইউটিউব", "ইউটুব", "yt"]):
        return None

    # "youtube e X chalao / search / khujo"  OR  "X youtube e chalao"
    m = (re.search(r"(?:youtube|ইউটিউবে?|ইউটুব|yt)\s*(?:e|te)?\s+(?:search|khujo|khunjo|chalao|bajao|play|খোঁজ|চালাও|বাজাও)\s+(?:kor\w*\s+)?(.+)", text)
         or re.search(r"(.+?)\s+(?:search|chalao|bajao|play|চালাও|বাজাও)\s+(?:on\s+|in\s+)?(?:youtube|ইউটিউবে?|ইউটুব|yt)\b", text))
    if m:
        q = _strip_fillers(m.group(1))
        q = re.sub(r"\b" + r"\b|\b".join(map(re.escape, SONG_WORDS + PLAY_WORDS)) + r"\b", " ", q)
        q = q.strip()
        if q:
            res = execute_tool("search_web", query=q, target="youtube")
            return {"response": f"ইউটিউবে তোমার জন্য '{q}' খুঁজে চালিয়ে দিয়েছি বন্ধু! ভালো লাগুক!",
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
    if not (_has_word(text, SONG_WORDS) and _has_word(text, PLAY_WORDS)):
        return None
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
    res = execute_tool("search_web", query=q, target="youtube")
    return {"response": f"ইউটিউবে তোমার জন্য '{q}' চালিয়ে দিয়েছি বন্ধু! মজে শোনো।",
            "actions": [_res("search_web", {"query": q, "target": "youtube"}, res)],
            "source": "command_router"}


def _route_search(text: str) -> Optional[Dict[str, Any]]:
    if not _has_word(text, ["search", "khujo", "khunjo", "google koro", "google e khonjo",
                            "সার্চ", "খোঁজো", "খোঁজ", "গুগল করো"]):
        return None
    target = "youtube" if any(w in text for w in ["youtube", "ইউটিউব", "yt"]) else "google"
    q = re.sub(r"\b(?:google|গুগলে?|search|সার্চ|khujo|khunjo|খোঁজো|খোঁজ|koro|করো|kor|for|on|in|the|e|te|er|ke)\b",
               " ", text)
    q = _strip_fillers(q).strip()
    if not q or len(q) < 2:
        return None
    res = execute_tool("search_web", query=q, target=target)
    if target == "youtube":
        reply = f"ইউটিউবে '{q}' চালিয়ে দিয়েছি বন্ধু!"
    else:
        reply = f"গুগলে '{q}' লিখে সার্চ করে দিয়েছি বন্ধু, ব্রাউজারে দেখে নাও!"
    return {"response": reply,
            "actions": [_res("search_web", {"query": q, "target": target}, res)],
            "source": "command_router"}


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
    (_route_memory, True),      # remember/recall — pure words, zero side effects
    (_route_agent_goal, True),  # multi-step goals — must beat single actions
    (_route_volume, False),
    (_route_brightness, False),
    (_route_screenshot, False), # BEFORE vision ("screenshot" must not hit vision)
    (_route_vision, False),
    (_route_lock, False),
    (_route_folder, False),
    (_route_youtube, False),
    (_route_song, False),
    (_route_search, False),
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
