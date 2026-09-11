import sys
sys.stdout.reconfigure(encoding='utf-8')
from Backend.llm.knowledge import get_companion_engine

engine = get_companion_engine()

test_queries = [
    "tumi ekhon ki korcho",
    "bhai kothay jachho",
    "amar ekta kotha shono",
    "tumi ki amake chinte parcho",
    "ki obstha",
    "hello",
    "dark mode on kor",
    "amar matha betha korche",
    "chrome kholo",
    "sound ta ektu barie de"
]

print("=== Testing with min_confidence=0.82 ===")
for q in test_queries:
    m = engine.find_match(q, min_confidence=0.82)
    if m:
        print(f"User: '{q}'")
        print(f"  -> Matched ({m['source']}): {m['response'][:50]}...")
    else:
        print(f"User: '{q}' -> No dataset match (will go to LLM cleanly)")
