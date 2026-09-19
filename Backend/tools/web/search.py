"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — Professional Web Search & Clean Query Extraction
═══════════════════════════════════════════════════════════════════════════════

  Solves the conversational voice search problem:
    - Strips filler speech ("search kore khujte bolchi", "amake bolo", "somporke")
    - Isolates the exact search entity/topic with 100% precision
    - Auto-detects target engine (Google, Wikipedia, News, YouTube, DuckDuckGo)
    - Provides real-time instant factual answers via Wikipedia/DuckDuckGo REST APIs
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict, Optional, Tuple

from ..registry import register_tool

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Wikimedia API specifically requires descriptive application User-Agent
_WIKI_USER_AGENT = "StarAssistant/1.0 (Desktop AI Assistant; contact@starassistant.ai)"


# ─────────────────────────────────────────────────────────────────────────────
#  Professional Natural Language Search Query Extractor
# ─────────────────────────────────────────────────────────────────────────────

def extract_clean_search_query(text: str) -> Tuple[str, str]:
    """
    Extract a professional, clean search query and the target engine from
    conversational voice commands in Bengali script, Banglish, and English.

    Returns:
        tuple (clean_query_string, target_engine)
    """
    t = (text or "").strip()
    if not t:
        return "", "google"

    # 1. Detect target engine
    engine = "google"
    t_lower = t.lower()
    if any(w in t_lower for w in ["wikipedia", "উইকিপিডিয়া", "উইকিপিডিয়া", "wiki"]):
        engine = "wikipedia"
    elif any(re.search(rf"\b{re.escape(w)}\b", t_lower) for w in ["youtube", "yt"]) or any(w in t_lower for w in ["ইউটিউব", "ইউটুব", "ইউটিউপ"]):
        engine = "youtube"
    elif "bing" in t_lower:
        engine = "bing"
    elif "duckduckgo" in t_lower:
        engine = "duckduckgo"
    elif any(w in t_lower for w in ["khobor", "news", "সংবাদ", "খবর"]):
        if any(w in t_lower for w in ["search", "সার্চ", "google", "গুগল"]):
            engine = "news"

    # 2. Normalize Bengali digits if any
    bn_digits = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
    clean = t.translate(bn_digits)

    # 3. Strip wake words & conversational greetings
    clean = re.sub(
        r"^(?:\s*(?:hey |ok |oi |aye )?(?:star|স্টার)\b[!,.]*\s*)+",
        "", clean, flags=re.IGNORECASE
    ).strip()
    clean = re.sub(
        r"^(?:শোনো|শোন|shono|shun|suno|bollo|বলো|বল|bolo|hey|হেই|please|plz|দাদা|ভাই|বন্ধু|bhai|dada|bondhu|bandhu)\s+",
        "", clean, flags=re.IGNORECASE
    ).strip()

    # 4. Strip leading conversational request wrappers
    clean = re.sub(
        r"^(?:amake|amar\s+jonno|amar\s+jonne|amar\s+jonyo|amake\s+diye|আমাকে|আমার\s+জন্য|আমার\s+জন্যে|একটু|ektu|zara|জারা)\s+",
        "", clean, flags=re.IGNORECASE
    ).strip()

    # 5. Multi-word search command prefixes
    prefixes = [
        # Search & find combinations
        r"^(?:search\s+kore\s+(?:khujte\s+bolchi|khujte\s+bolsi|khujte\s+bolo|khujo|khunjo|ber\s+koro|dao|bolo|dekhao))\s+",
        r"^(?:সার্চ\s+করে\s+(?:খুঁজতে\s+বলছি|খুঁজতে\s+বলো|খোঁজো|বের\s+করো|দাও|বলো|দেখাও))\s+",
        r"^(?:khujte\s+bolchi|khujte\s+bolsi|khujte\s+bolo|খুঁজতে\s+বলছি|খুঁজতে\s+বলো)\s+",
        r"^(?:kichu\s+)?(?:khuje\s+(?:ber\s+koro|dao|bolo|dekhao|ani)|খুঁজে\s+(?:বের\s+করো|দাও|বলো|দেখাও))\s+",

        # Engine-specific prefixes
        r"^(?:google|গুগলে?|গুগল)\s*(?:[ea]|te|এ|তে)?\s*(?:giye\s+|গিয়ে\s+|গিয়ে\s+)?(?:search\s+kore\s+(?:bolo|dao|dekhao)|search\s*(?:koro|kor|korun)?|খোঁজো|খোঁজ|সার্চ\s*(?:করে\s+(?:বলো|দাও|দেখাও)|করো|কর|করুন)?|khujo|khunjo|করো|koro)?\s+",
        r"^(?:wikipedia|উইকিপিডিয়াতে?|উইকিপিডিয়াতে?|উইকিপিডিয়া|উইকিপিডিয়া)\s*(?:te|e|তে|এ)?\s*(?:search\s*(?:koro|kor|korun)?|সার্চ\s*(?:করো|কর|করুন)?|খোঁজো|খোঁজ|khujo)?\s+",
        r"^(?:web|internet|ইন্টারনেটে?|ইন্টারনেট|ওয়েবে?|ওয়েব|ওয়েবে?|ওয়েব)\s*(?:e|te|এ|তে)?\s*(?:search\s*(?:koro|kor|korun)?|সার্চ\s*(?:করো|কর|করুন)?|খোঁজো|খোঁজ|khujo)?\s+",
        r"^(?:youtube|ইউটিউবে?|ইউটিউব)\s*(?:e|te|এ|তে)?\s*(?:search\s*(?:koro|kor|korun)?|সার্চ\s*(?:করো|কর|করুন)?|খোঁজো|খোঁজ|khujo)?\s+",

        # English prefixes
        r"^(?:please\s+)?(?:can\s+you\s+)?(?:search\s+(?:for|about|and\s+find(?:\s+out)?(?:\s+about)?)|look\s+up|find\s+out\s+about|google)\s+",

        # Bare search words
        r"^(?:search\s+kore\s+(?:bolo|dao|dekhao)|search\s+koro|search\s+kor|search\s+korun|search|সার্চ\s+করে\s+(?:বলো|দাও|দেখাও)|সার্চ\s+করো|সার্চ\s+কর|সার্চ\s+করুন|সার্চ)\s+",
        r"^(?:khujo|khunjo|khunj|khuj|খোঁজো|খোঁজ)\s+",
    ]
    for pat in prefixes:
        clean = re.sub(pat, "", clean, flags=re.IGNORECASE).strip()

    # 6. Suffixes (topic-first or trailing search commands)
    suffixes = [
        r"\s+(?:somporke|somporkey|shomporke|সম্পর্কে|নিয়ে|নিয়ে|niye|nia)\s+(?:search\s+kore\s+(?:bolo|dao|dekhao)|search\s+koro|search|খোঁজো|খোঁজ|সার্চ\s+করো|সার্চ|bolo|বলো|জানতে\s+চাই|jante\s+chai)$",
        r"\s+(?:somporke|somporkey|shomporke|সম্পর্কে|নিয়ে|নিয়ে|niye|nia)$",
        r"\s+(?:search\s+kore\s+(?:bolo|dao|dekhao)|search\s+koro|search\s+kor|search|সার্চ\s+করো|সার্চ|খুঁজে\s+দাও|khuje\s+dao|খোঁজো|khujo)$",
        r"\s+(?:google\s+e|google\s+a|গুগলে|গুগল\s+এ)$",
    ]
    for pat in suffixes:
        clean = re.sub(pat, "", clean, flags=re.IGNORECASE).strip()

    # 7. Residual leading noise words
    clean = re.sub(r"^(?:for|about|ekta|ekti|kono|akta|akti|একটা|একটি|কোনো)\s+", "", clean, flags=re.IGNORECASE).strip()

    # 8. Trailing objective markers
    clean = re.sub(r"\s+(?:ke|কে|ta|টা)$", "", clean, flags=re.IGNORECASE).strip()

    # Strip surrounding quotes and punctuation
    clean = clean.strip(" '\"`.,!?:;-")
    return clean, engine


# ─────────────────────────────────────────────────────────────────────────────
#  Registered Web Search Tools
# ─────────────────────────────────────────────────────────────────────────────

@register_tool(
    name="web_search",
    description="Professional web search using Google, Wikipedia, Bing, or DuckDuckGo."
)
def web_search(query: str, engine: str = "google", open_browser: bool = True) -> Dict[str, Any]:
    """Execute a professional web search on the target search engine."""
    clean_q, detected_engine = extract_clean_search_query(query)
    target_q = clean_q or query.strip()
    active_engine = (engine or detected_engine or "google").lower()
    encoded = urllib.parse.quote_plus(target_q)

    if active_engine == "wikipedia":
        # Direct Wikipedia search URL
        has_bengali = bool(re.search(r"[\u0980-\u09FF]", target_q))
        lang = "bn" if has_bengali else "en"
        url = f"https://{lang}.wikipedia.org/wiki/Special:Search?search={encoded}"
    elif active_engine == "news":
        url = f"https://news.google.com/search?q={encoded}&hl=bn"
    elif active_engine == "bing":
        url = f"https://www.bing.com/search?q={encoded}"
    elif active_engine == "duckduckgo":
        url = f"https://duckduckgo.com/?q={encoded}"
    elif active_engine == "youtube":
        url = f"https://www.youtube.com/results?search_query={encoded}"
    else:  # Google default
        url = f"https://www.google.com/search?q={encoded}"

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            return {"success": False, "query": target_q, "engine": active_engine, "error": str(e)}

    return {
        "success": True,
        "query": target_q,
        "engine": active_engine,
        "url": url,
        "message": f"'{target_q}' লিখে {active_engine.capitalize()}-এ সার্চ করা হয়েছে।"
    }


@register_tool(
    name="google_search",
    description="Perform a clean Google search for any topic, news, or question."
)
def google_search(query: str) -> Dict[str, Any]:
    """Convenience tool for Google web searching."""
    return web_search(query=query, engine="google", open_browser=True)


def clean_speech_text(text: str, max_chars: int = 250) -> str:
    """Prepare search extract text for natural, crisp TTS voice output."""
    if not text:
        return ""
    t = text.strip()
    # Remove HTML tags
    t = re.sub(r"<[^>]+>", "", t)
    # Remove citations like [1], [2], [note 1]
    t = re.sub(r"\[[^\]]*\]", "", t)
    # Remove pronunciation / audio artifacts like (শুনুনi), (/ˈiːlɒn/)
    t = re.sub(r"\([^)]*(?:শুনুন|listen|IPA|pronunciation|/)[^)]*\)", "", t, flags=re.IGNORECASE)
    # Clean empty parentheses and orphan brackets
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\s+\)", "", t)
    t = re.sub(r"\(\s+", "", t)
    # Clean double daari
    t = re.sub(r"।+", "।", t)
    # Normalize whitespace
    t = re.sub(r"\s+", " ", t).strip()

    # Take first 1 to 2 clean sentences
    sentences = re.split(r"(?<=[.!?।])\s+", t)
    if not sentences:
        return t[:max_chars]

    result = sentences[0]
    if len(sentences) > 1 and len(result) + len(sentences[1]) < max_chars:
        result += " " + sentences[1]
    elif len(result) > max_chars:
        result = result[:max_chars].rsplit(" ", 1)[0] + "..."
    return result.strip()


@register_tool(
    name="wikipedia_search",
    description="Search Wikipedia and retrieve article summary for people, places, concepts or history."
)
def wikipedia_search(query: str, lang: str = "auto", open_browser: bool = True) -> Dict[str, Any]:
    """Search Wikipedia with 2-step title discovery, language fallback (bn/en), and instant summary."""
    clean_q, _ = extract_clean_search_query(query)
    target_q = clean_q or query.strip()
    if not target_q:
        return {"success": False, "error": "খোঁজার জন্য কোনো বিষয় পাওয়া যায়নি।"}

    # Language priority: Bengali first for regional queries, English fallback
    has_bengali = bool(re.search(r"[\u0980-\u09FF]", target_q))
    if lang == "bn":
        langs_to_try = ["bn", "en"]
    elif lang == "en":
        langs_to_try = ["en", "bn"]
    else:
        langs_to_try = ["bn", "en"] if has_bengali else ["bn", "en"]

    headers = {"User-Agent": _WIKI_USER_AGENT}
    summary_text = ""
    title = target_q
    full_url = ""
    found_lang = "bn"

    for l in langs_to_try:
        try:
            enc_search = urllib.parse.quote_plus(target_q)
            search_api = (
                f"https://{l}.wikipedia.org/w/api.php?action=query&list=search"
                f"&srsearch={enc_search}&utf8=&format=json&srlimit=1"
            )
            req = urllib.request.Request(search_api, headers=headers)
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                search_results = data.get("query", {}).get("search", [])
                if search_results:
                    top_title = search_results[0]["title"]
                    enc_title = urllib.parse.quote(top_title.replace(" ", "_"))
                    sum_url = f"https://{l}.wikipedia.org/api/rest_v1/page/summary/{enc_title}"
                    req2 = urllib.request.Request(sum_url, headers=headers)
                    with urllib.request.urlopen(req2, timeout=2.5) as resp2:
                        sum_data = json.loads(resp2.read().decode("utf-8"))
                        raw_extract = sum_data.get("extract", "")
                        if raw_extract and len(raw_extract) > 15:
                            summary_text = clean_speech_text(raw_extract)
                            title = top_title
                            full_url = f"https://{l}.wikipedia.org/wiki/{enc_title}"
                            found_lang = l
                            break
        except Exception:
            continue

    # Open full article in browser if requested
    if open_browser and full_url:
        try:
            webbrowser.open(full_url)
        except Exception:
            pass

    if summary_text:
        return {
            "success": True,
            "title": title,
            "summary": summary_text,
            "url": full_url,
            "lang": found_lang,
            "message": f"উইকিপিডিয়া থেকে '{title}' সম্পর্কে তথ্য আনা হয়েছে।"
        }
    return {
        "success": False,
        "title": target_q,
        "summary": "",
        "url": full_url,
        "message": f"উইকিপিডিয়ায় '{target_q}' সম্পর্কিত কোনো নিবন্ধ পাওয়া যায়নি।"
    }


@register_tool(
    name="web_quick_answer",
    description="Fetch an instant direct factual answer from the web without opening a browser."
)
def web_quick_answer(query: str) -> Dict[str, Any]:
    """Fetch a clean factual answer suitable for spoken TTS output from Wikipedia, DDG, or snippets."""
    clean_q, _ = extract_clean_search_query(query)
    target_q = clean_q or query.strip()
    if not target_q:
        return {"success": False, "answer": "", "source": "none"}

    # 1. High-accuracy 2-step Wikipedia search (Bengali & English)
    wiki_res = wikipedia_search(query=target_q, open_browser=False)
    if wiki_res.get("success") and wiki_res.get("summary"):
        return {
            "success": True,
            "query": target_q,
            "answer": wiki_res["summary"],
            "title": wiki_res.get("title", target_q),
            "source": f"Wikipedia ({wiki_res.get('lang', 'bn').upper()})",
            "url": wiki_res.get("url", "")
        }

    # 2. DuckDuckGo Instant Answer API
    try:
        encoded = urllib.parse.quote_plus(target_q)
        ddg_url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(ddg_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_answer = data.get("AbstractText") or data.get("Answer")
            if raw_answer:
                clean_ans = clean_speech_text(raw_answer)
                return {
                    "success": True,
                    "query": target_q,
                    "answer": clean_ans,
                    "title": data.get("Heading", target_q),
                    "source": "DuckDuckGo Instant Answer",
                    "url": data.get("AbstractURL", "")
                }
    except Exception:
        pass

    # 3. DuckDuckGo HTML Search Snippet fallback
    try:
        encoded = urllib.parse.quote_plus(target_q)
        ddg_html_url = f"https://html.duckduckgo.com/html/?q={encoded}"
        req = urllib.request.Request(ddg_html_url, headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "bn-IN,bn;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            snippets = re.findall(r'class="result__snippet[^"]*"[^>]*>(.*?)</a>', html, flags=re.DOTALL)
            if snippets:
                clean_snippet = clean_speech_text(snippets[0])
                if clean_snippet and len(clean_snippet) > 15:
                    return {
                        "success": True,
                        "query": target_q,
                        "answer": clean_snippet,
                        "title": target_q,
                        "source": "Web Search Snippet",
                        "url": f"https://www.google.com/search?q={encoded}"
                    }
    except Exception:
        pass

    return {
        "success": False,
        "query": target_q,
        "answer": "",
        "message": "তাৎক্ষণিক উত্তর পাওয়া যায়নি, ব্রাউজারে সার্চ করা ভালো।"
    }


@register_tool(
    name="search_web",
    description="Search Google, Wikipedia, or YouTube in the default browser."
)
def search_web(query: str, target: str = "google", play: bool = False) -> Dict[str, Any]:
    """Universal search tool maintained for backward compatibility with smart extraction."""
    clean_q, detected = extract_clean_search_query(query)
    eff_query = clean_q or query.strip()
    eff_target = (target or detected or "google").lower()

    if eff_target == "youtube" or "youtube" in eff_target:
        from ..youtube.search import search_web as yt_search
        return yt_search(query=eff_query, target="youtube", play=play)

    return web_search(query=eff_query, engine=eff_target, open_browser=True)
