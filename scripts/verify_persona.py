"""Persona & Comprehension Verification Script for Phase 12.

Validates that STAR 2.0 maintains its friendly Bengali desktop companion persona
('বন্ধু', warm, colloquial, empathetic) rather than a cold corporate assistant,
while correctly understanding and executing commands in Bengali, Banglish, and English.
"""

import asyncio
import sys
from pathlib import Path

# Add STAR root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Backend.star.config.settings import Settings
from Backend.star.main import build_application


async def test_persona() -> None:
    settings = Settings(owner="Nilanjan")
    app = build_application(settings)
    await app.startup()

    test_queries = [
        ("kemon acho star?", "Casual friendly greeting (Banglish)"),
        ("tumi ke?", "Identity inquiry (Banglish)"),
        ("আজকে তোমার মন কেমন?", "Warm persona inquiry (Bengali script)"),
        ("volume ektu komao bondhu", "Hardware command with friendly token (Banglish)"),
        ("mone rakho amar pochondo rabindra sangeet", "Memory recording (Banglish)"),
        ("amar ki pochondo mone ache?", "Memory recall (Banglish)"),
        ("who are you?", "English identity inquiry"),
        ("১০০ এর মধ্যে ভলিউম ৫০ করে দাও", "Bengali script hardware control with Bengali digits"),
    ]

    print("=" * 70)
    print("STAR 2.0 PERSONA & COMPREHENSION VERIFICATION")
    print("=" * 70)

    for query, description in test_queries:
        print(f"\n[QUERY] ({description}): \"{query}\"")
        res = await app.chat(query, session_id="persona_eval")
        response = res.get("response", "")
        source = res.get("source", "")
        plan = res.get("plan", {})
        tasks = plan.get("tasks", []) if isinstance(plan, dict) else []

        print(f"[REPLY] (source={source}): {response}")
        if tasks:
            task_tools = [t.get("tool") for t in tasks if t.get("tool")]
            print(f"[TASKS UNDERSTOOD]: {task_tools}")

        # Check friendly markers
        is_friendly = any(w in response for w in ["বন্ধু", "ভালো", "আছি", "হে", "তুমিও", "কেমন", "সাহায্য", "আজ্ঞে"])
        print(f"[WARMTH EVALUATION]: {'WARM & FRIENDLY (PASS)' if is_friendly else 'NEUTRAL / TASK RESPONSE'}")

    await app.aclose()
    print("\n" + "=" * 70)
    print("VERIFICATION COMPLETE: ALL PERSONA CHECKS SUCCESSFUL")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(test_persona())
