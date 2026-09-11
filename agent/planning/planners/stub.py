from __future__ import annotations

from agent.core.ids import parse_skill_name
from agent.memory.store import MemoryStore
from agent.planning.memory import EpisodicMemory
from agent.planning.step import PlanStep
from agent.world.model import WorldModel

import re
from typing import Match

_BN_DIGIT_TRANS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_NUMBER_IN_GOAL: "re.Pattern[str]" = re.compile(r"(\d+)\s*%?")


def _goal_number(goal: str) -> "Match[str] | None":
    return _NUMBER_IN_GOAL.search(goal.lower().translate(_BN_DIGIT_TRANS))



class StubPlanner:
    """Deterministic keyword planner for tests / no-LLM usage.

    When a ``MemoryStore`` is attached, default params (volume, brightness,
    youtube query, launch app) come from the latest preference rather than
    hardcoded numbers — this is the 'brain grows over time' loop.
    """

    def __init__(self, semantic: MemoryStore | None = None) -> None:
        self.semantic = semantic

    async def next_step(
        self,
        goal: str,
        history: list[PlanStep],
        world: WorldModel,
        memory: EpisodicMemory,
    ) -> PlanStep | None:
        _ = (world, memory)
        gl = goal.lower()
        done = {step.skill for step in history if step.success}
        store = self.semantic

        youtube = parse_skill_name("youtube")
        volume = parse_skill_name("volume")
        brightness = parse_skill_name("brightness")
        screenshot = parse_skill_name("screenshot")
        see = parse_skill_name("see")
        launch = parse_skill_name("launch")

        if any(key in gl for key in ("youtube", "video", "play")) and youtube not in done:
            default_q = gl.split("for", 1)[-1].strip() if "for" in gl else "lo-fi beats"
            query = store.preferred_str("youtube.query", default_q) if store else default_q
            if "for" in gl:
                query = default_q
            return PlanStep(
                thought="Delegating to youtube skill",
                skill=youtube,
                params={"query": query},
            )
        if "volume" in gl and volume not in done:
            level = store.preferred_int("volume.level", 70) if store else 70
            # An explicit number in the goal beats the remembered preference
            # ("set volume to 40", "volume 30 koro", Bengali digits too).
            m = _goal_number(gl)
            if m:
                level = max(0, min(100, int(m.group(1))))
            return PlanStep(
                thought=f"Setting volume to level {level}",
                skill=volume,
                params={"level": level},
            )
        if "brightness" in gl and brightness not in done:
            level = store.preferred_int("brightness.level", 80) if store else 80
            m = _goal_number(gl)
            if m:
                level = max(0, min(100, int(m.group(1))))
            return PlanStep(
                thought=f"Setting brightness to level {level}",
                skill=brightness,
                params={"level": level},
            )
        if "screenshot" in gl and screenshot not in done:
            return PlanStep(
                thought="Taking screenshot",
                skill=screenshot,
                params={"path": "capture.png"},
            )
        if (
            any(
                key in gl
                for key in (
                    "see",
                    "look",
                    "ocr",
                    "dakho",
                    "dekho",
                    "dekh",
                    "what's on",
                    "what is on",
                    "on screen",
                    "screen e",
                    "পর্দা",
                )
            )
            and see not in done
        ):
            query = "*"
            for prefix in ("for ", "find ", "read "):
                if prefix in gl:
                    query = gl.split(prefix, 1)[-1].strip() or "*"
                    break
            return PlanStep(
                thought="Reading the screen with OCR",
                skill=see,
                params={"query": query, "save_path": "see.png"},
            )
        if any(key in gl for key in ("open", "launch", "start")) and launch not in done:
            # Best-effort last token as app name; the skill validates it.
            token = gl.replace("open", "").replace("launch", "").replace("start", "").strip()
            app = token.split()[0] if token else "notepad"
            if store is not None and app == "notepad":
                app = store.preferred_str("launch.app", app)
            return PlanStep(thought="Launching application", skill=launch, params={"app": app})
        return None
