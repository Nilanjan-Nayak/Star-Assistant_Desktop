"""Phase 1 — gateway integration tests (real sockets, ephemeral port)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from Backend.star.config.settings import Settings
from Backend.star.gateway.http import WSFrameDecoder, WSOpcode, ws_client_frame, ws_close_frame
from Backend.star.gateway.server import GatewayServer
from Backend.star.main import build_application
from Backend.star.observability.events import StarEventBus, reset_event_bus

# ── helpers ───────────────────────────────────────────────────────────────────


class FakeBrain:
    """Stands in for the Phase 3 brain so these tests stay deterministic."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(
        self, text: str, *, session_id: str | None = None, language: str | None = None, source: str = "text"
    ) -> dict[str, Any]:
        self.calls.append(text)
        return {
            "ok": True,
            "response": f"fake:{text}",
            "language": language or "bn",
            "source": "fake_brain",
            "session_id": session_id,
            "task_count": 1,
            "plan": {
                "plan_id": "plan_test0001",
                "intent": "demo",
                "rationale": "test plan",
                "requires_confirmation": False,
                "tasks": [
                    {
                        "task_id": "task_test0001",
                        "goal": text,
                        "agent": "conversation",
                        "state": "done",
                        "risk": "low",
                        "steps": [],
                        "summary": "answered",
                    }
                ],
            },
        }


async def _http(
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    host: str = "127.0.0.1",
    extra_headers: dict[str, str] | None = None,
    reader: asyncio.StreamReader | None = None,
    writer: asyncio.StreamWriter | None = None,
    close: bool = True,
) -> tuple[int, dict[str, str], Any, asyncio.StreamReader, asyncio.StreamWriter]:
    if reader is None or writer is None:
        reader, writer = await asyncio.open_connection(host, port)
    payload = json.dumps(body).encode() if body is not None else b""
    headers = {
        "Host": f"{host}:{port}",
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
        "Connection": "close" if close else "keep-alive",
        **(extra_headers or {}),
    }
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    writer.write(head.encode() + payload)
    await writer.drain()
    status, resp_headers, raw_body = await _read_response(reader)
    try:
        parsed: Any = json.loads(raw_body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = raw_body.decode(errors="replace")
    if close:
        writer.close()
    return status, resp_headers, parsed, reader, writer


async def _read_response(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = await reader.readexactly(length) if length else b""
    return status, headers, body


@pytest.fixture
async def gateway(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STAR_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("STAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STAR_WORKSPACE_ROOT", str(tmp_path / "data" / "workspace"))
    monkeypatch.setenv("STAR_AUDIT_PATH", str(tmp_path / "logs" / "audit.jsonl"))
    monkeypatch.setenv("STAR_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("STAR_EPISODES_PATH", str(tmp_path / "episodes.jsonl"))
    monkeypatch.setenv("STAR_PATTERNS_PATH", str(tmp_path / "patterns.jsonl"))
    monkeypatch.setenv("STAR_CANDIDATES_PATH", str(tmp_path / "learning" / "candidates.jsonl"))
    monkeypatch.setenv("STAR_FEEDBACK_PATH", str(tmp_path / "learning" / "feedback.jsonl"))
    monkeypatch.setenv("STAR_SESSION_ID", "test-session")
    reset_event_bus()
    settings = Settings.from_env(load_env_file=False)
    bus = StarEventBus(history_size=200)
    brain = FakeBrain()
    app = build_application(settings, bus=bus, brain=brain)
    await app.startup()
    server = GatewayServer(app, settings, host="127.0.0.1", port=0, bus=bus)
    await server.start()
    try:
        yield SimpleNamespace(app=app, server=server, bus=bus, brain=brain, settings=settings, port=server.port)
    finally:
        await server.stop()
        await app.aclose()
        reset_event_bus()


@pytest.fixture
async def brain_gateway(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Gateway with the real Phase 3 brain (memory + patterns redirected to tmp)."""
    monkeypatch.setenv("STAR_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("STAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STAR_WORKSPACE_ROOT", str(tmp_path / "data" / "workspace"))
    monkeypatch.setenv("STAR_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("STAR_PATTERNS_PATH", str(tmp_path / "patterns.jsonl"))
    monkeypatch.setenv("STAR_CANDIDATES_PATH", str(tmp_path / "learning" / "candidates.jsonl"))
    monkeypatch.setenv("STAR_FEEDBACK_PATH", str(tmp_path / "learning" / "feedback.jsonl"))
    monkeypatch.setenv("STAR_EPISODES_PATH", str(tmp_path / "episodes.jsonl"))
    monkeypatch.setenv("STAR_SESSION_ID", "brain-session")
    reset_event_bus()
    settings = Settings.from_env(load_env_file=False)
    bus = StarEventBus(history_size=200)
    app = build_application(settings, bus=bus)
    await app.startup()
    server = GatewayServer(app, settings, host="127.0.0.1", port=0, bus=bus)
    await server.start()
    try:
        yield SimpleNamespace(app=app, server=server, bus=bus, settings=settings, port=server.port)
    finally:
        await server.stop()
        await app.aclose()
        reset_event_bus()


# ── HTTP surface ──────────────────────────────────────────────────────────────


async def test_health_and_info(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/health")
    assert status == 200
    assert body["status"] in {"ok", "degraded"}
    assert body["dry_run"] is True
    assert body["checks"]["gateway"]["status"] == "ok"
    assert body["checks"]["brain"]["status"] == "ok"
    assert body["checks"]["security"]["status"] in {"ok", "degraded"}, "security surface lands in Phase 4"
    assert body["checks"]["tools"]["status"] == "ok", "typed tool registry lands in Phase 4"

    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/info")
    assert status == 200
    assert body["version"] == "2.0.0"
    assert body["protocol"] == "star.mission.v1"
    assert body["session_id"] == "test-session"
    assert "brain" in body["capabilities"]
    assert "security" in body["capabilities"] and "tools" in body["capabilities"]
    assert body["settings"]["secrets"]["GEMINI_API_KEY"] in {"<set>", "<unset>"}


async def test_console_is_served_at_root(gateway) -> None:
    status, headers, body, *_ = await _http(gateway.port, "GET", "/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert "STAR 2.0" in body
    assert "EMERGENCY STOP" in body
    assert "window.__STAR_INFO__" in body
    assert "http://" not in body.split("<style>")[0], "console must not pull external resources"


async def test_chat_round_trip_records_plan_and_tasks(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/chat", {"text": "volume 40 koro", "language": "bn"})
    assert status == 200
    assert body["response"] == "fake:volume 40 koro"
    assert body["language"] == "bn"
    assert body["latency_ms"] >= 0
    assert gateway.brain.calls == ["volume 40 koro"]
    assert gateway.app.plans()[0]["plan_id"] == "plan_test0001"
    assert gateway.app.tasks()[0]["agent"] == "conversation"


async def test_chat_validation(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/chat", {"text": ""})
    assert status == 422
    assert body["ok"] is False
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/chat", {"nope": 1})
    assert status == 422
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/chat", {"text": "x" * 5000})
    assert status == 413


async def test_unknown_route_and_method(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/does-not-exist")
    assert status == 404
    assert body["error"]["code"] == "not_found"
    status, _, body, *_ = await _http(gateway.port, "DELETE", "/api/v1/chat")
    assert status in {404, 405}


async def test_keep_alive_reuses_one_connection(gateway) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    status1, headers1, _, reader, writer = await _http(
        gateway.port, "GET", "/api/v1/health", reader=reader, writer=writer, close=False
    )
    assert status1 == 200
    assert headers1["connection"] == "keep-alive"
    status2, _, body2, reader, writer = await _http(
        gateway.port, "GET", "/api/v1/info", reader=reader, writer=writer, close=False
    )
    assert status2 == 200
    assert body2["name"] == "STAR 2.0"
    writer.close()


async def test_cors_headers_only_for_allowed_origins(gateway) -> None:
    _, headers, _, *_ = await _http(gateway.port, "GET", "/api/v1/health", extra_headers={"Origin": "https://x.dev"})
    assert headers.get("access-control-allow-origin") == "https://x.dev"
    _, headers, _, *_ = await _http(gateway.port, "GET", "/api/v1/health")
    assert "access-control-allow-origin" not in headers


async def test_events_endpoint_streams_mission_events(gateway) -> None:
    await _http(gateway.port, "POST", "/api/v1/chat", {"text": "hello star"})
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/events?limit=50")
    assert status == 200
    kinds = [event["kind"] for event in body["events"]]
    assert "request.received" in kinds
    assert "plan.created" in kinds or "task.completed" in kinds
    first_seq = body["events"][0]["seq"]
    _, _, body2, *_ = await _http(gateway.port, "GET", f"/api/v1/events?since={body['events'][-1]['seq']}")
    assert body2["ok"] is True
    assert all(event["seq"] > first_seq for event in body2["events"])


async def test_ui_telemetry_mirror_endpoint(gateway) -> None:
    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/ui", {"type": "ui.state", "state": "think", "text": "volume barao"}
    )
    assert status == 200
    assert body == {"ok": True, "mirrored": "ui.state"}
    kinds = [event.kind for event in gateway.bus.history(limit=20)]
    assert "task.progress" in kinds


async def test_emergency_stop_blocks_then_resumes(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/stop", {"reason": "test"})
    assert status == 200 and body["stopped"] is True
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/chat", {"text": "volume 10"})
    assert body["ok"] is False
    assert body["error"] == "emergency_stop_active"
    gateway.app.resume()
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/chat", {"text": "volume 10"})
    assert body["ok"] is True


async def test_pending_phase_endpoints_degrade_gracefully(gateway) -> None:
    for path in ("/api/v1/tools", "/api/v1/agents", "/api/v1/tasks", "/api/v1/workspaces", "/api/v1/audit?limit=5",
                 "/api/v1/confirmations"):
        status, _, body, *_ = await _http(gateway.port, "GET", path)
        assert status == 200, path
        assert body["ok"] is True, path
    # Phase 8 landed: /api/v1/memory is live, so it reports layers instead of "pending"
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/memory")
    assert status == 200 and body["ok"] is True and "pending" not in body
    assert set(body["layers"]) == {"working", "episodic", "semantic", "preference", "pattern"}
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/confirmations", {"confirmation_id": "x", "approve": True})
    assert status == 200 and body["ok"] is False and "unknown confirmation" in body["error"]


async def test_browser_endpoint_reports_the_agent_and_runs_a_dry_run_goal(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/browser")
    assert status == 200 and body["ok"] is True
    assert body["agent"]["name"] == "browser" and len(body["agent"]["tools"]) == 8
    assert body["settings"]["allowed_schemes"] == ["http", "https"]
    assert body["settings"]["allow_private_hosts"] is False and body["last_run"] is None

    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/browser", {"goal": "read https://example.com"})
    assert status == 200 and body["ok"] is True and body["state"] == "dry_run"
    assert body["steps"] and all(step["decision"] == "simulated" for step in body["steps"])
    assert "dry-run" in body["note"], "a simulated run must say so out loud"

    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/browser")
    assert body["runs"] == 1 and body["last_run"]["state"] == "dry_run"


async def test_browser_endpoint_blocks_a_forbidden_url_and_audits_it(gateway) -> None:
    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/browser", {"goal": "open file:///etc/passwd", "dry_run": False}
    )
    assert status == 200 and body["ok"] is False and body["state"] == "blocked"
    assert "guardrails" in body["error"] and body["steps"][0]["observation"]["block_rule"] == "scheme"

    status, _, audit, *_ = await _http(gateway.port, "GET", "/api/v1/audit?limit=30")
    assert status == 200
    assert any(entry["tool"] == "browser_open" for entry in audit["entries"]), "the refusal is audited like any action"


async def test_browser_endpoint_validation_and_emergency_stop(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/browser", {"goal": "   "})
    assert status == 422 and "goal" in body["error"]["message"]

    await _http(gateway.port, "POST", "/api/v1/stop", {"reason": "test"})
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/browser", {"goal": "read https://example.com"})
    assert status == 200 and body["ok"] is False and body["stopped"] is True
    gateway.app.resume()
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/browser", {"goal": "read https://example.com"})
    assert status == 200 and body["state"] == "dry_run"


async def test_route_table_documents_the_browser_surface(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/routes")
    assert status == 200
    paths = {(route["method"], route["path"]) for route in body["routes"]}
    assert ("GET", "/api/v1/browser") in paths and ("POST", "/api/v1/browser") in paths
    assert ("GET", "/api/v1/computer") in paths and ("POST", "/api/v1/computer") in paths
    assert ("GET", "/api/v1/workspaces") in paths and ("POST", "/api/v1/workspaces") in paths
    assert ("POST", "/api/v1/workspaces/*/close") in paths
    assert ("POST", "/api/v1/workspaces/*/checkpoint") in paths
    assert ("POST", "/api/v1/workspaces/*/restore") in paths
    assert ("GET", "/api/v1/learning") in paths and ("POST", "/api/v1/learning") in paths
    assert ("POST", "/api/v1/learning/cycle") in paths
    assert ("GET", "/api/v1/plans") in paths
    assert ("POST", "/api/v1/plans/*/pause") in paths
    assert ("POST", "/api/v1/plans/*/resume") in paths
    assert ("GET", "/api/v1/orchestrator") in paths
    assert ("GET", "/api/v1/security") in paths
    assert ("POST", "/api/v1/security/resume") in paths
    assert ("POST", "/api/v1/security/scan") in paths
    assert len(paths) == 41, "additive only — the surface must not shrink"


async def test_workspaces_endpoint_reports_the_manager(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/workspaces")
    assert status == 200 and body["ok"] is True and body["enabled"] is True
    assert body["isolation"]["backend"] == "null" and body["isolation"]["available"] is True
    assert "NOT hidden" in body["isolation"]["note"], "the console must not over-promise isolation"
    assert body["sessions_max"] == 4 and body["sessions_alive"] == 0 and body["workspaces"] == []
    assert body["quotas"]["max_files"] == 500 and body["checkpoints_enabled"] is True


async def test_workspaces_dry_run_records_the_intent(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/workspaces", {"kind": "files", "label": "demo"})
    assert status == 200 and body["ok"] is True and body["decision"] == "simulated"
    assert body["dry_run"] is True

    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/workspaces")
    assert body["sessions_alive"] == 0, "a dry-run must not create anything on disk"


async def test_workspaces_live_session_lifecycle(gateway) -> None:
    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/workspaces", {"kind": "coding", "label": "refactor", "dry_run": False}
    )
    assert status == 200 and body["decision"] == "executed"
    session_id = body["data"]["session_id"]
    path = Path(body["data"]["result"]["path"])
    assert path.is_dir() and (path / "_checkpoints").is_dir()

    status, _, detail, *_ = await _http(gateway.port, "GET", f"/api/v1/workspaces/{session_id}")
    assert status == 200 and detail["ok"] is True and detail["kind"] == "coding"
    assert detail["files_list"] == [] and detail["checkpoints"] == []

    status, _, body, *_ = await _http(
        gateway.port, "POST", f"/api/v1/workspaces/{session_id}/checkpoint", {"label": "v1", "dry_run": False}
    )
    assert status == 200 and body["decision"] == "executed"
    checkpoint_id = body["data"]["checkpoint"]["checkpoint_id"]
    assert body["data"]["state"] == "suspended"

    status, _, body, *_ = await _http(
        gateway.port, "POST", f"/api/v1/workspaces/{session_id}/restore", {"checkpoint_id": checkpoint_id, "dry_run": False}
    )
    assert status == 200 and body["decision"] == "executed" and body["data"]["state"] == "active"

    status, _, body, *_ = await _http(
        gateway.port, "POST", f"/api/v1/workspaces/{session_id}/close", {"dry_run": False}
    )
    assert status == 200 and body["decision"] == "executed" and body["risk"] == "medium"
    assert path.is_dir(), "closing keeps the files for inspection"

    status, _, body, *_ = await _http(
        gateway.port, "POST", f"/api/v1/workspaces/{session_id}/close", {"remove_files": True, "dry_run": False}
    )
    assert status == 200 and body["decision"] == "needs_confirmation" and body["risk"] == "high"
    assert path.is_dir(), "nothing is deleted before a human says yes"

    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/confirmations", {"confirmation_id": body["confirmation_id"], "approve": True}
    )
    assert status == 200 and body["ok"] is True and body["execution"]["decision"] == "executed"
    assert body["execution"]["destroyed"] is True and not path.exists()

    status, _, detail, *_ = await _http(gateway.port, "GET", f"/api/v1/workspaces/{session_id}")
    assert detail["state"] == "destroyed"

    status, _, audit, *_ = await _http(gateway.port, "GET", "/api/v1/audit?limit=60")
    tools = {entry["tool"] for entry in audit["entries"]}
    assert {"workspace_create", "workspace_close", "workspace_destroy"} <= tools
    assert any(entry["kind"] == "workspace.decision" for entry in audit["entries"])


async def test_workspaces_validation_and_unknown_sessions(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/workspaces/ws_nope")
    assert status == 200 and body["ok"] is False and "unknown workspace session" in body["error"]

    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/workspaces/*/close", {})
    assert status == 422 and "session_id" in body["error"]["message"]

    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/workspaces", "not-an-object")
    assert status == 422

    await _http(gateway.port, "POST", "/api/v1/stop", {"reason": "test"})
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/workspaces", {"kind": "files", "dry_run": False})
    assert status == 200 and body["ok"] is False and body["stopped"] is True
    gateway.app.resume()


async def test_computer_endpoint_reports_the_motor_and_guardrails(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/computer")
    assert status == 200 and body["ok"] is True
    assert body["motor"]["mode"] == "null" and "nothing moves" in body["motor"]["note"]
    assert body["guardrails"]["screen"] == "1920x1080"
    assert {"total_budget", "rate_limit", "wall_clock", "forbidden_region", "keyword_blocklist", "capability"} <= set(
        body["guardrails"]["governor_invariants"]
    )
    assert body["settings"]["screen"] == "1920x1080" and body["dry_run"] is True
    assert body["last_run"] is None


async def test_computer_endpoint_dry_run_records_the_intent(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/computer", {"goal": "click at 480,320"})
    assert status == 200 and body["ok"] is True and body["state"] == "dry_run"
    assert [step["action"] for step in body["steps"]] == ["move", "click", "capture"]
    assert all(step["decision"] == "simulated" for step in body["steps"])
    assert "did not move" in body["note"], "a dry-run must say so out loud"
    assert "480" in body["steps"][1]["verification_note"], "the human sees the intended coordinate"

    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/computer")
    assert body["runs"] == 1 and body["last_run"]["state"] == "dry_run"
    assert body["motor"]["actions"] == 0, "dry-run never reached the motor"


async def test_computer_endpoint_blocks_unsafe_goals(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/computer", {"goal": "click at 9000,9000"})
    assert status == 200 and body["ok"] is False and body["state"] == "blocked"
    assert body["steps"][0]["observation"]["block_rule"] == "bounds"

    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/computer", {"goal": "hotkey ctrl+alt+del", "dry_run": False}
    )
    assert status == 200 and body["state"] == "blocked" and "blocked_hotkey" in body["error"]

    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/computer", {"goal": "type 'sudo rm -rf / now'", "dry_run": False}
    )
    assert status == 200 and body["state"] == "blocked" and "destructive_text" in body["error"]

    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/computer")
    refusals = body["guardrails"]["refusals"]
    assert refusals and {entry["rule"] for entry in refusals} >= {"bounds", "blocked_hotkey", "destructive_text"}


async def test_computer_endpoint_parks_on_confirmation(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/computer", {"goal": "hotkey alt+tab", "dry_run": False})
    assert status == 200 and body["ok"] is False and body["state"] == "waiting_confirmation"
    confirmation_id = body["confirmation_id"]
    assert confirmation_id

    status, _, listed, *_ = await _http(gateway.port, "GET", "/api/v1/confirmations")
    assert status == 200 and listed["ok"] is True
    assert any(item["confirmation_id"] == confirmation_id for item in listed["confirmations"])

    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/confirmations", {"confirmation_id": confirmation_id, "approve": True}
    )
    assert status == 200 and body["ok"] is True and body["execution"]["decision"] == "executed"

    status, _, state, *_ = await _http(gateway.port, "GET", "/api/v1/computer")
    assert state["motor"]["actions"] >= 1, "after approval the null motor recorded the action"


async def test_computer_endpoint_validation_and_bengali_digits(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/computer", {"goal": "   "})
    assert status == 422 and "goal" in body["error"]["message"]

    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/computer", {"goal": "৪৮০,৩২০ এ ক্লিক করো"})
    assert status == 200 and body["state"] == "dry_run"
    assert {"x": 480, "y": 320} in [step["arguments"] for step in body["steps"]]

    await _http(gateway.port, "POST", "/api/v1/stop", {"reason": "test"})
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/computer", {"goal": "take a screenshot"})
    assert status == 200 and body["ok"] is False and body["stopped"] is True
    gateway.app.resume()


# ── WebSocket surface ─────────────────────────────────────────────────────────


async def _ws_connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, WSFrameDecoder]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        (
            f"GET /ws?session_id=ws-test HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    assert b"101 Switching Protocols" in head
    assert b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in head
    return reader, writer, WSFrameDecoder()


async def _ws_read_messages(
    reader: asyncio.StreamReader, decoder: WSFrameDecoder, *, count: int = 1, timeout: float = 8.0
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while len(messages) < count and asyncio.get_running_loop().time() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=max(0.2, deadline - asyncio.get_running_loop().time()))
        except TimeoutError:
            break
        if not chunk:
            break
        for frame in decoder.feed(chunk):
            if frame.opcode == WSOpcode.TEXT:
                messages.append(json.loads(frame.text))
            elif frame.opcode == WSOpcode.CLOSE:
                return messages
    return messages


async def test_websocket_hello_chat_and_events(gateway) -> None:
    reader, writer, decoder = await _ws_connect(gateway.port)
    try:
        await _ws_body(gateway, reader, writer, decoder)
    finally:
        with contextlib.suppress(Exception):
            writer.write(ws_close_frame())
            await writer.drain()
        writer.close()


async def _ws_body(gateway, reader, writer, decoder) -> None:
    hello = (await _ws_read_messages(reader, decoder, count=1))[0]
    assert hello["type"] == "hello"
    assert hello["protocol"] == "star.mission.v1"
    assert hello["session_id"] == "ws-test"
    assert hello["info"]["version"] == "2.0.0"

    writer.write(ws_client_frame(json.dumps({"type": "chat", "text": "play lo-fi", "language": "en"}).encode()))
    await writer.drain()
    messages = await _ws_read_messages(reader, decoder, count=6, timeout=8)
    results = [m for m in messages if m.get("type") == "result"]
    events = [m["event"] for m in messages if m.get("type") == "event"]
    assert results and results[0]["response"] == "fake:play lo-fi"
    assert results[0]["language"] == "en"
    assert events, "the mission stream must flow over the socket"
    assert {event["kind"] for event in events} & {"request.received", "plan.created", "task.completed"}

    writer.write(ws_client_frame(json.dumps({"type": "ping"}).encode()))
    await writer.drain()
    pong = [m for m in await _ws_read_messages(reader, decoder, count=3, timeout=5) if m.get("type") == "ack"]
    assert pong and pong[0]["pong"] is True

    writer.write(ws_client_frame(json.dumps({"type": "subscribe", "kinds": ["task.*"]}).encode()))
    await writer.drain()
    ack = [m for m in await _ws_read_messages(reader, decoder, count=2, timeout=5) if m.get("type") == "ack"]
    assert ack and ack[0]["kinds"] == ["task.*"]

    writer.write(ws_client_frame(json.dumps({"type": "bogus.verb"}).encode()))
    await writer.drain()
    replies = await _ws_read_messages(reader, decoder, count=2, timeout=5)
    unknown = [m for m in replies if m.get("type") in {"error", "result"}]
    assert unknown and "unknown message type" in str(unknown[0].get("error"))


async def test_websocket_rejects_invalid_json(gateway) -> None:
    reader, writer, decoder = await _ws_connect(gateway.port)
    try:
        await _ws_read_messages(reader, decoder, count=1)
        writer.write(ws_client_frame(b"{not json"))
        await writer.drain()
        messages = await _ws_read_messages(reader, decoder, count=2, timeout=5)
        assert any(m.get("type") == "error" and "invalid JSON" in m["error"] for m in messages)
    finally:
        with contextlib.suppress(Exception):
            writer.write(ws_close_frame())
            await writer.drain()
        writer.close()


async def test_chat_result_carries_the_language_profile(gateway) -> None:
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/chat", {"text": "hey star, volume 45 koro"})
    assert status == 200
    profile = body["language_profile"]
    assert profile["code"] == "bn" and profile["banglish"] is True
    assert body["language"] == "bn"
    kinds = [event["kind"] for event in gateway.app.event_history(limit=50)]
    assert "language.detected" in kinds


async def test_chat_returns_a_full_brain_result(brain_gateway) -> None:
    """Phase 3: a gateway reply now carries context, plan, tasks and reflection."""
    status, _, body, *_ = await _http(brain_gateway.port, "POST", "/api/v1/chat", {"text": "tomar nam ki?"})
    assert status == 200 and body["ok"] is True
    assert body["source"].startswith("brain:")
    assert body["response"]
    assert body["language_profile"]["code"] == "bn"
    assert body["plan"]["task_count"] >= 1
    assert body["tasks"][0]["agent"] == "conversation"
    assert body["tasks"][0]["state"] == "done"
    assert body["reflection"]["verdict"] in {
        "success",
        "partial",
        "failed",
        "no_action",
        "blocked",
        "awaiting_confirmation",
        "unknown",
    }
    assert isinstance(body["predictions"], list)
    assert body["context"]["dry_run"] is True
    assert body["events_published"] is True
    assert body["reasoning"]["used_llm"] in {True, False}

    kinds = [event["kind"] for event in brain_gateway.app.event_history(limit=200)]
    assert "brain.turn_started" in kinds and "brain.turn_completed" in kinds
    assert "context.built" in kinds and "reasoning.complete" in kinds and "reflection.result" in kinds
    assert kinds.index("context.built") < kinds.index("plan.created"), "memory is retrieved before planning"

    # the second turn sees the first one through working + episodic memory
    status, _, second, *_ = await _http(brain_gateway.port, "POST", "/api/v1/chat", {"text": "tumi ki koro?"})
    assert status == 200 and second["ok"] is True
    assert second["context"]["working_memory"], "session scratch memory is kept"
    assert (tmp_memory := (brain_gateway.settings.paths.memory_db)).exists() or tmp_memory is None


async def test_voice_endpoint_synthesises_and_barge_in(gateway) -> None:
    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/voice", {"type": "voice.synthesize", "text": "ভলিউম ঠিক করে দিলাম"}
    )
    assert status == 200
    assert body["ok"] in {True, False}, "ok depends on whether edge-tts is installed"
    assert "synthesis" in body

    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/voice", {"type": "voice.transcribe", "text": "hey star, brightness 70 koro"}
    )
    assert status == 200 and body["ok"] is True
    assert body["transcript"]["text"].startswith("<redacted:"), "transcript retention is off by default"

    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/voice", {"type": "voice.barge_in"})
    assert status == 200 and body["ok"] is True and body["generation"] >= 1

    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/voice", {"type": "voice.status"})
    assert status == 200 and body["ok"] is True and "stt" in body and "tts" in body


async def test_server_snapshot_and_metrics(gateway) -> None:
    await _http(gateway.port, "GET", "/api/v1/health")
    snapshot = gateway.server.snapshot()
    assert snapshot["running"] is True
    assert snapshot["requests"] >= 1
    assert snapshot["port"] == gateway.port
    assert snapshot["url"].startswith("http://")
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/status")
    assert status == 200
    assert body["tasks_total"] >= 0
    assert "gateway.request.ms" in str(body["metrics"]), body["metrics"]


async def test_security_endpoints_status_resume_scan_and_audit(gateway) -> None:
    # 1. GET /api/v1/security
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/security")
    assert status == 200 and body["ok"] is True
    assert body["phase"] in (4, 11)
    assert "safety_level" in body
    assert "budget" in body

    # 2. POST /api/v1/security/scan
    status, _, body, *_ = await _http(
        gateway.port, "POST", "/api/v1/security/scan", {"text": "api token: sk-abcdefghijklmnopqrstuvwxyz12345"}
    )
    assert status == 200 and body["ok"] is True
    assert body["has_secrets"] is True
    assert "[REDACTED" in body["redacted_text"]

    # 3. POST /api/v1/stop & POST /api/v1/security/resume
    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/stop", {"reason": "test_e_stop"})
    assert status == 200 and body["ok"] is True and body["stopped"] is True

    status, _, body, *_ = await _http(gateway.port, "POST", "/api/v1/security/resume")
    assert status == 200 and body["ok"] is True and body["stopped"] is False

    # 4. GET /api/v1/audit with filters
    status, _, body, *_ = await _http(gateway.port, "GET", "/api/v1/audit?limit=10")
    assert status == 200 and body["ok"] is True
    assert "entries" in body
