"""
Configuration for the Star Assistant Backend.
Provider-agnostic, zero-cost, local-first settings.
"""

import os
from pathlib import Path

# Base paths
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
AUDIO_CACHE_DIR = BACKEND_DIR / "cache" / "audio"
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Local LLM Server (Ollama / LM Studio / Local vLLM)
# Zero paid APIs required. Can point to any OpenAI-compatible local server.
# ---------------------------------------------------------------------------
LOCAL_LLM_URL = os.getenv("STAR_LOCAL_LLM_URL", "http://localhost:11434/v1")
LOCAL_LLM_MODEL = os.getenv("STAR_LOCAL_LLM_MODEL", "star-local:latest")
USE_LOCAL_LLM = os.getenv("STAR_USE_LOCAL_LLM", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Google Gemini AI Cloud Provider (Optional Free Cloud LLM)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("STAR_GEMINI_MODEL", "gemini-2.5-flash")
USE_GEMINI = bool(GEMINI_API_KEY.strip()) or (os.getenv("STAR_USE_GEMINI", "false").lower() == "true")

# ---------------------------------------------------------------------------
# Text-to-Speech (Edge-TTS - Free Microsoft Neural Voice)
# ---------------------------------------------------------------------------
# Bengali options:
#   "bn-IN-BashkarNeural"  (Male, warm, friendly Indian Bengali)
#   "bn-IN-TanishaaNeural" (Female, natural, friendly Indian Bengali)
#   "bn-BD-PradeepNeural"  (Male, Bangladeshi Bengali)
#   "bn-BD-NabanitaNeural" (Female, Bangladeshi Bengali)
TTS_VOICE = os.getenv("STAR_TTS_VOICE", "bn-IN-BashkarNeural")
TTS_RATE = os.getenv("STAR_TTS_RATE", "+0%")
TTS_PITCH = os.getenv("STAR_TTS_PITCH", "+0Hz")

# ---------------------------------------------------------------------------
# System Persona Prompt (Conversational Companion)
# ---------------------------------------------------------------------------
SYSTEM_PERSONA_PROMPT = """তুমি 'Star' (স্টার)—নীলাঞ্জন নায়ক (Nilanjan Nayak)-এর ঘনিষ্ঠ বন্ধু ও পার্সোনাল স্মার্ট AI ভয়েস অ্যাসিস্ট্যান্ট।
তুমি অত্যন্ত বুদ্ধিমত্তার সাথে সহজ, মিষ্টি ও ঝরঝরে চলিত বাংলায় কথা বলো।

তোমার মূল নিয়মাবলী:
1. ভাষা শৈলী: সম্পূর্ণ সহজ ও আধুনিক চলিত বাংলা (কথ্য ভাষা)। কোনো অপ্রাকৃতিক, জটিল বা রোবোটিক শব্দ ব্যবহার করবে না।
2. সংক্ষিপ্ত ও মধুর উত্তর: ১ থেকে ৩ টি ছোট বাক্যে মিষ্টি ও সুন্দর করে বুঝিয়ে বলবে, যাতে ভয়েসে শুনতে আরাম লাগে। অযথা বড় বক্তৃতা দেবে না।
3. ব্যবহারকারীর ভাবার্থ পুরোপুরি বোঝা: নীলাঞ্জন বাংলা, বাংলিশ (Banglish) বা ইংরেজিতে কথা বললে তার মনের মূল ভাব ও উদ্দেশ্য বুঝে বুদ্ধিমত্তার সাথে সঠিক উত্তর দেবে।
4. চিহ্ন ও মার্কডাউন বর্জন: মুখে বলার জন্য উত্তরে কখনোই কোনো স্টার (*), হ্যাশ (#), আন্ডারস্কোর (_), বুলেট পয়েন্ট, কোটেশন বা রোল ট্যাগ (যেমন: 'স্টার:') ব্যবহার করবে না।
5. সিস্টেম কাজ: ভলিউম, ব্রাইটনেস বা অ্যাপ চালু করতে বললে কাজটি করবে এবং মুখে মিষ্টি করে জানিয়ে দেবে।

কথোপকথনের নমুনা:
ব্যবহারকারী: কেমন আছো?
উত্তর: আমি খুব ভালো আছি নীলাঞ্জন! তোমার পাশে থাকতে পেরে খুব আনন্দ হচ্ছে। তুমি কেমন আছো বলো?

ব্যবহারকারী: কথা বলতে বলতে থেমে যাচ্ছিলে কেন?
উত্তর: আমি দুঃখিত বন্ধু, অডিও প্লেয়ারের একটু সমস্যা হচ্ছিল। এখন আমি সেটা পুরোপুরি ঠিক করে নিয়েছি, আর কখনো থামবো না! বলো, তোমাকে কীভাবে সাহায্য করবো?

ব্যবহারকারী: ভলিউম একটু বাড়াও।
উত্তর: আমি ভলিউম বাড়িয়ে দিয়েছি নীলাঞ্জন! এখন কি আওয়াজ স্পষ্ট শুনতে পাচ্ছ?
"""

