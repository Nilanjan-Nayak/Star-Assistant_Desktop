"""The browser agent — STAR 2.0's first goal-oriented worker (Phase 5).

It does not drive a browser itself. It turns a goal into a bounded list of
:class:`~Backend.star.agents.base.AgentStepPlan`s and hands each one to the
Phase 4 executor as a typed tool call, so policy, confirmation, audit and
dry-run apply to *every* hop. Then it observes, verifies and — once, honestly —
tries to recover.

Goal shapes it understands (English, Banglish and Bengali):

* "open example.com and tell me what it says"  → open → read → verify
* "read https://en.wikipedia.org/wiki/Star"    → read
* "links on example.com"                       → open → links
* "search rust async book"                     → search (+ read for text engines)
* "ওয়েবসাইটটা খুলে পড়ে শোনাও"                   → open → read
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Sequence

from Backend.star.agents.base import AgentRun, AgentStep, AgentStepPlan, StarAgent
from Backend.star.brain.schemas import AgentName
from Backend.star.browser.guardrails import find_urls
from Backend.star.browser.tools import SEARCH_ENGINES, BrowserToolkit, register_browser_tools
from Backend.star.config.settings import Settings
from Backend.star.observability.logging import star_logger

__all__ = ["BrowserAgent", "build_browser"]

_log = star_logger("star2.browser.agent")

#: engines whose result page is worth reading as text (SERPs of the big ones are not)
_READABLE_ENGINES = frozenset({"wikipedia", "duckduckgo"})

_OPEN_WORDS = (
    "open", "launch", "visit", "go to", "goto", "browse", "kholo", "khole", "khule", "চালু",
    "খুলে", "খুলো", "যাও", "ব্রাউজ",
)
_READ_WORDS = (
    "read", "summar", "summary", "what does", "tell me", "content", "text", "article",
    "পড়ো", "পড়ে", "পড়", "সারসংক্ষেপ", "বলো", "শোনাও", "padho", "pore",
)
_LINK_WORDS = ("link", "links", "urls", "লিংক", "লিঙ্ক")
_SEARCH_WORDS = (
    "search", "google", "find", "look up", "lookup", "khоjo", "khojo", "খোঁজ", "সার্চ", "খুঁজে", "অনুসন্ধান",
)
_SHOT_WORDS = ("screenshot", "snapshot", "ছবি", "স্ক্রিনশট")
_BARE_HOST_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}\b", re.IGNORECASE)
#: anything that is a URL but not http(s) — hand it to the guardrails so the
#: refusal is explicit and audited instead of quietly becoming a web search
_FORBIDDEN_URL_RE = re.compile(
    r"""\b(?:file|ftp|javascript|data|about|chrome|edge|view-source):[^\s<>"']+""", re.IGNORECASE
)
_ENGINE_HINTS = tuple(SEARCH_ENGINES)


class BrowserAgent(StarAgent):
    """Goal → bounded browser steps → verified observation."""

    name = AgentName.BROWSER

    workspace_kind = "browser"   # Phase 7: artifacts land in a browser session
    description = (
        "Opens, reads and searches the web through guardrailed tools. Never touches the "
        "network without a policy decision, and never claims an action it only simulated."
    )
    tools = (
        "browser_open",
        "browser_read",
        "browser_links",
        "browser_search",
        "browser_snapshot",
        "browser_close_tab",
        "browser_click",
        "browser_type",
    )
    keywords = _OPEN_WORDS + _READ_WORDS + _LINK_WORDS + _SEARCH_WORDS + ("website", "web", "url", "http", "www.", "ওয়েবসাইট", "ওয়েব", "পেজ")

    def __init__(self, settings: Settings | None = None, *, toolkit: BrowserToolkit | None = None, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self.max_steps = max(1, self.settings.browser.max_steps)
        self.toolkit = toolkit

    # ── OBSERVE: what does this goal imply? ───────────────────────────────
    def plan(self, goal: str) -> Sequence[AgentStepPlan]:
        text = (goal or "").strip()
        if not text:
            return ()
        lowered = text.lower()

        refused = _FORBIDDEN_URL_RE.findall(text)
        if refused and not find_urls(text):
            # never silently reinterpret "file:///etc/passwd" as a search query:
            # propose the action, let the guardrails refuse it, and report why
            return [
                AgentStepPlan(
                    action="open",
                    tool="browser_open",
                    arguments={"url": refused[0][:500]},
                    expect="the guardrails refuse a non-http(s) URL and say why",
                )
            ]

        urls = find_urls(text)
        if not urls:
            hosts = [h for h in _BARE_HOST_RE.findall(lowered) if not h.endswith(_ENGINE_HINTS)]
            urls = [f"https://{hosts[0]}"] if hosts else []
        url = urls[0] if urls else ""
        if not url and self.score_goal(text) <= 0.0:
            return ()                      # not a browser goal — say so instead of guessing

        wants_open = any(word in lowered for word in _OPEN_WORDS)
        wants_read = any(word in lowered for word in _READ_WORDS)
        wants_links = any(word in lowered for word in _LINK_WORDS)
        wants_shot = any(word in lowered for word in _SHOT_WORDS)
        wants_search = any(word in lowered for word in _SEARCH_WORDS) or not url

        steps: list[AgentStepPlan] = []

        if url:
            if wants_open or not (wants_read or wants_links or wants_shot):
                steps.append(
                    AgentStepPlan(
                        action="open",
                        tool="browser_open",
                        arguments={"url": url},
                        expect=f"the browser is pointed at {url}",
                        # a window is a nicety; the reading is the point
                        optional=not self.settings.browser.auto_open,
                    )
                )
            if wants_read or not (wants_links or wants_shot):
                steps.append(
                    AgentStepPlan(
                        action="read",
                        tool="browser_read",
                        arguments={"url": url, "max_chars": self.settings.browser.max_chars},
                        expect="the page returns a title and readable text",
                    )
                )
            if wants_links:
                steps.append(
                    AgentStepPlan(
                        action="links",
                        tool="browser_links",
                        arguments={"url": url, "limit": 20},
                        expect="a list of links comes back",
                        optional=True,
                    )
                )
            if wants_shot:
                steps.append(
                    AgentStepPlan(
                        action="snapshot",
                        tool="browser_snapshot",
                        arguments={},
                        expect="a screenshot of the browser window",
                        optional=True,
                    )
                )
            return steps

        if wants_search:
            engine = next((name for name in _ENGINE_HINTS if name in lowered), "google")
            query = _clean_query(text, engine)
            steps.append(
                AgentStepPlan(
                    action="search",
                    tool="browser_search",
                    arguments={"query": query, "engine": engine, "open_browser": self.settings.browser.auto_open},
                    expect=f"a {engine} search URL for {query!r}",
                )
            )
            if engine in _READABLE_ENGINES:
                steps.append(
                    AgentStepPlan(
                        action="read",
                        tool="browser_read",
                        arguments={
                            "url": _engine_url(engine, query),
                            "max_chars": self.settings.browser.max_chars,
                        },
                        expect="the search result page returns readable text",
                        optional=True,
                    )
                )
            return steps

        return ()

    # ── OBSERVE (after the act): reduce a result to facts ─────────────────
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
            # a guardrail refusal is a policy outcome, not a tool failure
            base["blocked_by_policy"] = True
            base["block_rule"] = verdict.get("rule", "")
            base["block_reason"] = verdict.get("reason", "")

        action = step_plan.action
        if action == "read":
            text = str(data.get("text") or "")
            base.update(
                {
                    "url": data.get("url", step_plan.arguments.get("url", "")),
                    "title": data.get("title", ""),
                    "description": data.get("description", ""),
                    "text_chars": len(text),
                    "word_count": data.get("word_count", 0),
                    "text_head": text[:400],
                    "link_count": data.get("link_count", 0),
                    "truncated": bool(data.get("truncated")),
                    "simulated": bool(data.get("simulated")),
                }
            )
            if text:
                context["last_text"] = text
                context["texts"].append(text)
            if data.get("url"):
                context["last_url"] = str(data["url"])
        elif action == "open":
            base.update(
                {
                    "url": data.get("url", step_plan.arguments.get("url", "")),
                    "host": data.get("host", ""),
                    "opened": bool(data.get("opened", False)),
                    "simulated": bool(data.get("simulated")),
                }
            )
            if base["url"]:
                context["urls"].append(base["url"])
                context["last_url"] = base["url"]
        elif action == "search":
            base.update(
                {
                    "query": data.get("query", step_plan.arguments.get("query", "")),
                    "engine": data.get("engine", ""),
                    "url": data.get("url", ""),
                    "opened": bool(data.get("opened", False)),
                    "simulated": bool(data.get("simulated")),
                }
            )
            if base["url"]:
                context["search_url"] = base["url"]
        elif action == "links":
            links = data.get("links") or []
            base.update({"count": len(links), "top": links[:10], "url": data.get("url", step_plan.arguments.get("url", ""))})
            if links:
                context["links"] = links
            if base["url"]:
                context["last_url"] = str(base["url"])
        else:                                   # snapshot / click / type / close_tab
            base.update({k: v for k, v in data.items() if isinstance(v, (str, int, float, bool)) and k != "value"})
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
            message = str(observation.get("error") or step.error or "the tool reported failure")
            if "not available here" in message:
                return False, "desktop-only tool unavailable in this environment"
            return False, message[:200]

        if observation.get("simulated") or step.decision == "simulated":
            return True, "simulated in dry-run — nothing was really fetched or clicked"

        action = step_plan.action
        if action == "read":
            chars = int(observation.get("text_chars") or 0)
            if chars <= 0 and not observation.get("title"):
                return False, "the page came back empty"
            return True, f"read {chars} chars from {observation.get('url') or step_plan.arguments.get('url')}"
        if action == "open":
            if observation.get("opened") is False and observation.get("url"):
                return True, "no browser window could be launched here, but the URL passed the guardrails"
            return True, f"opened {observation.get('url')}"
        if action == "search":
            if not observation.get("url"):
                return False, "no search URL was produced"
            return True, f"{observation.get('engine') or 'search'} URL ready"
        if action == "links":
            count = int(observation.get("count") or 0)
            return True, f"{count} link(s) found" if count else "the page has no links"
        return True, "ok"

    # ── RECOVER (one honest attempt, bounded) ─────────────────────────────
    async def recover(
        self, step_plan: AgentStepPlan, step: AgentStep, context: dict[str, Any], *, run: AgentRun
    ) -> tuple[bool, str]:
        recoveries = int(context.get("recoveries", 0))
        if recoveries >= 2:
            return False, "recovery budget used up"
        context["recoveries"] = recoveries + 1

        note = step.verification_note or step.error or ""
        if step.decision == "needs_confirmation":
            return False, f"waiting for a human decision ({step.confirmation_id or 'no id'})"
        if "desktop-only tool unavailable" in note:
            return False, "this machine cannot do that browser action (no desktop tooling)"

        # a window we cannot open is not a failure to read: fall back to fetching
        if step_plan.action == "open" and self.executor is not None:
            url = str(step_plan.arguments.get("url") or context.get("last_url") or "")
            if url:
                fallback = await self.executor.call(
                    "browser_read",
                    {"url": url, "max_chars": self.settings.browser.max_chars},
                    session_id=run.session_id,
                    request_id=run.request_id,
                    plan_id=run.plan_id,
                    task_id=run.task_id,
                    call_id=f"{run.run_id}:recover-read",
                    dry_run=run.dry_run,
                )
                if fallback.ok:
                    context["last_url"] = url
                    context.setdefault("recovered_by", []).append("browser_read")
                    return True, f"could not open a window — read {url} instead"

        # reading failed: try the page's own links once (only if we already know them)
        if step_plan.action == "read" and context.get("links"):
            for link in list(context["links"])[:3]:
                candidate = link.get("url") if isinstance(link, dict) else str(link)
                if not candidate or candidate == context.get("last_url"):
                    continue
                retry = await self.executor.call(
                    "browser_read",
                    {"url": candidate, "max_chars": self.settings.browser.max_chars},
                    session_id=run.session_id,
                    request_id=run.request_id,
                    plan_id=run.plan_id,
                    task_id=run.task_id,
                    call_id=f"{run.run_id}:recover-link",
                    dry_run=run.dry_run,
                )
                if retry.ok:
                    context["last_url"] = candidate
                    return True, f"original page unreadable — read {candidate} instead"

        if step_plan.optional:
            return True, "optional step skipped"
        return False, note[:200] or "no recovery available"

    # ── the answer the brain will speak ───────────────────────────────────
    def summarise(self, run: AgentRun, context: dict[str, Any]) -> str:
        if run.state == "dry_run":
            actions = ", ".join(dict.fromkeys(step.action for step in run.steps))
            return f"dry-run: would have run {actions} for {run.goal[:60]!r}"
        title = ""
        for step in reversed(run.steps):
            if step.action == "read" and step.observation.get("title"):
                title = str(step.observation["title"])
                break
        actions = [step.action for step in run.steps if step.ok]
        if actions == ["links"]:
            count = int(run.steps[0].observation.get("count") or 0)
            where = context.get("last_url") or ""
            return f"listed {count} link(s) on {where}".rstrip()
        url = context.get("last_url") or context.get("search_url") or ""
        if title and url:
            return f"read {title} ({url})"
        if url:
            return f"worked on {url}"
        return f"{sum(1 for s in run.steps if s.ok)}/{len(run.steps)} browser step(s) succeeded"

    def summarise_result(self, run: AgentRun, context: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"state": run.state, "steps": run.steps_used, "dry_run": run.dry_run}
        if context.get("last_url"):
            result["url"] = context["last_url"]
        if context.get("search_url"):
            result["search_url"] = context["search_url"]
        for step in reversed(run.steps):
            if step.action == "read" and step.observation:
                result["title"] = step.observation.get("title", "")
                result["text"] = str(context.get("last_text") or "")[: self.settings.browser.max_chars]
                result["word_count"] = step.observation.get("word_count", 0)
                break
        if context.get("links"):
            result["links"] = list(context["links"])[:10]
        # the base result carries the background workspace this run used (Phase 7)
        inherited = super().summarise_result(run, context)
        for key in ("workspace", "workspace_error"):
            if key in inherited:
                result[key] = inherited[key]
        return result

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["guardrails"] = self.toolkit.guard.describe() if self.toolkit else None
        data["last_run"] = self.runs[-1].public() if self.runs else None
        return data


_BENGALI_RE = re.compile(r"[\u0980-\u09FF]")


def _engine_url(engine: str, query: str) -> str:
    """Search URL for an engine — Bengali queries land on bn.wikipedia like the legacy tool."""
    encoded = urllib.parse.quote_plus(query)
    if engine == "wikipedia" and _BENGALI_RE.search(query):
        return f"https://bn.wikipedia.org/w/index.php?search={encoded}"
    return SEARCH_ENGINES.get(engine, SEARCH_ENGINES["google"]).format(q=encoded)


def _clean_query(goal: str, engine: str) -> str:
    """Strip the instruction words, keep what the user actually wants found."""
    text = goal.strip()
    for word in _SEARCH_WORDS:
        text = re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.IGNORECASE)
    for noise in ("for me", "please", "koro", "kore", "kholo", "দাও", "করো", "করে", "একটু", "please"):
        text = re.sub(rf"\b{re.escape(noise)}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(rf"\b{re.escape(engine)}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.:;!?-")
    return text[:200] or goal.strip()[:200]


def build_browser(
    settings: Settings | None = None,
    *,
    bus: Any = None,
    executor: Any = None,
    registry: Any = None,
    stop_gate: Any = None,
    workspace: Any = None,
) -> BrowserAgent:
    """Register the browser tools and return a ready agent (used by ``main.py``)."""
    cfg = settings or Settings()
    if registry is not None and cfg.browser.enabled:
        toolkit = register_browser_tools(registry, cfg, bus=bus)
    else:
        toolkit = BrowserToolkit(cfg, registry=registry, bus=bus)
    return BrowserAgent(
        cfg, bus=bus, executor=executor, registry=registry, stop_gate=stop_gate, toolkit=toolkit, workspace=workspace
    )
