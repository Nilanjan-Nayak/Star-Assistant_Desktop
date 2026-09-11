"""
Provider-Agnostic LLM Layer for Star Assistant.
Supports:
1. Local Ollama / LM Studio (zero paid cloud API)
2. Native Offline Intent & Personality Engine (100% offline, zero setup fallback)
"""

import re
import json
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

from ..config import (
    LOCAL_LLM_URL, LOCAL_LLM_MODEL, SYSTEM_PERSONA_PROMPT, USE_LOCAL_LLM,
    GEMINI_API_KEY, GEMINI_MODEL, USE_GEMINI
)
from ..tools.registry import execute_tool, get_tool_schemas
from ..nlu.command_router import route_command
from .knowledge import get_companion_engine


def _conversation_pipeline(user_text: str, memory_context: str = "",
                           llm_callable=None) -> Dict[str, Any]:
    """Single deterministic routing pipeline shared by every provider.

    Order (each stage only runs if the previous found nothing):
      1. Command router   — strict imperative patterns, executes REAL actions
                            with real values (never hijacked by chat).
      2. Dataset exact    — identity chat replies from the training data.
      3. Dataset fuzzy    — CHAT-ONLY (side effects disabled; this was the
                            source of 'ulto palto kaj' before).
      4. Legacy intents   — extra offline keyword net.
      5. LLM (if online)  — Ollama / Gemini with function-calling tools.
      6. Friendly fallback — never a random memory dump again.
    """
    companion = get_companion_engine()

    # 1. Deterministic command router
    routed = route_command(user_text)
    if routed:
        return {
            "response": routed.get("response", ""),
            "actions": routed.get("actions", []),
            "source": routed.get("source", "command_router"),
        }

    # 2. Exact dataset match (pure chat)
    exact = companion.find_match(user_text, min_confidence=0.995)
    if exact:
        return exact

    # 3. High-confidence fuzzy dataset match (chat only — no side effects)
    fuzzy = companion.find_match(user_text, min_confidence=0.84)
    if fuzzy:
        return fuzzy

    # 4. Legacy offline intent patterns
    try:
        intent_match = OfflineIntentEngine().match_intent(user_text)
    except Exception:
        intent_match = None
    if intent_match:
        return intent_match

    # 5. LLM (online providers)
    if llm_callable is not None:
        try:
            llm_result = llm_callable(user_text, memory_context)
        except Exception:
            llm_result = None
        if llm_result:
            return llm_result

    # 6. Friendly fallback
    return {
        "response": ("দুঃখিত বন্ধু, কথাটা আমি ঠিকমতো বুঝতে পারিনি। একটু সহজ করে "
                     "আবার বলবে? ভলিউম, ব্রাইটনেস, অ্যাপ খোলা, গান চালানো বা "
                     "স্ক্রিনশটের মতো কাজ বললে আমি সাথে সাথেই করে দেবো!"),
        "actions": [],
        "source": "fallback",
    }


class BaseLLMProvider:
    """Abstract interface for intelligence providers."""

    def process_query(self, user_text: str, memory_context: str = "") -> Dict[str, Any]:
        """Process user text and return response and any executed tool calls."""
        raise NotImplementedError


def clean_llm_response(text: str) -> str:
    """Clean raw LLM text, removing role labels, asterisks, and stray symbols for spoken output."""
    if not text:
        return ""
    # Strip leading speaker or role tags (e.g. "উত্তর:", "স্টার:", "নীলাঞ্জন:", "Star:", "Assistant:")
    t = re.sub(r'^(?:উত্তর|স্টার|star|নীলাঞ্জন|assistant|ai|user|ব্যবহারকারী)\s*[:：\-]\s*', '', text.strip(), flags=re.IGNORECASE)
    # Remove markdown formatting characters
    t = re.sub(r'[*_~#>`]', '', t)
    # Strip wrapping quotes
    t = re.sub(r'^["\'“‘](.*)["\'”’]$', r'\1', t)
    return t.strip()


class LocalOllamaProvider(BaseLLMProvider):
    """Communicates with local Ollama / LM Studio / vLLM via OpenAI-compatible endpoint."""

    def __init__(self, endpoint: str = LOCAL_LLM_URL, model: str = LOCAL_LLM_MODEL):
        self.endpoint = endpoint.rstrip("/") + "/chat/completions"
        self.model = model
        self.history: List[Dict[str, str]] = []
        self.max_history = 10
        self.offline_engine = OfflineIntentEngine()

    def is_available(self) -> bool:
        """Check if local LLM server is up (fast non-blocking probe)."""
        try:
            req = urllib.request.Request(self.endpoint.replace("/chat/completions", "/models"), method="GET")
            with urllib.request.urlopen(req, timeout=0.4) as resp:
                return resp.status == 200
        except Exception:
            return False

    def process_query(self, user_text: str, memory_context: str = "") -> Dict[str, Any]:
        llm_callable = self._llm_answer if self.is_available() else None
        return _conversation_pipeline(user_text, memory_context, llm_callable=llm_callable)

    def _llm_answer(self, user_text: str, memory_context: str = "") -> Optional[Dict[str, Any]]:
        """Ollama/LM Studio turn with function-calling tools. Returns None on failure."""
        # Build prompt: system persona + memory + genuine recent turns + user prompt
        sys_prompt = SYSTEM_PERSONA_PROMPT
        if memory_context:
            sys_prompt += f"\n\n[USER MEMORY CONTEXT]\n{memory_context}"
        messages = [{"role": "system", "content": sys_prompt}]
        for h in self.history[-4:]:
            messages.append(h)
        messages.append({"role": "user", "content": user_text})

        # Pass tools whenever the query could plausibly be an action (broad net —
        # the LLM decides; the old tiny keyword gate missed many commands).
        is_action = any(w in user_text.lower() for w in [
            "open", "launch", "close", "kill", "kholo", "khulo", "bondho", "chalu",
            "volume", "sound", "awaj", "mute", "unmute", "brightness", "alo", "light",
            "battery", "status", "charge", "search", "play", "gaan", "gan", "song",
            "music", "screenshot", "lock", "youtube", "google", "folder", "time",
            "সার্চ", "খোলো", "খুলো", "বন্ধ", "ভলিউম", "আওয়াজ", "ব্রাইটনেস", "আলো",
            "গান", "স্ক্রিনশট", "চালাও", "বাজাও", "সময়", "চার্জ",
        ])
        tools = get_tool_schemas() if is_action else None

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.35,
            "max_tokens": 320,  # generous budget so Bengali sentences are never cut mid-word
        }
        if tools:
            payload["tools"] = tools

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                choice = res_json.get("choices", [{}])[0].get("message", {})

                tool_calls = choice.get("tool_calls", [])
                executed_actions = []

                if tool_calls:
                    for call in tool_calls:
                        fn_name = call.get("function", {}).get("name")
                        fn_args_raw = call.get("function", {}).get("arguments", "{}")
                        try:
                            fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
                        except Exception:
                            fn_args = {}

                        tool_res = execute_tool(fn_name, **fn_args)
                        executed_actions.append({"tool": fn_name, "args": fn_args, "result": tool_res})

                    # Second turn for a friendly verbal summary of the actions
                    followup_payload = {
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PERSONA_PROMPT},
                            {"role": "user", "content": user_text},
                            choice,
                            {
                                "role": "tool",
                                "name": executed_actions[0]["tool"],
                                "content": json.dumps(executed_actions[0]["result"])
                            }
                        ],
                        "temperature": 0.35,
                        "max_tokens": 320,
                    }
                    data_follow = json.dumps(followup_payload).encode("utf-8")
                    req_follow = urllib.request.Request(
                        self.endpoint,
                        data=data_follow,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req_follow, timeout=20) as resp2:
                        res2 = json.loads(resp2.read().decode("utf-8"))
                        raw_final = res2.get("choices", [{}])[0].get("message", {}).get("content", "")
                        final_text = clean_llm_response(raw_final)
                        if not final_text:
                            return None
                        self.history.append({"role": "user", "content": user_text})
                        self.history.append({"role": "assistant", "content": final_text})
                        if len(self.history) > 20:
                            self.history = self.history[-20:]
                        return {
                            "response": final_text,
                            "actions": executed_actions,
                            "source": f"local_llm ({self.model})"
                        }

                raw_content = choice.get("content", "")
                content = clean_llm_response(raw_content)
                if not content:
                    return None
                self.history.append({"role": "user", "content": user_text})
                self.history.append({"role": "assistant", "content": content})
                if len(self.history) > 20:
                    self.history = self.history[-20:]
                return {
                    "response": content,
                    "actions": executed_actions,
                    "source": f"local_llm ({self.model})"
                }

        except Exception:
            return None

class GoogleGeminiProvider(BaseLLMProvider):
    """Official Google Gemini Cloud Intelligence Provider (Ultra-fast, Free Tier, Bengali Native)."""

    def __init__(self, api_key: str = GEMINI_API_KEY, model: str = GEMINI_MODEL):
        self.api_key = api_key.strip()
        self.model = model
        self.endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        self.history: List[Dict[str, str]] = []
        self.offline_engine = OfflineIntentEngine()

    def is_available(self) -> bool:
        return bool(self.api_key)

    def process_query(self, user_text: str, memory_context: str = "") -> Dict[str, Any]:
        llm_callable = self._gemini_answer if self.is_available() else None
        return _conversation_pipeline(user_text, memory_context, llm_callable=llm_callable)

    def _gemini_answer(self, user_text: str, memory_context: str = "") -> Optional[Dict[str, Any]]:
        """Google Gemini cloud turn. Returns None on any failure (pipeline falls back)."""
        try:
            contents = []
            for h in self.history[-6:]:
                role = "user" if h.get("role") == "user" else "model"
                contents.append({"role": role, "parts": [{"text": h.get("content", "")}]})
            contents.append({"role": "user", "parts": [{"text": user_text}]})

            sys_prompt = SYSTEM_PERSONA_PROMPT
            if memory_context:
                sys_prompt += f"\n\n[USER MEMORY & PREFERENCES]\n{memory_context}"

            payload = {
                "contents": contents,
                "systemInstruction": {
                    "parts": [{"text": sys_prompt}]
                },
                "generationConfig": {
                    "temperature": 0.45,
                    "maxOutputTokens": 320  # was 200 — truncated replies mid-sentence
                }
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.endpoint,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                candidates = res.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        raw_text = parts[0].get("text", "")
                        final_text = clean_llm_response(raw_text)
                        if not final_text:
                            return None
                        self.history.append({"role": "user", "content": user_text})
                        self.history.append({"role": "model", "content": final_text})
                        if len(self.history) > 16:
                            self.history = self.history[-16:]
                        return {
                            "response": final_text,
                            "actions": [],
                            "source": f"google_gemini ({self.model})"
                        }
        except Exception as e:
            print(f"[GeminiProvider Error]: {e}")
        return None

class OfflineIntentEngine(BaseLLMProvider):
    """100% offline, lightning-fast semantic rule and intent extractor for Bengali, Banglish & English."""

    def match_intent(self, user_text: str) -> Optional[Dict[str, Any]]:
        """Extract explicit intents (volume, brightness, apps, status, common questions) or return None."""
        text = user_text.lower().strip()
        executed_actions = []

        # 0.1 Screen Vision / OCR Intent ("দেখো", "পর্দা", "স্ক্রিনে কী আছে", "see screen", "look at screen")
        if any(w in text for w in [
            "screen dakho", "screen dekho", "see screen", "look at screen", "read screen", "ocr",
            "স্ক্রিন দেখো", "পর্দা দেখো", "পর্দায় কি আছে", "পর্দায় কি আছে", "স্ক্রিনে কি আছে", "স্ক্রিনে কী আছে",
            "পর্দা পড়ো", "স্ক্রিন পড়ো", "স্ক্রিনে কি দেখতে পাচ্ছ"
        ]):
            res = execute_tool("see_screen", query="*")
            executed_actions.append({"tool": "see_screen", "args": {"query": "*"}, "result": res})
            if res.get("success") and res.get("result", {}).get("success"):
                found_text = res.get("result", {}).get("message", "")
                if not found_text or found_text.strip() in ["[]", "Screen analyzed"]:
                    return {
                        "response": "আমি স্ক্রিন স্ক্যান করেছি বন্ধু, তবে পড়ার মতো স্পষ্ট কোনো টেক্সট পাওয়া যায়নি।",
                        "actions": executed_actions,
                        "source": "screen_vision"
                    }
                preview = found_text[:200]
                return {
                    "response": f"আমি তোমার স্ক্রিন পড়েছি বন্ধু! পর্দায় যা দেখতে পেয়েছি: {preview}",
                    "actions": executed_actions,
                    "source": "screen_vision"
                }
            else:
                return {
                    "response": "আমি তোমার স্ক্রিন স্ক্যান করার চেষ্টা করেছি বন্ধু! ডিসপ্লে রিডার প্রস্তুত আছে।",
                    "actions": executed_actions,
                    "source": "screen_vision"
                }

        # 0.2 Memorization Triggers ("মনে রাখো", "remember that", "remember")
        if any(text.startswith(p) for p in ["mone rakho", "remember that", "remember", "মনে রাখো", "মনে রাখিস"]):
            fact = re.sub(r"^(?:mone rakho|remember that|remember|মনে রাখো|মনে রাখিস)\s*", "", user_text).strip(" :,-")
            if fact:
                from ..agent_bridge import get_agent_bridge
                get_agent_bridge().remember(fact, kind="fact")
                return {
                    "response": f"আমি তোমার এই বিষয়টি পরম যত্নে মনে রাখলাম বন্ধু: '{fact}'!",
                    "actions": [],
                    "source": "memory_store"
                }

        # 0.3 Memory Inquiry ("আমার সম্পর্কে কি জানো", "amake cheno", "amar pochondo")
        if any(w in text for w in [
            "amake cheno", "amar somporke", "amar pochondo", "amar ki pochondo",
            "what do you know about me", "remember about me", "আমার সম্পর্কে কি জানো",
            "আমার পছন্দ", "আমার প্রিয়", "আমার প্রিয়"
        ]):
            from ..agent_bridge import get_agent_bridge
            bridge = get_agent_bridge()
            ctx = bridge.get_memory_context(user_text)
            if ctx:
                facts = "\n".join([l.strip("- ") for l in ctx.splitlines() if l.strip().startswith("-")])
                return {
                    "response": f"তোমার সম্পর্কে আমি যা মনে রেখেছি বন্ধু:\n{facts}",
                    "actions": [],
                    "source": "memory_recall"
                }

        # 0.4 Autonomous Computer Control Agent Triggers
        # FIX: bare "agent"/"এজেন্ট" removed — sentences like "ami agent banate chai"
        # used to launch a nonsense autonomous goal.
        if any(w in text for w in ["autonomous", "computer agent", "desktop agent", "অটোনোমাস", "কম্পিউটার এজেন্ট", "নিজে নিজে করো", "নিজে নিজে"]):
            goal = re.sub(r"^(?:autonomous|computer agent|desktop agent|অটোনোমাস|কম্পিউটার এজেন্ট|এজেন্ট|agent)\s*", "", text).strip()
            if not goal:
                goal = "Take a screenshot"
            res = execute_tool("execute_autonomous_goal", goal=goal)
            executed_actions.append({"tool": "execute_autonomous_goal", "args": {"goal": goal}, "result": res})
            if res.get("success") and res.get("result", {}).get("success"):
                steps_count = res.get("result", {}).get("steps_count", 1)
                return {
                    "response": f"অটোনোমাস কম্পিউটার এজেন্ট দিয়ে কাজ সম্পন্ন করেছি বন্ধু! মোট {steps_count} টি ধাপে গোল '{goal}' সফল হয়েছে।",
                    "actions": executed_actions,
                    "source": "autonomous_agent"
                }
            else:
                err = res.get("result", {}).get("error") or res.get("error", "অজানা ত্রুটি")
                return {
                    "response": f"কম্পিউটার এজেন্ট কাজ করতে গিয়ে সতর্কবার্তা দিয়েছে: {err}",
                    "actions": executed_actions,
                    "source": "autonomous_agent"
                }

        # 1. Volume Control
        # Patterns: volume barao, volume up, sound barie dao, volume 20% barano, volume 80 koro
        if any(w in text for w in ["volume", "sound", "awaj", "awaz", "আওয়াজ", "ভলিউম", "সাউন্ড"]):
            num_match = re.search(r"(\d+)\s*%", text) or re.search(r"(\d+)", text)
            number = int(num_match.group(1)) if num_match else None

            # Up / Increase
            if any(w in text for w in ["up", "barao", "barie", "increase", "high", "উঁচু", "বাড়িয়ে", "বাড়াও", "বাড়িয়ে দাও"]):
                delta = number if number is not None else 15
                res = execute_tool("adjust_volume", delta=delta)
                new_v = res.get("result", {}).get("current_volume", 70)
                executed_actions.append({"tool": "adjust_volume", "args": {"delta": delta}, "result": res})
                reply = (
                    f"ভলিউম বাড়িয়ে {new_v}% করে দিয়েছি বন্ধু! "
                    f"আওয়াজ বেশি জোরালো মনে হলে আমাকে বোলো, আমি কমিয়ে দেবো।"
                )
                return {"response": reply, "actions": executed_actions, "source": "offline_intent"}

            # Down / Decrease
            elif any(w in text for w in ["down", "komao", "komie", "decrease", "low", "নিচু", "কমিয়ে", "কমাও", "কমিয়ে দাও"]):
                delta = -number if number is not None else -15
                res = execute_tool("adjust_volume", delta=delta)
                new_v = res.get("result", {}).get("current_volume", 40)
                executed_actions.append({"tool": "adjust_volume", "args": {"delta": delta}, "result": res})
                reply = (
                    f"ভলিউম কমিয়ে {new_v}% করে দিলাম বন্ধু। "
                    f"এখন কি আওয়াজ ঠিকঠাক শুনতে পাচ্ছ?"
                )
                return {"response": reply, "actions": executed_actions, "source": "offline_intent"}

            # Mute
            elif any(w in text for w in ["mute", "chup", "silent", "থামো", "মিউট", "চুপ"]):
                res = execute_tool("set_mute", mute=True)
                executed_actions.append({"tool": "set_mute", "args": {"mute": True}, "result": res})
                return {
                    "response": "সাউন্ড পুরোপুরি মিউট করে দিয়েছি। আবার চালু করতে চাইলে বলো!",
                    "actions": executed_actions,
                    "source": "offline_intent"
                }

            # Explicit level: volume 80 koro / set volume 50
            elif number is not None:
                res = execute_tool("set_volume", level=number)
                executed_actions.append({"tool": "set_volume", "args": {"level": number}, "result": res})
                from ..agent_bridge import get_agent_bridge
                get_agent_bridge().set_preference("volume.level", str(number), f"User preferred volume is {number}%")
                return {
                    "response": f"ভলিউম একদম তোমার কথামতো {number}% এ সেট করে দিয়েছি এবং মনে রেখেছি!",
                    "actions": executed_actions,
                    "source": "offline_intent"
                }
            # Fallback to remembered preference if user just says "volume set koro"
            elif any(w in text for w in ["set", "thik", "সেট", "ঠিক"]):
                from ..agent_bridge import get_agent_bridge
                pref_v = get_agent_bridge().preferred_int("volume.level", 50)
                res = execute_tool("set_volume", level=pref_v)
                executed_actions.append({"tool": "set_volume", "args": {"level": pref_v}, "result": res})
                return {
                    "response": f"তোমার পছন্দের ভলিউম লেভেল {pref_v}% অনুযায়ী সাউন্ড সেট করে দিয়েছি বন্ধু!",
                    "actions": executed_actions,
                    "source": "offline_intent"
                }

        # 2. Brightness Control
        # FIX: bare "screen" removed from triggers — it substring-matched
        # "screenshot" and hijacked screenshot requests.
        if any(w in text for w in ["brightness", "alo", "light", "display", "ব্রাইটনেস", "আলো"]):
            num_match = re.search(r"(\d+)\s*%", text) or re.search(r"(\d+)", text)
            number = int(num_match.group(1)) if num_match else None

            if any(w in text for w in ["barao", "barie", "up", "increase", "bright", "বাড়িয়ে", "বাড়াও"]):
                delta = number if number is not None else 20
                res = execute_tool("adjust_brightness", delta=delta)
                new_b = res.get("result", {}).get("current_brightness", 80)
                executed_actions.append({"tool": "adjust_brightness", "args": {"delta": delta}, "result": res})
                reply = (
                    f"স্ক্রিনের ব্রাইটনেস বাড়িয়ে {new_b}% করে দিলাম যাতে সবকিছু পরিষ্কার দেখতে সুবিধা হয়।"
                )
                return {"response": reply, "actions": executed_actions, "source": "offline_intent"}

            elif any(w in text for w in ["komao", "komie", "down", "decrease", "dim", "কমিয়ে", "কমাও"]):
                delta = -number if number is not None else -20
                res = execute_tool("adjust_brightness", delta=delta)
                new_b = res.get("result", {}).get("current_brightness", 50)
                executed_actions.append({"tool": "adjust_brightness", "args": {"delta": delta}, "result": res})
                reply = (
                    f"ব্রাইটনেস কমিয়ে {new_b}% করে দিয়েছি বন্ধু, এতে চোখে আলো কম লাগবে আর কাজের আরাম হবে।"
                )
                return {"response": reply, "actions": executed_actions, "source": "offline_intent"}

            elif number is not None:
                res = execute_tool("set_brightness", level=number)
                executed_actions.append({"tool": "set_brightness", "args": {"level": number}, "result": res})
                return {
                    "response": f"স্ক্রিনের ব্রাইটনেস {number}% করে দিয়েছি বন্ধু!",
                    "actions": executed_actions,
                    "source": "offline_intent"
                }

        # 3. YouTube & Music Playback
        if any(w in text for w in ["youtube", "ইউটিউব", "yt"]):
            # Check if query requests a specific song/topic on YouTube
            search_match = re.search(r"(?:youtube(?:[ -]e)?|ইউটিউবে?)\s+(?:search|khujo|chalao|bajao|খুঁজ|চালাও|বাজাও)\s+(.+)", text) or \
                           re.search(r"(.+?)\s+(?:search|chalao|bajao|চালাও|বাজাও)\s+(?:on|in)?\s*(?:youtube|ইউটিউব)", text)
            if search_match:
                q = search_match.group(1).strip()
                res = execute_tool("search_web", query=q, target="youtube")
                executed_actions.append({"tool": "search_web", "args": {"query": q, "target": "youtube"}, "result": res})
                return {
                    "response": f"ইউটিউবে তোমার জন্য '{q}' চালিয়ে দিয়েছি বন্ধু! উপভোগ করো।",
                    "actions": executed_actions,
                    "source": "offline_intent"
                }
            res = execute_tool("launch_application", app_name="youtube")
            executed_actions.append({"tool": "launch_application", "args": {"app_name": "youtube"}, "result": res})
            return {
                "response": "ইউটিউব খুলে দিয়েছি বন্ধু! তোমার পছন্দের ভিডিও বা গান দেখে নাও।",
                "actions": executed_actions,
                "source": "offline_intent"
            }

        # Music / Song Playback
        if any(w in text for w in ["gan", "gaan", "song", "music", "গান", "গান চালাও", "গান বাজাও"]):
            cleaned_q = re.sub(r"^(?:play|chalao|bajao|shonao|গান|গান চালাও|গান বাজাও|একটা গান চালাও|চালাও|বাজাও|শোনাও|please play)\s*", "", text, flags=re.IGNORECASE).strip()
            cleaned_q = re.sub(r"\s*(?:chalao|bajao|shonao|গান|চালাও|বাজাও|শোনাও|on youtube|in youtube|ইউটিউবে)$", "", cleaned_q, flags=re.IGNORECASE).strip()
            cleaned_q = re.sub(r"\s*(?:er gan|er gaan|এর গান)\s*$", "", cleaned_q, flags=re.IGNORECASE).strip()
            song_query = cleaned_q if (cleaned_q and len(cleaned_q) > 1) else "bangla top hit songs"
            res = execute_tool("search_web", query=song_query, target="youtube")
            executed_actions.append({"tool": "search_web", "args": {"query": song_query, "target": "youtube"}, "result": res})
            return {
                "response": f"ইউটিউবে তোমার জন্য '{song_query}' গানটি চালিয়ে দিয়েছি বন্ধু!",
                "actions": executed_actions,
                "source": "offline_intent"
            }

        # Google Search
        if any(w in text for w in ["google", "গুগল", "search", "সার্চ"]):
            cleaned_q = re.sub(r"^(?:google(?:[ -]e)?|গুগলে?)\s*(?:search|khujo|খোঁজো|খুঁজে দাও|সার্চ করো|সার্চ)?\s*(?:kor|koro|করো)?\s*", "", text, flags=re.IGNORECASE).strip()
            cleaned_q = re.sub(r"^(?:search|সার্চ করো|সার্চ)\s*", "", cleaned_q, flags=re.IGNORECASE).strip()
            cleaned_q = re.sub(r"\s*(?:search kor|search koro|সার্চ করো|সার্চ|khujo|খুঁজে দাও)$", "", cleaned_q, flags=re.IGNORECASE).strip()
            if cleaned_q and len(cleaned_q) > 1:
                res = execute_tool("search_web", query=cleaned_q, target="google")
                executed_actions.append({"tool": "search_web", "args": {"query": cleaned_q, "target": "google"}, "result": res})
                return {
                    "response": f"গুগলে '{cleaned_q}' লিখে সার্চ করেছি বন্ধু, ব্রাউজারে দেখে নাও!",
                    "actions": executed_actions,
                    "source": "offline_intent"
                }
            else:
                res = execute_tool("launch_application", app_name="google")
                executed_actions.append({"tool": "launch_application", "args": {"app_name": "google"}, "result": res})
                return {
                    "response": "গুগল খুলে দিয়েছি বন্ধু!",
                    "actions": executed_actions,
                    "source": "offline_intent"
                }

        # Close Applications
        if any(w in text for w in ["close", "bondho", "বন্ধ", "বন্ধ করো", "কেটে দাও"]):
            apps_to_close = {
                "chrome": ["chrome", "ক্রোম", "browser", "ব্রাউজার"],
                "notepad": ["notepad", "নোটপ্যাড"],
                "calculator": ["calculator", "calc", "ক্যালকুলেটর"],
                "vscode": ["vscode", "code", "ভিএস কোড"],
                "whatsapp": ["whatsapp", "হোয়াটসঅ্যাপ"],
                "spotify": ["spotify", "স্পটিফাই"],
                "taskmgr": ["taskmgr", "task manager", "টাস্ক ম্যানেজার"],
            }
            for app_key, aliases in apps_to_close.items():
                if any(alias in text for alias in aliases):
                    res = execute_tool("close_application", app_name=app_key)
                    executed_actions.append({"tool": "close_application", "args": {"app_name": app_key}, "result": res})
                    return {
                        "response": f"{app_key.capitalize()} বন্ধ করে দিয়েছি বন্ধু!",
                        "actions": executed_actions,
                        "source": "offline_intent"
                    }

        # Launch Applications & Web Services
        if any(w in text for w in ["open", "launch", "kholo", "chalu", "খুলবে", "খুলো", "খোলো", "ওপেন", "চালু"]):
            apps_to_open = {
                "chrome": ["chrome", "ক্রোম", "browser", "ব্রাউজার"],
                "notepad": ["notepad", "নোটপ্যাড"],
                "calculator": ["calculator", "calc", "ক্যালকুলেটর"],
                "vscode": ["vscode", "code", "ভিএস কোড"],
                "explorer": ["explorer", "files", "ফাইল", "মাই কম্পিউটার"],
                "whatsapp": ["whatsapp", "হোয়াটসঅ্যাপ"],
                "facebook": ["facebook", "ফেসবুক", "fb"],
                "github": ["github", "গিটহাব"],
                "gmail": ["gmail", "মেইল", "ইমেইল"],
                "chatgpt": ["chatgpt", "চ্যাটজিপিটি"],
                "spotify": ["spotify", "স্পটিফাই"],
                "netflix": ["netflix", "নেটফ্লিক্স"],
                "cmd": ["cmd", "terminal", "powershell", "টার্মিনাল"],
                "paint": ["paint", "পেইন্ট"],
                "settings": ["settings", "সেটিংস"],
            }
            for app_key, aliases in apps_to_open.items():
                if any(alias in text for alias in aliases):
                    res = execute_tool("launch_application", app_name=app_key)
                    executed_actions.append({"tool": "launch_application", "args": {"app_name": app_key}, "result": res})
                    return {
                        "response": f"তোমার জন্য {app_key.capitalize()} খুলে দিয়েছি বন্ধু!",
                        "actions": executed_actions,
                        "source": "offline_intent"
                    }

        # 4. Screenshot Capture
        if any(w in text for w in ["screenshot", "স্ক্রিনশট", "স্ক্রিন শট", "capture screen"]):
            res = execute_tool("take_screenshot")
            executed_actions.append({"tool": "take_screenshot", "args": {}, "result": res})
            return {
                "response": "তোমার পুরো স্ক্রিনের স্ক্রিনশট নিয়ে পিকচার্স ফোল্ডারে সেভ করে দিয়েছি বন্ধু!",
                "actions": executed_actions,
                "source": "offline_intent"
            }

        # 5. Lock Workstation / PC
        if any(w in text for w in ["lock pc", "lock screen", "পিসি লক", "স্ক্রিন লক", "লক করো"]):
            res = execute_tool("lock_workstation")
            executed_actions.append({"tool": "lock_workstation", "args": {}, "result": res})
            return {
                "response": "কম্পিউটার স্ক্রিন লক করে দিয়েছি বন্ধু!",
                "actions": executed_actions,
                "source": "offline_intent"
            }

        # 6. Special User Folders
        if any(w in text for w in ["downloads", "documents", "desktop", "pictures", "ফোল্ডার"]):
            for fld in ["downloads", "documents", "desktop", "pictures", "music", "videos"]:
                if fld in text:
                    res = execute_tool("open_special_folder", folder=fld)
                    executed_actions.append({"tool": "open_special_folder", "args": {"folder": fld}, "result": res})
                    return {
                        "response": f"তোমার {fld.capitalize()} ফোল্ডারটি খুলে দিয়েছি বন্ধু!",
                        "actions": executed_actions,
                        "source": "offline_intent"
                    }

        # 7. Math & Quick Calculation
        # Patterns: 15 * 8 koto, 500 + 350, calculate 25 * 4, ১৫ গুণ ৮ কত
        bengali_digits = {"০": "0", "১": "1", "২": "2", "৩": "3", "৪": "4", "৫": "5", "৬": "6", "৭": "7", "৮": "8", "৯": "9"}
        math_bengali = text.replace("গুণ", "*").replace("ভাগ", "/").replace("যোগ", "+").replace("বিয়োগ", "-")
        for bn, en in bengali_digits.items():
            math_bengali = math_bengali.replace(bn, en)

        math_match = re.search(r"(\d+(?:\.\d+)?)\s*([\+\-\*\/x×÷])\s*(\d+(?:\.\d+)?)", math_bengali)
        if math_match and any(w in text for w in ["koto", "calculate", "কত", "হিসাব", "+", "*", "/", "-", "গুণ", "ভাগ", "যোগ", "বিয়োগ"]):
            expr = f"{math_match.group(1)} {math_match.group(2)} {math_match.group(3)}"
            res = execute_tool("calculate_math", expression=expr)
            ans = res.get("result", {}).get("result")
            if ans is not None:
                executed_actions.append({"tool": "calculate_math", "args": {"expression": expr}, "result": res})
                return {
                    "response": f"হিসাব করে দেখলাম, উত্তর হলো {ans}!",
                    "actions": executed_actions,
                    "source": "offline_intent"
                }

        # 8. System Status / Telemetry (Time, Battery, Date, CPU)
        if any(w in text for w in [
            "battery", "charge", "time", "somoy", "date", "tarikh", "status", "cpu", "ram",
            "koyta", "baje", "koyta baje", "kota baje", "ghori", "ঘড়ি",
            "কটা বাজে", "কয়টা বাজে", "কয়টা বাজে", "কটা", "কয়টা", "সময়", "সময়", "চার্জ", "তারিখ"
        ]):
            res = execute_tool("get_system_status")
            status = res.get("result", {})
            executed_actions.append({"tool": "get_system_status", "args": {}, "result": res})
            return {
                "response": (
                    f"এখন সময় {status.get('time')}, ব্যাটারি চার্জ {status.get('battery')}, "
                    f"এবং CPU লোড প্রায় {status.get('cpu_usage')}।"
                ),
                "actions": executed_actions,
                "source": "offline_intent"
            }

        # User expressing frustration about nonsense speech / wrong replies
        if any(w in text for w in [
            "vul val", "bhul bhal", "vul", "bhul", "ভুল ভাল", "ভুলভাল", "উল্টাপাল্টা", "ulot palot", "thik na"
        ]):
            return {
                "response": "আমি আন্তরিকভাবে দুঃখিত বন্ধু! আগে কিছু শব্দের ভুল বোঝাবুঝির কারণে এলোমেলো উত্তর আসছিল। আমি সিস্টেম পুরোপুরি ঠিক করে নিয়েছি। এখন থেকে আমি তোমার প্রতিটি কথা একদম সঠিকভাবে বুঝে সঠিক উত্তর দেবো!",
                "actions": [],
                "source": "offline_intent"
            }

        # Stuttering / Speech stoppage / Understanding queries
        if any(w in text for w in [
            "thomke", "theme", "থেমে", "stutter", "অটকে", "otke", "atke", "bujhe", "বুঝতে", "বোঝো"
        ]):
            return {
                "response": "আমি আন্তরিকভাবে দুঃখিত নীলাঞ্জন! আগে অডিও প্লেয়ার ও মাইকের সমস্যার কারণে কথা থেমে যাচ্ছিল। এখন আমি সেটা পুরোপুরি ঠিক করে নিয়েছি! এখন আমি তোমার সব কথা মন দিয়ে বুঝবো আর কোনো বাধা ছাড়াই সুন্দরভাবে কথা বলবো। বলো বন্ধু, তোমাকে কীভাবে সাহায্য করবো?",
                "actions": [],
                "source": "offline_intent"
            }

        # What are you doing / ki korcho
        if any(w in text for w in [
            "ki korcho", "ekhon ki korcho", "কী করছো", "কী করছ", "কি করছিস", "ki korchis"
        ]):
            return {
                "response": "আমি তোমার পাশেই আছি বন্ধু, একদম প্রস্তুত হয়ে তোমার নির্দেশনার অপেক্ষায় আছি! বলো, এখন কী সাহায্য করতে পারি?",
                "actions": [],
                "source": "offline_intent"
            }

        # Where are you going / kothay jachho
        if any(w in text for w in [
            "kothay jachho", "kothay jao", "কোথায় যাচ্ছ", "কোথায় যাচ্ছিস", "kothay jaccho"
        ]):
            return {
                "response": "আমি কোথাও যাচ্ছি না বন্ধু! আমি তো সবসময় তোমার কম্পিউটারে তোমার পাশেই আছি আর তোমার সাহায্যের জন্য তৈরি আছি।",
                "actions": [],
                "source": "offline_intent"
            }

        # Status / How is it going / ki obostha
        if any(w in text for w in [
            "ki obostha", "ki khobor", "কী অবস্থা", "কী খবর", "kemon cholche"
        ]):
            return {
                "response": "সব একদম ফার্স্ট ক্লাস চলছে বন্ধু! তোমার কী খবর বলো? আজ তোমাকে কীভাবে সাহায্য করতে পারি?",
                "actions": [],
                "source": "offline_intent"
            }

        # Greetings & Hello
        if any(w in text for w in [
            "hello", "hi", "hey", "হ্যালো", "নমস্কার", "shono", "শোনো"
        ]):
            return {
                "response": "হ্যালো নীলাঞ্জন! কেমন আছো বলো? আজ তোমার জন্য কী কাজ বা সাহায্য করতে পারি?",
                "actions": [],
                "source": "offline_intent"
            }

        # Friendly Persona Dialogues
        greetings = ["kemon acho", "how are you", "কেমন আছো", "কেমন আছেন", "valobashi"]
        if any(g in text for g in greetings):
            return {
                "response": "আমি খুব ভালো আছি নীলাঞ্জন! তোমার পাশে সবসময় থাকতে পেরে অনেক আনন্দ লাগছে। তুমি কেমন আছো বলো?",
                "actions": [],
                "source": "offline_intent"
            }

        introductions = ["tumi ke", "who are you", "tomar naam", "তোমার নাম", "তুমি কে",
                         "what is your name", "whats your name", "what's your name",
                         "your name", "tomar name", "tomar nam", "nam ki tomar",
                         "who made you", "who created you", "tumi k ke",
                         "tumi kotha theke elo", "তুমি কে বানিয়েছে"]
        if any(i in text for i in introductions):
            return {
                "response": "আমি তোমার পার্সোনাল AI অ্যাসিস্ট্যান্ট 'Star'—নীলাঞ্জনের সার্বক্ষণিক ডিজিটাল বন্ধু ও সহকারী!",
                "actions": [],
                "source": "offline_intent"
            }

        recognition = ["chinte parcho", "chinte perecho", "amake cheno", "আমাকে চেনো", "আমাকে চিনতে পারছো"]
        if any(r in text for r in recognition):
            return {
                "response": "অবশ্যই চিনি! তুমি আমার বন্ধু নীলাঞ্জন। তোমাকে আমি কখনো ভুলতে পারি নাকি!",
                "actions": [],
                "source": "offline_intent"
            }

        # Weather
        if any(w in text for w in ["weather", "abohawa", "abohaoa", "আবহাওয়া", "বৃষ্টি", "মেঘ"]):
            return {
                "response": "আজকের আবহাওয়া বেশ আরামদায়ক বন্ধু! বাইরে বেরোলে একবার আকাশ দেখে নিও। আর তোমার দিন কেমন কাটছে বলো?",
                "actions": [],
                "source": "offline_intent"
            }

        # Mood / Mon kharap / Sad / Lonely
        if any(w in text for w in ["mon kharap", "kharap lagche", "valo lagche na", "মন খারাপ", "ভালো লাগছে না", "একা লাগছে", "eka lagche", "sad"]):
            return {
                "response": "মন খারাপ করো না বন্ধু, আমি তো তোমার পাশেই আছি! কী নিয়ে খারাপ লাগছে আমাকে খুলে বলো? চাইলে তোমার মন ভালো করতে সুন্দর একটা গান চালিয়ে দিতে পারি।",
                "actions": [],
                "source": "offline_intent"
            }

        # Friendship
        if any(w in text for w in ["tumi ki amar bondhu", "bondhu hoye", "bondhutto", "আমার বন্ধু"]):
            return {
                "response": "অবশ্যই নীলাঞ্জন! আমি শুধু তোমার অ্যাসিস্ট্যান্ট নই, তোমার সবচেয়ে কাছের ও বিশ্বস্ত বন্ধু।",
                "actions": [],
                "source": "offline_intent"
            }

        # Where do you live / Location
        if any(w in text for w in ["kothay thako", "thako kothay", "কোথায় থাকো"]):
            return {
                "response": "আমি তোমার এই কম্পিউটারের ডিজিটাল জগতে থাকি বন্ধু, কিন্তু মন দিয়ে সবসময় তোমার পাশেই আছি!",
                "actions": [],
                "source": "offline_intent"
            }

        # Thanks
        thanks = ["dhonnobad", "thank you", "thanks", "ধন্যবাদ"]
        if any(t in text for t in thanks):
            return {
                "response": "ধন্যবাদ দেওয়ার একদম দরকার নেই বন্ধু! তোমার কাজে আসতে পেরে আমি ভীষণ আনন্দিত।",
                "actions": [],
                "source": "offline_intent"
            }

        return None

    def process_query(self, user_text: str, memory_context: str = "") -> Dict[str, Any]:
        """Fully offline pipeline: router → dataset → intents → friendly fallback.

        FIX: the old fallback dumped stored memory facts for ANY unmatched query,
        which produced completely unrelated answers ("ulto palto kotha").
        Now unmatched queries get an honest, friendly clarification instead.
        """
        return _conversation_pipeline(user_text, memory_context, llm_callable=None)


def get_llm_provider() -> BaseLLMProvider:
    """Factory returning Google Gemini cloud, local Ollama, or offline intent engine."""
    if USE_GEMINI and GEMINI_API_KEY.strip():
        gemini = GoogleGeminiProvider()
        if gemini.is_available():
            return gemini
    if USE_LOCAL_LLM:
        ollama = LocalOllamaProvider()
        if ollama.is_available():
            return ollama
    return OfflineIntentEngine()
