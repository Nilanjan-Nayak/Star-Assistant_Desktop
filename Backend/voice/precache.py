"""
Pre-caches audio files for all dataset responses into local disk.
Makes voice responses instant (0ms TTS latency).
"""

import json
import asyncio
import hashlib
from pathlib import Path

from Backend.config import PROJECT_ROOT
from Backend.voice.tts import clean_for_speech, get_voice_engine

async def precache_all():
    engine = get_voice_engine()
    dataset_path = PROJECT_ROOT / "data" / "train_all.jsonl"
    if not dataset_path.exists():
        return

    import edge_tts

    answers = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for l in f:
            if l.strip():
                try:
                    d = json.loads(l)
                    ans = d["messages"][1]["content"].strip()
                    if ans and ans not in answers:
                        answers.append(ans)
                except Exception:
                    pass

    print(f"[PreCache] Found {len(answers)} unique responses to check/cache.")
    count = 0
    for idx, ans in enumerate(answers):
        spoken = clean_for_speech(ans) or ans
        has_bng = any('\u0980' <= ch <= '\u09ff' for ch in spoken)
        voice = engine.voice if has_bng else "en-IN-NeerjaExpressiveNeural"
        key = f"{spoken}_{voice}_{engine.rate}_{engine.pitch}".encode("utf-8")
        h = hashlib.md5(key).hexdigest()
        out = engine.cache_dir / f"tts_{h}.mp3"

        if out.exists() and out.stat().st_size > 0:
            continue

        try:
            comm = edge_tts.Communicate(text=spoken, voice=voice, rate=engine.rate, pitch=engine.pitch)
            await comm.save(str(out))
            count += 1
            if count % 10 == 0:
                print(f"[PreCache] Cached {count} new files... ({idx+1}/{len(answers)})")
        except Exception as e:
            print(f"[PreCache] Warning on '{spoken[:25]}...': {e}")
            await asyncio.sleep(0.5)

    print(f"[PreCache] Completed! Pre-cached {count} audio files into disk cache.")

if __name__ == "__main__":
    asyncio.run(precache_all())
