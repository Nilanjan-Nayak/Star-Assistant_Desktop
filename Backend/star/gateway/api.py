"""HTTP route table for the STAR 2.0 gateway.

REST is for request/response; the WebSocket carries the streaming mission events
(blueprint §7). Every handler returns an :class:`HttpResponse` and may raise
:class:`HttpError`, which the server converts into a JSON error body.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from Backend.star.contracts import StarApplicationProtocol
from Backend.star.gateway.http import (
    HttpError,
    HttpRequest,
    HttpResponse,
    html_response,
    json_response,
    text_response,
)

__all__ = ["Route", "Router", "build_router"]

Handler = Callable[[StarApplicationProtocol, HttpRequest], Awaitable[HttpResponse] | HttpResponse]


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    path: str
    handler: Handler
    description: str = ""


# ─────────────────────────────────────────────────────────────────────────────
#  Handlers
# ─────────────────────────────────────────────────────────────────────────────


async def _health(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    return json_response(app.health())


async def _info(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    return json_response(app.info())


async def _status(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    return json_response(app.status())


async def _chat(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "chat body must be a JSON object")
    text = str(body.get("text") or body.get("query") or "").strip()
    if not text:
        raise HttpError(422, "field 'text' is required and must be non-empty")
    if len(text) > 4000:
        raise HttpError(413, "message too long (max 4000 characters)")
    started = time.perf_counter()
    result = await app.chat(
        text,
        session_id=body.get("session_id") or None,
        language=body.get("language") or None,
        source=str(body.get("source") or "http"),
    )
    result.setdefault("latency_ms", round((time.perf_counter() - started) * 1000.0, 2))
    return json_response(result)


async def _events(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    limit = min(req.query_int("limit", 50), 500)
    since = req.query_int("since", 0)
    kinds_raw = req.query.get("kinds", "")
    kinds = frozenset(k.strip() for k in kinds_raw.split(",") if k.strip()) or None
    return json_response(
        {
            "ok": True,
            "events": app.event_history(limit=limit, kinds=kinds, since_seq=since),
        }
    )


async def _tools(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    return json_response({"ok": True, "tools": app.tools()})


async def _agents(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    return json_response({"ok": True, "agents": app.agents()})


async def _tasks(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    limit = min(req.query_int("limit", 50), 200)
    return json_response({"ok": True, "tasks": app.tasks(limit=limit), "plans": app.plans(limit=10)})


async def _memory(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    query = req.query.get("q") or None
    limit = min(req.query_int("limit", 20), 100)
    # Phase 8: the layered snapshot awaits the stores itself, so prefer the async
    # variant when the app has one — the event loop is never blocked by SQLite.
    probe = getattr(app, "memory_snapshot_async", None)
    payload = await probe(query, limit=limit) if callable(probe) else app.memory_snapshot(query, limit=limit)
    return json_response(payload)


async def _remember(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    text = str(body.get("text") or "").strip()
    if not text:
        raise HttpError(422, "field 'text' is required")
    result = await app.remember(
        text,
        kind=str(body.get("kind") or "fact"),
        key=body.get("key") or None,
        value=body.get("value") or None,
    )
    return json_response(result)


async def _audit(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    limit = min(req.query_int("limit", 50), 500)
    return json_response({"ok": True, "entries": app.audit_tail(limit=limit)})


async def _learning(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    """``GET`` = the safe learning loop (gates, candidates, feedback, promotions).

    ``POST`` records one explicit feedback signal — the human half of the loop:
    ``{"signal": "positive|negative|correction", "subject"/"candidate_id", "key"/"value", "note"}``.
    A stated preference (``key``+``value``) with a positive/correction signal seeds a
    user-approved habit; nothing is promoted that has not cleared every gate.
    """
    if req.method == "GET":
        limit = min(req.query_int("limit", 50), 200)
        return json_response(app.learning_snapshot(limit=limit))
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    signal = str(body.get("signal") or "").strip().lower()
    if signal not in ("positive", "negative", "correction"):
        raise HttpError(422, "field 'signal' must be positive, negative or correction")
    return json_response(
        app.learning_feedback(
            signal=signal,
            subject=str(body.get("subject") or ""),
            candidate_id=str(body.get("candidate_id") or ""),
            key=str(body.get("key") or ""),
            value=str(body.get("value") or ""),
            note=str(body.get("note") or ""),
            session_id=str(body.get("session_id") or ""),
        )
    )


async def _learning_cycle(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    """``POST`` runs one validation (+ promotion) sweep — never bypasses a gate."""
    body = req.json() if req.method == "POST" else {}
    if body and not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    body = body or {}
    promote = body.get("promote")
    return json_response(
        app.learning_cycle(
            **{
                "promote": True if promote is None else bool(promote),
                **({"limit": int(body["limit"])} if body.get("limit") else {}),
            }
        )
    )


async def _browser(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    """``GET`` = browser agent state; ``POST {"goal": …}`` = run one goal (dry-run respected)."""
    if req.method == "GET":
        return json_response(app.browser_state())
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    goal = str(body.get("goal") or body.get("text") or "").strip()
    if not goal:
        raise HttpError(422, "field 'goal' is required")
    dry_run = body.get("dry_run")
    return json_response(
        await app.run_browser_goal(
            goal,
            session_id=str(body.get("session_id") or ""),
            dry_run=None if dry_run is None else bool(dry_run),
        )
    )


async def _computer(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    """``GET`` = computer agent/motor state; ``POST {"goal": …}`` = run one goal."""
    if req.method == "GET":
        return json_response(app.computer_state())
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    goal = str(body.get("goal") or body.get("text") or "").strip()
    if not goal:
        raise HttpError(422, "field 'goal' is required")
    dry_run = body.get("dry_run")
    return json_response(
        await app.run_computer_goal(
            goal, session_id=str(body.get("session_id") or ""), dry_run=None if dry_run is None else bool(dry_run)
        )
    )


async def _workspaces(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    """``GET`` = manager state + every session; ``POST {"kind","label"}`` = open one."""
    if req.method == "GET":
        return json_response(app.workspace_state())
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    ttl = body.get("ttl_s")
    dry_run = body.get("dry_run")
    return json_response(
        await app.create_workspace(
            str(body.get("kind") or "generic"),
            str(body.get("label") or ""),
            None if ttl is None else float(ttl),
            dry_run=None if dry_run is None else bool(dry_run),
        )
    )


def _workspace_id(req: HttpRequest, action: str) -> str:
    """/api/v1/workspaces/{session_id}/{action} → the session id."""
    parts = req.path.strip("/").split("/")
    # /api/v1/workspaces/{session_id}          → 4 parts
    # /api/v1/workspaces/{session_id}/{action} → 5 parts
    session_id = parts[3] if len(parts) >= 4 else ""
    if not session_id or session_id == "*":
        raise HttpError(422, f"usage: POST /api/v1/workspaces/{{session_id}}/{action}")
    return session_id


async def _workspace_detail(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    return json_response(app.workspace_session(_workspace_id(req, "")))


async def _workspace_close(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    session_id = _workspace_id(req, "close")
    body = req.json() if req.body else {}
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    destroy = bool(body.get("remove_files", body.get("destroy", False)))
    dry_run = body.get("dry_run")
    return json_response(
        await app.close_workspace(session_id, destroy=destroy, dry_run=None if dry_run is None else bool(dry_run))
    )


async def _workspace_checkpoint(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    session_id = _workspace_id(req, "checkpoint")
    body = req.json() if req.body else {}
    label = str(body.get("label") or "") if isinstance(body, dict) else ""
    dry_run = body.get("dry_run") if isinstance(body, dict) else None
    return json_response(
        await app.checkpoint_workspace(session_id, label, dry_run=None if dry_run is None else bool(dry_run))
    )


async def _workspace_restore(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    session_id = _workspace_id(req, "restore")
    body = req.json() if req.body else {}
    checkpoint_id = str(body.get("checkpoint_id") or "") if isinstance(body, dict) else ""
    dry_run = body.get("dry_run") if isinstance(body, dict) else None
    return json_response(
        await app.restore_workspace(session_id, checkpoint_id, dry_run=None if dry_run is None else bool(dry_run))
    )


async def _confirmations(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    return json_response({"ok": True, "confirmations": app.confirmations()})


async def _confirm(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    cid = str(body.get("confirmation_id") or body.get("id") or "").strip()
    if not cid:
        raise HttpError(422, "field 'confirmation_id' is required")
    approve = body.get("approve")
    if not isinstance(approve, bool):
        raise HttpError(422, "field 'approve' must be a boolean")
    return json_response(await app.resolve_confirmation(cid, approve=approve, note=str(body.get("note") or "")))


async def _cancel(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    parts = req.path.strip("/").split("/")
    # /api/v1/tasks/{task_id}/cancel
    task_id = parts[3] if len(parts) >= 5 else ""
    if not task_id:
        raise HttpError(422, "usage: POST /api/v1/tasks/{task_id}/cancel")
    return json_response(await app.cancel_task(task_id))


async def _plans(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    limit = min(req.query_int("limit", 20), 100)
    return json_response({"ok": True, "plans": app.plans(limit=limit)})


async def _plan_pause(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    parts = req.path.strip("/").split("/")
    plan_id = parts[3] if len(parts) >= 5 else ""
    if not plan_id:
        raise HttpError(422, "usage: POST /api/v1/plans/{plan_id}/pause")
    orch = getattr(app, "orchestrator", None)
    if orch is None:
        raise HttpError(503, "orchestrator not available")
    return json_response(orch.pause_plan(plan_id))


async def _plan_resume(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    parts = req.path.strip("/").split("/")
    plan_id = parts[3] if len(parts) >= 5 else ""
    if not plan_id:
        raise HttpError(422, "usage: POST /api/v1/plans/{plan_id}/resume")
    orch = getattr(app, "orchestrator", None)
    if orch is None:
        raise HttpError(503, "orchestrator not available")
    return json_response(await orch.resume_plan(plan_id))


async def _orchestrator(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    orch = getattr(app, "orchestrator", None)
    if orch is None:
        return json_response({"ok": False, "error": "orchestrator not wired"})
    return json_response({"ok": True, "orchestrator": orch.describe(), "health": orch.health()})


async def _security(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    status_fn = getattr(app, "security_status", None)
    if callable(status_fn):
        return json_response(status_fn())
    sec = getattr(app, "security", None)
    if sec is not None and hasattr(sec, "describe"):
        return json_response(sec.describe())
    return json_response({"ok": True, "phase": 11, "stopped": getattr(app, "_stopped", False)})


async def _security_resume(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    res_fn = getattr(app, "resume", None)
    if callable(res_fn):
        return json_response(res_fn())
    return json_response({"ok": True, "stopped": False})


async def _security_scan(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    text = str(body.get("text") or "")
    scan_fn = getattr(app, "security_scan", None)
    if callable(scan_fn):
        return json_response(scan_fn(text))
    return json_response({"ok": True, "has_secrets": False, "secret_kinds": [], "redacted_text": text})


async def _audit(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    tool = req.query.get("tool")
    session_id = req.query.get("session_id")
    decision = req.query.get("decision")
    risk = req.query.get("risk")
    limit = min(req.query_int("limit", 50), 500)

    audit_fn = getattr(app, "security_audit", None)
    if callable(audit_fn):
        entries = audit_fn(
            tool=tool or None,
            session_id=session_id or None,
            decision=decision or None,
            risk=risk or None,
            limit=limit,
        )
        return json_response({"ok": True, "count": len(entries), "entries": entries})
    return json_response({"ok": True, "count": 0, "entries": []})


async def _stop(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    reason = "http"
    if req.body:
        try:
            body = json.loads(req.body.decode("utf-8"))
            reason = str(body.get("reason") or reason)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return json_response(await app.emergency_stop(reason=reason))


async def _ui(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    """Telemetry channel for the *existing* HUD (reactor state, log lines).

    The HUD stays the visible Star; it just tells the gateway what it is showing
    so the mission stream, audit log and ops console stay in sync.
    """
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    body.setdefault("type", "ui.state")
    return json_response(await app.handle_message(body))


async def _voice(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    """Voice verbs over plain HTTP (``voice.transcribe`` / ``voice.synthesize`` / …)."""
    body = req.json()
    if not isinstance(body, dict):
        raise HttpError(422, "body must be a JSON object")
    body.setdefault("type", "voice.synthesize" if body.get("text") and not body.get("audio_base64") else "voice.transcribe")
    return json_response(await app.handle_message(body))


async def _routes(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = (app, req)
    return json_response(
        {
            "ok": True,
            "routes": [
                {"method": route.method, "path": route.path, "description": route.description}
                for route in build_router(app).routes
            ],
        }
    )


async def _console(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = req
    from Backend.star.gateway.console import render_console

    return html_response(render_console(app.info()))


async def _robots(app: StarApplicationProtocol, req: HttpRequest) -> HttpResponse:
    _ = (app, req)
    return text_response("User-agent: *\nDisallow: /\n")


# ─────────────────────────────────────────────────────────────────────────────
#  Router
# ─────────────────────────────────────────────────────────────────────────────


class Router:
    """Exact-match first, then ``*`` segment wildcards (``/api/v1/tasks/*/cancel``)."""

    def __init__(self, app: StarApplicationProtocol, routes: list[Route]) -> None:
        self.app = app
        self.routes = routes
        self._index: dict[tuple[str, str], Route] = {(r.method, r.path): r for r in routes}
        self._wildcards: list[Route] = [r for r in routes if "*" in r.path]

    def match(self, method: str, path: str) -> Route | None:
        route = self._index.get((method, path))
        if route is not None:
            return route
        route_parts = path.strip("/").split("/")
        for candidate in self._wildcards:
            if candidate.method != method:
                continue
            pattern = candidate.path.strip("/").split("/")
            if len(pattern) != len(route_parts):
                continue
            if all(p == "*" or p == actual for p, actual in zip(pattern, route_parts, strict=True)):
                return candidate
        return None

    async def dispatch(self, req: HttpRequest) -> HttpResponse:
        route = self.match(req.method, req.path)
        if route is None:
            if any(r.path == req.path for r in self.routes):
                raise HttpError(405, f"method {req.method} not allowed for {req.path}")
            raise HttpError(404, f"no route for {req.method} {req.path}")
        return await route.handler(self.app, req)


def build_router(app: StarApplicationProtocol) -> Router:
    """The complete v1 surface. Additive only — never renumber or rename."""
    routes = [
        Route("GET", "/", _console, "developer ops console (diagnostics UI, not the Star HUD)"),
        Route("GET", "/healthz", _health, "liveness probe"),
        Route("GET", "/robots.txt", _robots, "disallow crawlers"),
        Route("GET", "/api/v1/health", _health, "health of every subsystem"),
        Route("GET", "/api/v1/info", _info, "build info, settings (secret-free), capabilities"),
        Route("GET", "/api/v1/status", _status, "live runtime status snapshot"),
        Route("GET", "/api/v1/routes", _routes, "this route table"),
        Route("POST", "/api/v1/chat", _chat, "send user text → plan → tasks → response"),
        Route("POST", "/api/v1/ui", _ui, "HUD telemetry mirror (reactor state / log lines)"),
        Route("POST", "/api/v1/voice", _voice, "voice verbs: transcribe / synthesize / barge-in / enrol"),
        Route("GET", "/api/v1/events", _events, "replay recent mission events (WS fallback)"),
        Route("GET", "/api/v1/agents", _agents, "registered agents and their responsibilities"),
        Route("GET", "/api/v1/tools", _tools, "typed tool registry incl. risk + permission metadata"),
        Route("GET", "/api/v1/tasks", _tasks, "recent tasks and plans"),
        Route("POST", "/api/v1/tasks/*/cancel", _cancel, "cancel a running task"),
        Route("GET", "/api/v1/plans", _plans, "recent plans and their task sequences"),
        Route("POST", "/api/v1/plans/*/pause", _plan_pause, "pause execution of an active plan"),
        Route("POST", "/api/v1/plans/*/resume", _plan_resume, "resume a paused plan from checkpoint"),
        Route("GET", "/api/v1/orchestrator", _orchestrator, "orchestrator state, queue, and checkpoints"),
        Route("GET", "/api/v1/memory", _memory, "memory layers + hybrid retrieval preview"),
        Route("POST", "/api/v1/memory", _remember, "store a fact/preference"),
        Route("GET", "/api/v1/learning", _learning, "safe learning loop: gates, candidates, feedback, promotions"),
        Route("POST", "/api/v1/learning", _learning, "record explicit feedback (the human half of the loop)"),
        Route("POST", "/api/v1/learning/cycle", _learning_cycle, "run one validation + promotion sweep"),
        Route("GET", "/api/v1/audit", _audit, "tail the append-only audit log"),
        Route("GET", "/api/v1/browser", _browser, "browser agent state (guardrails, last run)"),
        Route("POST", "/api/v1/browser", _browser, "run one browser goal: open / read / links / search"),
        Route("GET", "/api/v1/computer", _computer, "computer agent state (motor, guardrails, last run)"),
        Route("POST", "/api/v1/computer", _computer, "run one screen/mouse/keyboard goal (dry-run first)"),
        Route("GET", "/api/v1/workspaces", _workspaces, "background workspace manager + sessions"),
        Route("POST", "/api/v1/workspaces", _workspaces, "open an isolated background session"),
        Route("GET", "/api/v1/workspaces/*", _workspace_detail, "one session: files, checkpoints, quotas"),
        Route("POST", "/api/v1/workspaces/*/close", _workspace_close, "close a session (remove_files=true needs approval)"),
        Route("POST", "/api/v1/workspaces/*/checkpoint", _workspace_checkpoint, "snapshot a session's files"),
        Route("POST", "/api/v1/workspaces/*/restore", _workspace_restore, "restore a snapshot into a session"),
        Route("GET", "/api/v1/confirmations", _confirmations, "pending human confirmations"),
        Route("POST", "/api/v1/confirmations", _confirm, "approve/deny a pending confirmation"),
        Route("POST", "/api/v1/stop", _stop, "EMERGENCY STOP — cancel everything, mute motors"),
        Route("GET", "/api/v1/security", _security, "security policies, safety level, budgets, and state"),
        Route("POST", "/api/v1/security/resume", _security_resume, "resume from emergency stop"),
        Route("POST", "/api/v1/security/scan", _security_scan, "scan payload or text for credentials"),
    ]
    return Router(app, routes)
