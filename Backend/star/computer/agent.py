"""The computer agent — screen, mouse and keyboard as a goal-oriented worker (Phase 6).

Same spine as the browser agent (:class:`~Backend.star.agents.base.StarAgent`),
same rule: **it never drives the OS itself.** It turns a goal into bounded steps
and hands each one to the Phase 4 executor as a typed tool call, so validation,
permissions, confirmations, audit and dry-run all apply.

Extra step for physical actions: before the executor is even asked, the intended
action is built as a real ``ActionSpec`` and pushed through the computer
guardrails + the existing six safety-governor invariants
(:meth:`ComputerToolkit.preview`). So a dry-run rehearsal refuses an off-screen
click exactly like a live run would — the rehearsal is the point.

Goal shapes it understands (English, Banglish, Bengali):

* "take a screenshot" / "ছবি তোলো" / "screen capture"       → capture
* "what is on the screen?" / "স্ক্রিনে কী আছে"                → screen_read
* "click at 480,320" / "480,320 এ ক্লিক করো"                 → move → click → capture
* "type hello world" / "হ্যালো লেখো"                          → keyboard_type
* "press enter" / "এন্টার চাপো"                               → key_press
* "alt+tab" / "hotkey ctrl+s"                                → hotkey (confirmation-gated)
* "scroll down 5" / "একটু নিচে স্ক্রল করো"                    → scroll
* "drag to 900,700"                                          → mouse_drag
* "wait 3 seconds" / "৩ সেকেন্ড অপেক্ষা করো"                  → wait
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from agent.core.enums import ActionKind

from Backend.star.agents.base import AgentRun, AgentStep, AgentStepPlan, StarAgent
from Backend.star.brain.schemas import AgentName
from Backend.star.computer.tools import ComputerToolkit, register_computer_tools
from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger
from Backend.star.voice.language import normalize_text

__all__ = ["ComputerAgent", "build_computer"]

_log = star_logger("star2.computer.agent")

_SHOT_WORDS = ("screenshot", "screen shot", "capture", "ছবি তোলো", "ছবি তুলো", "স্ক্রিনশট", "ছবি", "screen capture")
_READ_WORDS = ("what is on the screen", "read the screen", "screen e ki", "স্ক্রিনে কী", "স্ক্রিনে কি", "পর্দায় কী", "see the screen")
_CLICK_WORDS = ("click", "tap", "ক্লিক", "চাপ দাও", "press at")
_TYPE_WORDS = ("type", "write", "লিখো", "টাইপ", "লেখো")
_PRESS_WORDS = ("press", "hit", "চাপো", "প্রেস")
_HOTKEY_WORDS = ("hotkey", "shortcut", "combination", "হটকি")
_SCROLL_WORDS = ("scroll", "wheel", "স্ক্রল", "ঘোরাও")
_DRAG_WORDS = ("drag", "টানো", "ড্র্যাগ")
_WAIT_WORDS = ("wait", "sleep", "অপেক্ষা", "থামো")
_MOVE_WORDS = ("move the pointer", "move mouse", "pointer move", "মাউস সরাও")

_COORD_RE = re.compile(r"\b(\d{1,5})\s*(?:,|x|\s)\s*(\d{1,5})\b")
_KEYS_RE = re.compile(r"\b([a-z]{2,12}(?:\s*\+\s*[a-z0-9]{1,12})+)\b")
_NAMED_KEYS = ("enter", "tab", "esc", "escape", "space", "backspace", "delete", "up", "down", "left", "right", "home", "end", "pageup", "pagedown", "f1", "f2", "f3", "f4", "f5", "f11")
_QUOTED_RE = re.compile(r"""["“']([^"”']{1,600})["”']""")
_SECONDS_RE = re.compile(r"\b(\d{1,3}(?:\.\d)?)\s*(?:s|sec|seconds?|সেকেন্ড)\b")

#: step action → motor action kind (perception steps have no motor action)
_STEP_KINDS: dict[str, ActionKind] = {
    "move": ActionKind.MOVE,
    "click": ActionKind.CLICK,
    "drag": ActionKind.DRAG,
    "type": ActionKind.TYPE,
    "press": ActionKind.PRESS,
    "hotkey": ActionKind.HOTKEY,
    "scroll": ActionKind.SCROLL,
    "wait": ActionKind.WAIT,
}


class ComputerAgent(StarAgent):
    """Goal → bounded physical steps → verified against the screen."""

    name = AgentName.COMPUTER

    workspace_kind = "computer"   # Phase 7: artifacts land in a computer session
    description = (
        "Looks at the screen and moves the mouse/keyboard through the existing agent motor + "
        "safety governor. Dry-run records exactly what would happen; nothing moves without policy."
    )
    tools = (
        "screen_capture",
        "screen_read",
        "mouse_move",
        "mouse_click",
        "mouse_drag",
        "keyboard_type",
        "key_press",
        "hotkey",
        "scroll",
        "wait",
    )
    keywords = (
        _SHOT_WORDS + _READ_WORDS + _CLICK_WORDS + _TYPE_WORDS + _PRESS_WORDS + _SCROLL_WORDS
        + _DRAG_WORDS + _MOVE_WORDS + ("screen", "mouse", "keyboard", "pointer", "স্ক্রিন", "মাউস", "কীবোর্ড")
    )

    def __init__(self, settings: Settings | None = None, *, toolkit: ComputerToolkit | None = None, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self.max_steps = max(1, self.settings.computer.max_steps)
        self.toolkit = toolkit

    # ── OBSERVE: what does the goal imply? ────────────────────────────────
    def plan(self, goal: str) -> Sequence[AgentStepPlan]:
        text = normalize_text(goal or "")
        if not text:
            return ()
        lowered = text.lower()
        coord = _find_coord(lowered)
        steps: list[AgentStepPlan] = []

        def add(action: str, tool: str, arguments: dict[str, Any], expect: str, *, optional: bool = False) -> None:
            steps.append(
                AgentStepPlan(action=action, tool=tool, arguments=arguments, expect=expect, optional=optional)
            )

        # perception first: know the screen before touching it
        if any(word in lowered for word in _SHOT_WORDS):
            add("capture", "screen_capture", {}, "a screenshot comes back")
        if any(word in lowered for word in _READ_WORDS):
            add("read_screen", "screen_read", {"query": text[:120]}, "the screen text is readable")
            return steps[: self.max_steps]
        if any(word in lowered for word in _SHOT_WORDS) and not _has_motor_intent(lowered):
            return steps[: self.max_steps]

        if any(word in lowered for word in _WAIT_WORDS):
            seconds = _find_seconds(lowered)
            add("wait", "wait", {"seconds": seconds}, f"waited {seconds}s")

        if any(word in lowered for word in _DRAG_WORDS) and coord:
            add("drag", "mouse_drag", {"x": coord[0], "y": coord[1]}, f"dragged to {coord[0]},{coord[1]}")
        elif any(word in lowered for word in _CLICK_WORDS) and coord:
            add("move", "mouse_move", {"x": coord[0], "y": coord[1]}, f"pointer at {coord[0]},{coord[1]}", optional=True)
            add("click", "mouse_click", {"x": coord[0], "y": coord[1], "button": _button(lowered), "clicks": _clicks(lowered)},
                f"clicked at {coord[0]},{coord[1]}")
            add("capture", "screen_capture", {}, "a screenshot proves what changed", optional=True)
        elif any(word in lowered for word in _MOVE_WORDS) and coord:
            add("move", "mouse_move", {"x": coord[0], "y": coord[1]}, f"pointer at {coord[0]},{coord[1]}")

        # keyboard intents are emitted in the order the human wrote them
        def first_pos(words: tuple[str, ...] | list[str]) -> int:
            found = [lowered.find(word) for word in words if word in lowered]
            return min(found) if found else -1

        keyboard: list[tuple[int, AgentStepPlan]] = []
        keys = _find_keys(lowered)
        if keys and (any(word in lowered for word in _HOTKEY_WORDS) or "+" in keys[0] or len(keys) > 1):
            keyboard.append((first_pos(_HOTKEY_WORDS), AgentStepPlan(
                action="hotkey", tool="hotkey", arguments={"keys": keys}, expect=f"pressed {'+'.join(keys)}")))
        elif any(word in lowered for word in _PRESS_WORDS):
            key = _find_named_key(lowered)
            if key:
                keyboard.append((first_pos(_PRESS_WORDS), AgentStepPlan(
                    action="press", tool="key_press", arguments={"key": key}, expect=f"pressed {key}")))

        typed = _find_text_to_type(text, lowered)
        if typed and any(word in lowered for word in _TYPE_WORDS):
            keyboard.append((first_pos(_TYPE_WORDS), AgentStepPlan(
                action="type", tool="keyboard_type", arguments={"text": typed},
                expect=f"typed {len(typed)} character(s)")))

        for _, planned in sorted(keyboard, key=lambda item: (item[0] < 0, item[0])):
            steps.append(planned)

        if any(word in lowered for word in _SCROLL_WORDS):
            amount = _find_amount(lowered)
            direction = "up" if ("up" in lowered or "উপরে" in lowered or "উপরে" in text) else "down"
            add("scroll", "scroll", {"amount": amount, "direction": direction}, f"scrolled {direction} {amount}")

        if not steps:
            return ()
        if not any(step.action in ("capture", "read_screen") for step in steps) and _has_motor_intent(lowered):
            # VERIFY needs eyes: finish with a screenshot when the goal moved something
            add("capture", "screen_capture", {}, "a screenshot shows the result", optional=True)
        return steps[: self.max_steps]

    # ── ACT: guardrail the intended action *before* the executor ──────────
    async def act(self, step_plan: AgentStepPlan, context: dict[str, Any], *, run: AgentRun, dry_run: bool) -> dict[str, Any]:
        kind = _STEP_KINDS.get(step_plan.action)
        if kind is not None and self.toolkit is not None:
            preview = self.toolkit.preview(kind, _motor_params(step_plan))
            if not preview.get("ok", False):
                return {
                    "ok": False,
                    "decision": "blocked",
                    "error": preview.get("error", "blocked by computer guardrails"),
                    "result": {"verdict": preview.get("verdict", {}), "action": preview.get("action", {})},
                }
            context.setdefault("previews", []).append(preview.get("action", {}))
        return await super().act(step_plan, context, run=run, dry_run=dry_run)

    # ── OBSERVE (after the act) ───────────────────────────────────────────
    def observe(self, step_plan: AgentStepPlan, outcome: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        data = outcome.get("result") if isinstance(outcome.get("result"), dict) else {}
        base: dict[str, Any] = {
            "decision": outcome.get("decision", ""),
            "ok": bool(outcome.get("ok")),
            "dry_run": bool(outcome.get("dry_run")),
            "via": data.get("via", ""),
        }
        if outcome.get("error"):
            base["error"] = str(outcome["error"])[:300]
        verdict = data.get("verdict")
        if isinstance(verdict, dict) and not verdict.get("ok", True):
            base["blocked_by_policy"] = True
            base["block_rule"] = verdict.get("rule", "")
            base["block_reason"] = verdict.get("reason", "")
        action = data.get("action")
        if isinstance(action, dict):
            base["action"] = action
            context.setdefault("actions", []).append(action)
        for key in ("screen_changed", "diff_ratio", "retries", "detail", "action_id"):
            if key in data:
                base[key] = data[key]
        if data.get("simulated"):
            base["simulated"] = True
            base["action"] = base.get("action") or dict(step_plan.arguments, kind=step_plan.action)
        for key in ("path", "image_path", "text", "ocr_text", "width", "height"):
            if key in data:
                base[key] = data[key]
        return base

    # ── VERIFY ────────────────────────────────────────────────────────────
    def verify(self, step_plan: AgentStepPlan, observation: dict[str, Any], step: AgentStep) -> tuple[bool, str]:
        if observation.get("blocked_by_policy"):
            return False, f"guardrails refused ({observation.get('block_rule')}): {observation.get('block_reason')}"[:200]
        if step.decision in ("denied", "blocked"):
            return False, f"policy refused the step ({step.decision})"
        if step.decision == "needs_confirmation":
            return False, f"waiting for your approval ({step.confirmation_id or 'confirmation'})"
        if not step.ok:
            message = str(observation.get("error") or step.error or "the action did not complete")
            if "not available here" in message:
                return False, "desktop-only capability unavailable in this environment"
            return False, message[:200]

        if observation.get("simulated") or step.decision == "simulated":
            # merge what the motor said it would do with the plan, so the note is specific
            action: dict[str, Any] = {**(step.arguments or {}), **(observation.get("action") or {})}
            coord = action.get("coord")
            if not coord and isinstance(action.get("x"), (int, float)) and isinstance(action.get("y"), (int, float)):
                coord = {"x": action["x"], "y": action["y"]}
            detail = ""
            if coord:
                detail = f" at {coord['x']},{coord['y']}"
            elif action.get("text"):
                detail = f" {str(action['text'])[:40]!r}"
            elif action.get("keys"):
                detail = f" {'+'.join(str(key) for key in action['keys'])}"
            elif action.get("key"):
                detail = f" {action['key']}"
            elif action.get("amount") is not None or action.get("seconds") is not None:
                detail = f" {action.get('direction', '')} {action.get('amount', action.get('seconds', ''))}".strip()
            return True, f"simulated in dry-run — recorded the intended {step.action}{detail}; nothing moved"

        if step.action in ("capture", "read_screen"):
            if observation.get("error"):
                return False, str(observation["error"])[:200]
            return True, "screen observation captured"

        changed = observation.get("screen_changed")
        ratio = observation.get("diff_ratio")
        if self.toolkit is not None and self.toolkit.motor.mode == "pyautogui" and changed is False:
            return False, "the screen did not change — the action probably missed"
        note = f"motor reported success ({observation.get('via') or 'agent.motor'})"
        if ratio is not None:
            note += f", screen diff {ratio}"
        return True, note

    # ── RECOVER ───────────────────────────────────────────────────────────
    async def recover(self, step_plan: AgentStepPlan, step: AgentStep, context: dict[str, Any], *, run: AgentRun) -> tuple[bool, str]:
        used = int(context.get("recoveries", 0))
        if used >= 2:
            return False, "recovery budget used up"
        context["recoveries"] = used + 1

        note = step.verification_note or step.error or ""
        if step.decision == "needs_confirmation":
            return False, f"waiting for a human decision ({step.confirmation_id or 'no id'})"
        if "desktop-only capability unavailable" in note:
            return False, "this machine cannot do that action (no desktop tooling)"
        if step.observation.get("blocked_by_policy") or step.decision in ("denied", "blocked"):
            return False, "policy refused — retrying would be wrong"

        # a missed click is worth one careful retry: look, then move, then click again
        if step_plan.action == "click" and self.executor is not None:
            coord = step_plan.arguments
            for tool, args in (("screen_capture", {}), ("mouse_move", {"x": coord.get("x", 0), "y": coord.get("y", 0)})):
                retry = await self.executor.call(
                    tool, args, session_id=run.session_id, request_id=run.request_id,
                    plan_id=run.plan_id, task_id=run.task_id,
                    call_id=f"{run.run_id}:recover-{tool}", dry_run=run.dry_run,
                )
                if not retry.ok and tool == "mouse_move":
                    return False, f"could not reposition the pointer: {retry.error or 'unknown'}"
            again = await self.executor.call(
                "mouse_click", dict(coord), session_id=run.session_id, request_id=run.request_id,
                plan_id=run.plan_id, task_id=run.task_id,
                call_id=f"{run.run_id}:recover-click", dry_run=run.dry_run,
            )
            if again.ok:
                context.setdefault("recovered_by", []).append("mouse_click")
                return True, "looked again, repositioned the pointer and clicked once more"
            return False, "the second click did not land either"

        if step_plan.optional:
            return True, "optional step skipped"
        return False, note[:200] or "no recovery available"

    # ── the answer ────────────────────────────────────────────────────────
    def summarise(self, run: AgentRun, context: dict[str, Any]) -> str:
        if run.state == "dry_run":
            actions = ", ".join(dict.fromkeys(step.action for step in run.steps))
            return f"dry-run: would have run {actions} — nothing moved"
        actions = [step.action for step in run.steps if step.ok]
        if not actions:
            return f"no step succeeded ({run.state})"
        return f"{', '.join(dict.fromkeys(actions))} — {len(actions)} step(s) done"

    def summarise_result(self, run: AgentRun, context: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"state": run.state, "steps": run.steps_used, "dry_run": run.dry_run}
        actions = list(context.get("actions") or [])
        if actions:
            result["actions"] = actions[:12]
        for step in reversed(run.steps):
            if step.action == "capture" and step.observation:
                result["capture"] = {k: v for k, v in step.observation.items() if k in ("path", "image_path", "via", "ok")}
                break
            if step.action == "read_screen" and step.observation:
                result["screen_text"] = str(step.observation.get("text") or step.observation.get("ocr_text") or "")[:800]
                break
        if self.toolkit is not None:
            result["motor"] = self.toolkit.motor.describe()
        # the base result carries the background workspace this run used (Phase 7)
        inherited = super().summarise_result(run, context)
        for key in ("workspace", "workspace_error"):
            if key in inherited:
                result[key] = inherited[key]
        return result

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["motor"] = self.toolkit.motor.describe() if self.toolkit else None
        data["guardrails"] = self.toolkit.guard.describe() if self.toolkit else None
        data["last_run"] = self.runs[-1].public() if self.runs else None
        return data


# ─────────────────────────────────────────────────────────────────────────────
#  Goal parsing helpers (deterministic — no model output ever reaches the motor)
# ─────────────────────────────────────────────────────────────────────────────


def _has_motor_intent(lowered: str) -> bool:
    groups = (_CLICK_WORDS, _TYPE_WORDS, _PRESS_WORDS, _HOTKEY_WORDS, _SCROLL_WORDS, _DRAG_WORDS, _MOVE_WORDS, _WAIT_WORDS)
    return any(any(word in lowered for word in group) for group in groups)


def _find_coord(lowered: str) -> tuple[int, int] | None:
    for x, y in _COORD_RE.findall(lowered):
        return int(x), int(y)
    return None


def _find_seconds(lowered: str) -> float:
    match = _SECONDS_RE.search(lowered)
    return float(match.group(1)) if match else 1.0


def _find_amount(lowered: str) -> int:
    match = re.search(r"\b(\d{1,3})\b", lowered)
    return max(1, min(20, int(match.group(1)))) if match else 3


def _find_keys(lowered: str) -> list[str]:
    match = _KEYS_RE.search(lowered)
    if match:
        return [part.strip() for part in match.group(1).split("+") if part.strip()]
    for key in _NAMED_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", lowered) and any(word in lowered for word in _HOTKEY_WORDS):
            return [key]
    return []


def _find_named_key(lowered: str) -> str:
    for key in _NAMED_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", lowered):
            return "esc" if key == "escape" else key
    return ""


def _find_text_to_type(text: str, lowered: str) -> str:
    quoted = _QUOTED_RE.search(text)
    if quoted:
        return quoted.group(1).strip()
    markers = ("type ", "write ", "টাইপ ", "লিখো ", "লেখো ")
    for marker in markers:
        index = lowered.find(marker)
        if index >= 0:
            rest = text[index + len(marker):].strip()
            rest = re.sub(r"\b(please|koro|kore|দাও|করো|একটু)\b", " ", rest, flags=re.IGNORECASE)
            return re.sub(r"\s+", " ", rest).strip(" ,.:;!?-")[:600]
    return ""


def _button(lowered: str) -> str:
    if "right" in lowered or "ডান" in lowered:
        return "right"
    if "middle" in lowered:
        return "middle"
    return "left"


def _clicks(lowered: str) -> int:
    if "double" in lowered or "ডাবল" in lowered or "দুইবার" in lowered or "twice" in lowered:
        return 2
    if "triple" in lowered or "তিনবার" in lowered:
        return 3
    return 1


def _motor_params(step_plan: AgentStepPlan) -> dict[str, Any]:
    """Step arguments → the params ``agent.motor.spec.spec_from_kind`` expects."""
    args = dict(step_plan.arguments)
    kind = _STEP_KINDS.get(step_plan.action)
    if kind is ActionKind.WAIT:
        return {"duration": max(0.0, float(args.get("seconds", 1.0)))}
    if kind is ActionKind.TYPE:
        return {"text": str(args.get("text", ""))}
    if kind is ActionKind.HOTKEY:
        return {"keys": [str(key) for key in (args.get("keys") or [])]}
    if kind is ActionKind.PRESS:
        return {"key": str(args.get("key", ""))}
    if kind is ActionKind.SCROLL:
        params: dict[str, Any] = {"amount": int(args.get("amount", 3)), "direction": str(args.get("direction", "down"))}
        if int(args.get("x", -1)) >= 0 and int(args.get("y", -1)) >= 0:
            params.update({"x": int(args["x"]), "y": int(args["y"])})
        return params
    return {"x": int(args.get("x", 0)), "y": int(args.get("y", 0))}


def build_computer(
    settings: Settings | None = None,
    *,
    bus: Any = None,
    executor: Any = None,
    registry: Any = None,
    stop_gate: Any = None,
    workspace: Any = None,
) -> ComputerAgent:
    """Register the computer tools and return a ready agent (used by ``main.py``)."""
    cfg = settings or Settings()
    if registry is not None and cfg.computer.enabled:
        toolkit = register_computer_tools(registry, cfg, bus=bus)
    else:
        toolkit = ComputerToolkit(cfg, registry=registry, bus=bus)
    return ComputerAgent(
        cfg, bus=bus, executor=executor, registry=registry, stop_gate=stop_gate, toolkit=toolkit, workspace=workspace
    )
