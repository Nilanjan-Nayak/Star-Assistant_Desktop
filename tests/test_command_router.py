"""
Regression tests for the Star Assistant NLU command router + pipeline.

These lock in the fixes for the reported problems:
  • "ami ja bolchi se ulta palta kotha bole"  → wrong canned dataset replies
  • "kono kaj korte parche na"                → keyword gaps / no action
  • dataset samples executing THEIR actions instead of the user's
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from Backend.nlu import route_command


# ─── Volume ──────────────────────────────────────────────────────────────────

def test_volume_up_english():
    r = route_command("volume barao")
    assert r and r["actions"][0]["tool"] == "adjust_volume"
    assert r["actions"][0]["args"]["delta"] > 0


def test_volume_down_bengali_script():
    r = route_command("ভলিউম কমাও")
    assert r and r["actions"][0]["tool"] == "adjust_volume"
    assert r["actions"][0]["args"]["delta"] < 0


def test_volume_set_level():
    r = route_command("volume 30 koro")
    assert r and r["actions"][0]["tool"] == "set_volume"
    assert r["actions"][0]["args"]["level"] == 30


def test_volume_bengali_digits():
    r = route_command("ভলিউম ৫০ করো")
    assert r and r["actions"][0]["tool"] == "set_volume"
    assert r["actions"][0]["args"]["level"] == 50


def test_volume_question_is_not_a_command():
    """'volume komanor jonno ki korbo' is a QUESTION — must not act."""
    assert route_command("volume komanor jonno ki korbo") is None


# ─── Apps ────────────────────────────────────────────────────────────────────
# On non-Windows CI the app-launcher shells out (os.startfile / start) which
# produces harmless Popen GC noise — ignore it, we only assert the routing.

@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_open_app_banglish_spelling():
    """'khulo' (previously unmatched vs 'kholo') must open the app now."""
    r = route_command("chrome khulo")
    assert r and r["actions"][0]["tool"] == "launch_application"
    assert r["actions"][0]["args"]["app_name"] == "chrome"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_close_app():
    r = route_command("chrome bondho koro")
    assert r and r["actions"][0]["tool"] == "close_application"


# ─── Screenshot (was hijacked by the brightness 'screen' substring) ─────────

def test_screenshot_not_hijacked():
    r = route_command("screenshot nao")
    assert r and r["actions"][0]["tool"] == "take_screenshot"


# ─── Media / search ──────────────────────────────────────────────────────────

def test_song_play_searches_youtube_with_clean_query():
    r = route_command("amar jonno ekta valo gaan chalao")
    assert r and r["actions"][0]["tool"] == "search_web"
    assert r["actions"][0]["args"]["target"] == "youtube"
    q = r["actions"][0]["args"]["query"]
    for filler in ("amar jonno", "ekta", "valo"):
        assert filler not in q.lower()


def test_youtube_named_search():
    r = route_command("youtube e tom math gon chalao")
    assert r and r["actions"][0]["tool"] == "search_web"
    assert r["actions"][0]["args"]["query"] == "tom math"


def test_google_search():
    r = route_command("google e kobita search koro")
    assert r and r["actions"][0]["tool"] == "search_web"
    assert r["actions"][0]["args"]["target"] == "google"
    assert r["actions"][0]["args"]["query"] == "kobita"


# ─── Memory ──────────────────────────────────────────────────────────────────

def test_remember_fact():
    r = route_command("mone rakho amar pochondo coffee")
    assert r and "coffee" in r["response"]


# ─── Chat must NOT be hijacked by the router ─────────────────────────────────

@pytest.mark.parametrize("text", [
    "kemon acho",
    "ki korchis",
    "ami jani na toma ke boli",
    "amar mon kharap",
    "tumi ke",
])
def test_chat_falls_through(text):
    assert route_command(text) is None


# ─── Math ────────────────────────────────────────────────────────────────────

def test_math_banglish_gun():
    r = route_command("15 gun 8 koto")
    assert r and "120" in r["response"]


# ─── Multi-step goals route to the autonomous agent ─────────────────────────

def test_multistep_goal_hits_agent():
    r = route_command("screenshot nao tarpor volume 20 koro")
    assert r and r["actions"][0]["tool"] == "execute_autonomous_goal"


def test_single_action_does_not_hit_agent():
    r = route_command("volume 20 koro")
    assert r and r["actions"][0]["tool"] == "set_volume"


def test_agent_talk_is_not_a_goal():
    assert route_command("ami agent banate chai") is None


# ─── Bengali normalization sanity ────────────────────────────────────────────

def test_bengali_matras_survive_normalization():
    from Backend.nlu.command_router import _normalize
    assert _normalize("ভলিউম") == "ভলিউম"


# ─── Dataset engine never executes sample actions on fuzzy matches ──────────

def test_dataset_fuzzy_match_has_no_side_effects():
    from Backend.llm.knowledge import get_companion_engine
    engine = get_companion_engine()
    m = engine.find_match("amar ekta kotha chilo", min_confidence=0.84)
    if m:  # only if something fuzzy-matched
        assert not m.get("actions"), "fuzzy dataset match must not execute actions"


# ─── Memory dedup (recall flooding fix) ─────────────────────────────────────

def test_memory_remember_is_idempotent(tmp_path):
    """Writing the same keyless fact twice must create ONE row."""
    from agent.core.enums import MemoryKind
    from agent.memory.store import MemoryStore
    store = MemoryStore(tmp_path / "mem.db", owner="tester")
    try:
        store.remember("amar pochondo coffee", kind=MemoryKind.FACT)
        store.remember("amar pochondo coffee", kind=MemoryKind.FACT)
        rows = store.recall_relevant("amar pochondo coffee", top_k=10)
        matches = [r for r in rows if r.text == "amar pochondo coffee"]
        assert len(matches) == 1
    finally:
        store.close()


def test_memory_recall_dedupes_and_keeps_distinct_facts(tmp_path):
    """Distinct facts must all surface — no single fact may flood top_k."""
    from agent.core.enums import MemoryKind
    from agent.memory.store import MemoryStore
    store = MemoryStore(tmp_path / "mem.db", owner="tester")
    try:
        store.remember("amar pochondo coffee", kind=MemoryKind.FACT)
        store.remember("amar pochondo browni", kind=MemoryKind.FACT)
        recs = store.recall_relevant("amar ki pochondo", top_k=5)
        texts = {r.text for r in recs}
        assert "amar pochondo coffee" in texts
        assert "amar pochondo browni" in texts
    finally:
        store.close()


# ─── Status questions guard ─────────────────────────────────────────────────

def test_status_question_guard():
    """'battery keno kharap korche' is a question, not a status request."""
    assert route_command("battery keno kharap korche") is None
    assert route_command("ram kivabe barabo") is None
