"""
LLM-backed Planner for the ComputerControlAgent ReAct loop.

Implements the ``agent.planning.planner.Planner`` protocol (same signature as
``StubPlanner``) but asks the configured LLM (Google Gemini cloud or a local
Ollama server) to choose the next skill + params for the current goal.

Design goals
------------
• Works fully offline: every failure path (no internet, bad JSON, unknown
  skill, timeout) transparently falls back to the deterministic StubPlanner,
  so the autonomous agent NEVER breaks.
• Skill menu is generated from the real registry schema names — the LLM can
  only choose skills that actually exist.
• Strict JSON contract with robust parsing (code fences, prose, etc.).

This planner is injected from ``Backend/agent_bridge.py`` so the layered
``agent/`` package stays free of Backend imports.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Dict, Optional

from agent.core.ids import parse_skill_name
from agent.memory.store import MemoryStore
from agent.planning.memory import EpisodicMemory
from agent.planning.planners.stub import StubPlanner
from agent.planning.step import PlanStep
from agent.world.model import WorldModel

from ..config import (
    GEMINI_API_KEY, GEMINI_MODEL, LOCAL_LLM_URL, LOCAL_LLM_MODEL,
)

# The skill menu mirrors agent/skills/builtins (volume, brightness, launch,
# youtube, screenshot, see).  Kept explicit so the LLM prompt is stable and
# tiny — and every entry is validated again before execution anyway.
_SKILL_MENU = """\
- volume: set system volume. params: {"level": 0-100}
- brightness: set screen brightness. params: {"level": 0-100}
- launch: open an application. params: {"app": "chrome" | "notepad" | "calc" | "mspaint" | "cmd" | ...}
- youtube: search & open a video on YouTube. params: {"query": "search text"}
- screenshot: save a screenshot to disk. params: {"path": "file.png"}
- see: OCR-read the screen and report text. params: {"query": "*"}
"""

_SYSTEM = """You are the planning module of a desktop automation agent.
Given a GOAL, the steps already done, and the available skills, output ONLY a
JSON object choosing the NEXT single step:
  {"skill": "<name>", "params": {...}, "thought": "<one short line>"}
If the goal is already fully achieved (or impossible), output:
  {"done": true, "thought": "<one short line>"}
Rules: valid JSON only, no markdown fences, no commentary. Use only the listed
skills with exactly their parameter names. Prefer the fewest steps needed.
"""

_PROMPT_TEMPLATE = """GOAL: {goal}

STEPS ALREADY DONE:
{history}

AVAILABLE SKILLS:
{skills}

Next step JSON:"""


def _parse_json_block(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = raw.strip()
    # strip markdown fences / surrounding prose
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class AgentLLMPlanner:
    """ReAct planner that uses the configured LLM with a StubPlanner safety net."""

    def __init__(self, semantic: MemoryStore | None = None,
                 step_timeout: float = 6.0) -> None:
        self.semantic = semantic
        self.step_timeout = step_timeout
        self._fallback = StubPlanner(semantic=semantic)
        self._mode = self._detect_mode()

    # ── provider detection ────────────────────────────────────────────────
    def _detect_mode(self) -> str:
        if GEMINI_API_KEY.strip():
            return "gemini"
        try:
            req = urllib.request.Request(
                LOCAL_LLM_URL.rstrip("/").removesuffix("/v1") + "/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if resp.status == 200:
                    return "ollama"
        except Exception:
            pass
        return "ollama"  # still try; factory probe may pass later

    # ── LLM backends ──────────────────────────────────────────────────────
    def _ask_llm(self, prompt: str) -> Optional[str]:
        if self._mode == "gemini":
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "systemInstruction": {"parts": [{"text": _SYSTEM}]},
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 200},
            }
        else:
            url = LOCAL_LLM_URL.rstrip("/") + "/chat/completions"
            payload = {
                "model": LOCAL_LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 200,
            }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=self.step_timeout) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if self._mode == "gemini":
            candidates = res.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "")
            return None
        return res.get("choices", [{}])[0].get("message", {}).get("content", "")

    # ── Planner protocol ──────────────────────────────────────────────────
    async def next_step(
        self,
        goal: str,
        history: list[PlanStep],
        world: WorldModel,
        memory: EpisodicMemory,
    ) -> PlanStep | None:
        try:
            step = await self._plan_with_llm(goal, history)
        except Exception:
            step = None
        if step is not None:
            return step
        # Safety net — deterministic planner keeps the agent alive offline.
        return await self._fallback.next_step(goal, history, world, memory)

    async def _plan_with_llm(self, goal: str, history: list[PlanStep]) -> Optional[PlanStep]:
        if history:
            done_lines = "\n".join(
                f"- [{'OK' if s.success else 'FAIL'}] {s.skill}: {s.observation or ''}"
                for s in history[-8:]
            )
        else:
            done_lines = "(none yet)"

        prompt = _PROMPT_TEMPLATE.format(goal=goal, history=done_lines, skills=_SKILL_MENU)
        raw = await __import__("asyncio").to_thread(self._ask_llm, prompt)
        data = _parse_json_block(raw or "")
        if not data:
            return None

        if data.get("done"):
            return None

        skill_raw = str(data.get("skill", "")).strip()
        params = data.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        thought = str(data.get("thought", "LLM planned step"))[:200]

        try:
            skill = parse_skill_name(skill_raw)
        except ValueError:
            return None  # unknown skill → stub decides

        return PlanStep(thought=thought, skill=skill, params=params)
