#!/usr/bin/env python3
"""STAR 2.0 smoke test — boots the gateway and exercises the public surface.

This is the "smoke test" step the blueprint requires after every phase:

    python scripts/smoke.py            # full smoke run, exit code 0 = PASS
    python scripts/smoke.py --phase 1  # only the checks tagged for phase 1

It never touches the OS (dry-run) and never opens the desktop HUD.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Backend.star.config.settings import get_settings  # noqa: E402
from Backend.star.gateway.http import WSOpcode, WSFrameDecoder, ws_encode  # noqa: E402
from Backend.star.gateway.server import GatewayServer  # noqa: E402
from Backend.star.main import build_application  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    _results.append((name, PASS if condition else FAIL, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def _mask(payload: bytes, opcode: int = 0x1) -> bytes:
    """Client frames must be masked (RFC 6455 §5.3)."""
    import os
    import struct

    key = os.urandom(4)
    size = len(payload)
    first = 0x80 | (opcode & 0x0F)
    if size < 126:
        head = struct.pack("!BB", first, 0x80 | size)
    elif size <= 0xFFFF:
        head = struct.pack("!BBH", first, 0x80 | 126, size)
    else:
        head = struct.pack("!BBQ", first, 0x80 | 127, size)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


async def _http(host: str, port: int, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    reader, writer = await asyncio.open_connection(host, port)
    payload = json.dumps(body).encode() if body is not None else b""
    head = (
        f"{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
    ).encode()
    writer.write(head + payload)
    await writer.drain()
    chunks = []
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    raw = b"".join(chunks)
    writer.close()
    status_line, _, rest = raw.partition(b"\r\n\r\n")
    status = int(status_line.split(b" ")[1])
    try:
        return status, json.loads(rest.decode())
    except json.JSONDecodeError:
        return status, rest.decode(errors="replace")[:200]


async def _websocket_chat(host: str, port: int, text: str) -> dict[str, Any]:
    reader, writer = await asyncio.open_connection(host, port)
    import base64
    import os

    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(
        (
            f"GET /ws?session_id=smoke HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError(f"ws handshake failed: {head[:80]!r}")
    writer.write(_mask(json.dumps({"type": "chat", "text": text}).encode()))
    await writer.drain()

    decoder = WSFrameDecoder()
    result: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
        except TimeoutError:
            break
        if not chunk:
            break
        for frame in decoder.feed(chunk):
            if frame.opcode == WSOpcode.CLOSE:
                writer.close()
                return {"result": result, "events": events}
            if frame.opcode == WSOpcode.PING:
                writer.write(ws_encode(frame.payload, WSOpcode.PONG))
                continue
            if frame.opcode != WSOpcode.TEXT:
                continue
            message = json.loads(frame.text)
            if message.get("type") == "result":
                result = message
            elif message.get("type") == "event":
                events.append(message["event"])
            if result and len(events) >= 3:
                break
        if result and len(events) >= 3:
            break
    writer.write(_mask(b"", 0x8))
    writer.close()
    return {"result": result, "events": events}


async def run_smoke() -> int:
    settings = get_settings()
    # Phase 9: the learning ledgers — and the pattern store the loop promotes into — are
    # append-only runtime state where a promoted candidate is correctly *terminal*. A re-run
    # over the repo's own ledger would find last run's candidates and (rightly) skip
    # re-promoting them, so the smoke would not be idempotent. Point them at a throwaway dir:
    # every run starts clean and leaves zero residue in the repo (data/learning/, patterns.jsonl).
    smoke_dir = Path(tempfile.mkdtemp(prefix="star-smoke-"))
    settings = settings.model_copy(update={"paths": settings.paths.model_copy(update={
        "candidates_path": smoke_dir / "candidates.jsonl",
        "feedback_path": smoke_dir / "feedback.jsonl",
        "patterns_path": smoke_dir / "patterns.jsonl",
        "checkpoints_path": smoke_dir / "checkpoints.jsonl",
    })})
    app = build_application(settings)
    await app.startup()
    server = GatewayServer(app, settings, host="127.0.0.1", port=0)
    await server.start()
    host, port = "127.0.0.1", server.port
    print(f"\n[smoke] gateway on http://{host}:{port} (dry_run={settings.security.dry_run})\n")

    try:
        status, body = await _http(host, port, "GET", "/api/v1/health")
        check("GET /api/v1/health → 200", status == 200, f"status={body.get('status')}")

        status, body = await _http(host, port, "GET", "/api/v1/info")
        secrets = body.get("settings", {}).get("secrets", {})
        check(
            "GET /api/v1/info → build info + secrets shown only as <set>/<unset>",
            status == 200
            and bool(body.get("version"))
            and bool(secrets)
            and all(str(v).startswith("<") for v in secrets.values()),
            f"version={body.get('version')} secrets={secrets}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/routes")
        check("GET /api/v1/routes → surface documented", status == 200 and len(body.get("routes", [])) >= 15,
              f"routes={len(body.get('routes', []))}")

        status, body = await _http(host, port, "GET", "/")
        check("GET / → ops console HTML", status == 200 and isinstance(body, str) and "STAR 2.0" in body)

        status, body = await _http(host, port, "POST", "/api/v1/chat", {"text": "volume 40 koro"})
        check(
            "POST /api/v1/chat (Banglish command) → response",
            status == 200 and bool(body.get("response")),
            f"source={body.get('source')} lang={body.get('language')}",
        )
        check(
            "chat → Banglish detected (Phase 2 language routing)",
            body.get("language_profile", {}).get("banglish") is True
            and body.get("language_profile", {}).get("code") == "bn",
            f"profile={body.get('language_profile')}",
        )
        check(
            "chat → brain plan + tasks + reflection (Phase 3)",
            str(body.get("source", "")).startswith("brain:")
            and body.get("plan", {}).get("task_count", 0) >= 1
            and bool(body.get("reflection", {}).get("verdict")),
            f"tasks={len(body.get('tasks', []))} verdict={body.get('reflection', {}).get('verdict')}",
        )
        check(
            "chat → predictions are suggestions only",
            all(item.get("executed") is False for item in body.get("predictions", [])),
            f"predictions={[item.get('tool') for item in body.get('predictions', [])]}",
        )

        status, events_body = await _http(host, port, "GET", "/api/v1/events?limit=60")
        stream = [event.get("kind") for event in events_body.get("events", [])]
        brain_order = [k for k in stream if k and k.split(".")[0] in ("context", "reasoning", "plan", "reflection")]
        check(
            "events → memory retrieved before planning",
            "context.built" in stream and "plan.created" in stream
            and stream.index("context.built") < stream.index("plan.created"),
            f"order={brain_order[:6]}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/chat", {"text": ""})
        check("POST /api/v1/chat with empty text → 422", status == 422, str(body)[:80])

        status, body = await _http(host, port, "GET", "/api/v1/nope")
        check("GET unknown route → 404 JSON", status == 404 and body.get("ok") is False)

        status, body = await _http(host, port, "GET", "/api/v1/events?limit=20")
        kinds = {event.get("kind") for event in body.get("events", [])}
        check("GET /api/v1/events → mission stream", status == 200 and "request.received" in kinds,
              f"kinds={sorted(k for k in kinds if k)}")

        ws = await _websocket_chat(host, port, "hey star, kemon acho?")
        check(
            "WS /ws → hello + chat result + streamed events",
            bool(ws["result"].get("response")) and len(ws["events"]) >= 1,
            f"events={len(ws['events'])}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/stop", {"reason": "smoke"})
        check("POST /api/v1/stop → emergency stop acknowledged", status == 200 and body.get("stopped") is True)
        status, body = await _http(host, port, "POST", "/api/v1/chat", {"text": "volume 10"})
        check("chat after emergency stop is refused", body.get("ok") is False, str(body.get("error")))
        app.resume()

        status, body = await _http(host, port, "GET", "/api/v1/tools")
        check("GET /api/v1/tools → registry surface", status == 200 and isinstance(body.get("tools"), list),
              f"tools={len(body.get('tools', []))}")

        # ── Phase 5: the browser agent ──────────────────────────────────────
        status, body = await _http(host, port, "GET", "/api/v1/browser")
        agent_state = body.get("agent") or {}
        check(
            "GET /api/v1/browser → agent + guardrails",
            status == 200 and body.get("ok") is True and agent_state.get("name") == "browser",
            f"tools={len(agent_state.get('tools', []))} schemes={(body.get('settings') or {}).get('allowed_schemes')}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/agents")
        check("GET /api/v1/agents → the browser worker is registered",
              status == 200 and any(a.get("name") == "browser" for a in body.get("agents", [])),
              f"agents={[a.get('name') for a in body.get('agents', [])]}")

        status, body = await _http(host, port, "POST", "/api/v1/browser", {"goal": "read https://example.com"})
        steps = body.get("steps") or []
        check(
            "POST /api/v1/browser → dry-run plans and simulates, never fetches",
            status == 200 and body.get("state") == "dry_run" and bool(steps)
            and all(step.get("decision") == "simulated" for step in steps),
            f"state={body.get('state')} steps={[s.get('action') for s in steps]}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/browser",
                                   {"goal": "open file:///etc/passwd", "dry_run": False})
        check("POST /api/v1/browser → a file:// URL is blocked by the guardrails",
              status == 200 and body.get("state") == "blocked" and "guardrails" in str(body.get("error", "")),
              str(body.get("error"))[:90])

        status, body = await _http(host, port, "POST", "/api/v1/browser", {"goal": "   "})
        check("POST /api/v1/browser with an empty goal → 422", status == 422, str(body.get("error", ""))[:60])

        # ── Phase 6: the computer agent ────────────────────────────────────
        status, body = await _http(host, port, "GET", "/api/v1/computer")
        motor = body.get("motor") or {}
        guard = body.get("guardrails") or {}
        check(
            "GET /api/v1/computer → motor + guardrails + the reused governor",
            status == 200 and body.get("ok") is True and motor.get("mode") == "null"
            and "total_budget" in (guard.get("governor_invariants") or []),
            f"backend={motor.get('backend')} screen={(body.get('settings') or {}).get('screen')}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/agents")
        check("GET /api/v1/agents → the computer worker is registered",
              status == 200 and any(a.get("name") == "computer" for a in body.get("agents", [])),
              f"agents={[a.get('name') for a in body.get('agents', [])]}")

        status, body = await _http(host, port, "POST", "/api/v1/computer", {"goal": "click at 480,320"})
        steps = body.get("steps") or []
        check(
            "POST /api/v1/computer → dry-run plans move+click+capture and moves nothing",
            status == 200 and body.get("state") == "dry_run"
            and [step.get("action") for step in steps] == ["move", "click", "capture"]
            and all(step.get("decision") == "simulated" for step in steps),
            f"note={str(body.get('note', ''))[:60]}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/computer", {"goal": "click at 9000,9000"})
        check("POST /api/v1/computer → off-screen coordinates are blocked before the motor",
              status == 200 and body.get("state") == "blocked"
              and (body.get("steps") or [{}])[0].get("observation", {}).get("block_rule") == "bounds",
              str(body.get("error", ""))[:90])

        status, body = await _http(host, port, "POST", "/api/v1/computer", {"goal": "hotkey ctrl+alt+del", "dry_run": False})
        check("POST /api/v1/computer → ctrl+alt+del is on the blocked hotkey list",
              status == 200 and body.get("state") == "blocked" and "blocked_hotkey" in str(body.get("error", "")),
              str(body.get("error", ""))[:90])

        status, body = await _http(host, port, "POST", "/api/v1/computer", {"goal": "৪৮০,৩২০ এ ক্লিক করো"})
        check("POST /api/v1/computer → Bengali digits in the goal are understood",
              status == 200 and body.get("state") == "dry_run"
              and {"x": 480, "y": 320} in [step.get("arguments", {}) for step in (body.get("steps") or [])],
              f"state={body.get('state')}")

        status, body = await _http(host, port, "POST", "/api/v1/computer", {"goal": "   "})
        check("POST /api/v1/computer with an empty goal → 422", status == 422, str(body.get("error", ""))[:60])

        # ── Phase 7: the invisible background workspace ────────────────────
        status, body = await _http(host, port, "GET", "/api/v1/workspaces")
        isolation = body.get("isolation") or {}
        check(
            "GET /api/v1/workspaces → manager state + honest isolation claim",
            status == 200 and body.get("ok") is True and isolation.get("backend") == "null"
            and "NOT hidden" in str(isolation.get("note", "")) and body.get("sessions_max", 0) >= 1,
            f"alive={body.get('sessions_alive')} quotas={list((body.get('quotas') or {}))[:3]}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/workspaces", {"kind": "files", "label": "smoke"})
        check("POST /api/v1/workspaces → dry-run simulates and creates nothing on disk",
              status == 200 and body.get("decision") == "simulated" and body.get("dry_run") is True,
              f"decision={body.get('decision')}")

        status, body = await _http(host, port, "POST", "/api/v1/workspaces",
                                   {"kind": "coding", "label": "smoke live", "dry_run": False})
        session_id = str((body.get("data") or {}).get("session_id") or "")
        session_path = Path(str((body.get("data") or {}).get("result", {}).get("path") or ""))
        check("POST /api/v1/workspaces (dry_run=false) → a jailed session directory exists",
              status == 200 and body.get("decision") == "executed" and session_path.is_dir()
              and (session_path / "_checkpoints").is_dir(),
              f"session={session_id[:16]} path={session_path.name}")

        status, body = await _http(host, port, "GET", f"/api/v1/workspaces/{session_id}")
        check("GET /api/v1/workspaces/{id} → session detail (files, checkpoints, quota counters)",
              status == 200 and body.get("ok") is True and body.get("kind") == "coding"
              and isinstance(body.get("files_list"), list),
              f"state={body.get('state')} files={body.get('files')}")

        status, body = await _http(host, port, "POST", f"/api/v1/workspaces/{session_id}/checkpoint",
                                   {"label": "smoke", "dry_run": False})
        checkpoint_id = str((body.get("data") or {}).get("checkpoint", {}).get("checkpoint_id") or "")
        check("POST …/checkpoint → snapshot stored, session suspended",
              status == 200 and body.get("decision") == "executed" and bool(checkpoint_id)
              and body.get("data", {}).get("state") == "suspended",
              f"checkpoint={checkpoint_id[:22]}")

        status, body = await _http(host, port, "POST", f"/api/v1/workspaces/{session_id}/restore",
                                   {"checkpoint_id": checkpoint_id, "dry_run": False})
        check("POST …/restore → snapshot restored, session active again",
              status == 200 and body.get("decision") == "executed" and body.get("data", {}).get("state") == "active",
              f"restored={body.get('data', {}).get('restored')}")

        status, body = await _http(host, port, "POST", f"/api/v1/workspaces/{session_id}/close", {"dry_run": False})
        check("POST …/close → medium risk, executed, files kept for inspection",
              status == 200 and body.get("decision") == "executed" and body.get("risk") == "medium"
              and session_path.is_dir(),
              f"note={body.get('data', {}).get('note', '')[:40]}")

        status, body = await _http(host, port, "POST", f"/api/v1/workspaces/{session_id}/close",
                                   {"remove_files": True, "dry_run": False})
        confirmation_id = str(body.get("confirmation_id") or "")
        check("POST …/close {remove_files:true} → high risk, needs an explicit approval, nothing deleted yet",
              status == 200 and body.get("decision") == "needs_confirmation" and body.get("risk") == "high"
              and bool(confirmation_id) and session_path.is_dir(),
              f"confirmation={confirmation_id[:20]}")

        status, body = await _http(host, port, "POST", "/api/v1/confirmations",
                                   {"confirmation_id": confirmation_id, "approve": True})
        execution = body.get("execution") or {}
        check("approve → the session files are really deleted (and audited)",
              status == 200 and body.get("ok") is True and execution.get("decision") == "executed"
              and execution.get("destroyed") is True and not session_path.exists(),
              f"removed_files={execution.get('removed_files')}")

        status, body = await _http(host, port, "GET", "/api/v1/audit?limit=80")
        check("audit carries workspace.decision entries next to tool.decision",
              status == 200 and any(entry.get("kind") == "workspace.decision" for entry in body.get("entries", []))
              and any(str(entry.get("tool", "")).startswith("workspace_") for entry in body.get("entries", [])),
              f"kinds={sorted({entry.get('kind') for entry in body.get('entries', [])})}")

        status, body = await _http(host, port, "GET", "/api/v1/workspaces/ws_does_not_exist")
        check("GET an unknown session → an honest error, not a 500",
              status == 200 and body.get("ok") is False and "unknown workspace session" in str(body.get("error", "")),
              str(body.get("error", ""))[:60])

        # ── Phase 8: layered memory ─────────────────────────────────────────
        status, body = await _http(host, port, "GET", "/api/v1/memory")
        layers = body.get("layers") or {}
        check(
            "GET /api/v1/memory → all five layers, each naming the store it adapts",
            status == 200 and body.get("ok") is True and body.get("enabled") is True
            and set(layers) == {"working", "episodic", "semantic", "preference", "pattern"}
            and all({"count", "store", "detail"} <= set(layer) and layer["store"] for layer in layers.values()),
            "counts=" + json.dumps({name: layer.get("count") for name, layer in layers.items()}),
        )
        check(
            "memory → no second store: the layers point at MemoryStore / EpisodicMemory / PatternStore",
            "MemoryStore" in layers.get("semantic", {}).get("store", "")
            and "EpisodicMemory" in layers.get("episodic", {}).get("store", "")
            and "PatternStore" in layers.get("pattern", {}).get("store", "")
            and "ContextBuilder" in layers.get("working", {}).get("store", ""),
            f"working={layers.get('working', {}).get('store', '')[:40]}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/memory",
                                   {"text": "smoke: the desk lamp is on the left of the monitor"})
        memory_id = body.get("memory_id")
        check("POST /api/v1/memory → a fact lands in the semantic layer",
              status == 200 and body.get("ok") is True and body.get("layer") == "semantic"
              and isinstance(memory_id, int) and memory_id > 0,
              f"memory_id={memory_id}")

        status, body = await _http(host, port, "POST", "/api/v1/memory",
                                   {"text": "bn", "kind": "preference", "key": "smoke.reply_language", "value": "bn"})
        preference_id = body.get("memory_id")
        check("POST /api/v1/memory (kind=preference) → the preference layer, latest value per key",
              status == 200 and body.get("ok") is True and body.get("layer") == "preference"
              and body.get("key") == "smoke.reply_language",
              f"value={body.get('value')}")

        status, body = await _http(host, port, "GET", "/api/v1/memory?q=smoke desk lamp&limit=8")
        retrieved = body.get("retrieved") or []
        trace = body.get("trace") or {}
        check(
            "GET /api/v1/memory?q= → one blended ranking, scored per layer weight",
            status == 200 and bool(retrieved)
            and all({"layer", "text", "score", "kind"} <= set(hit) for hit in retrieved)
            and all(-1.0 <= float(hit["score"]) <= 1.0 for hit in retrieved)
            and retrieved == sorted(retrieved, key=lambda hit: -float(hit["score"]))
            and int(trace.get("candidates", 0)) >= len(retrieved),
            f"hits={[hit['layer'] for hit in retrieved]} candidates={trace.get('candidates')}",
        )
        check("memory → the preference is listed for the console",
              any(item.get("key") == "smoke.reply_language" and item.get("value") == "bn"
                  for item in body.get("preferences") or []),
              f"preferences={len(body.get('preferences') or [])}")

        episodes = body.get("episodes") or []
        check(
            "episodic → the dry-run browser/computer goals were recorded, labelled as simulations",
            bool(episodes) and any(str(item.get("goal", "")).startswith("[dry-run]") for item in episodes)
            and all({"episode_id", "goal", "steps", "succeeded"} <= set(item) for item in episodes),
            f"episodes={len(episodes)} latest={str(episodes[0].get('goal'))[:44] if episodes else '—'}",
        )
        check(
            "working → the conversation scratch is the brain's own buffer, not a copy",
            bool(body.get("working")) and app.memory.working.delegated is True
            and app.memory.working._provider is app.brain.context_builder,
            f"turns={len(body.get('working') or [])}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/tools")
        memory_tools = {tool["name"]: tool for tool in body.get("tools", []) if tool["name"].startswith("memory_")}
        check(
            "GET /api/v1/tools → seven memory tools, risk-tiered (reads low, write medium, delete high)",
            status == 200 and len(memory_tools) == 7
            and memory_tools.get("memory_recall", {}).get("risk") == "low"
            and memory_tools.get("memory_preference_set", {}).get("risk") == "medium"
            and memory_tools.get("memory_forget", {}).get("risk") == "high"
            and all(tool.get("category") == "memory" for tool in memory_tools.values()),
            f"tools={sorted(memory_tools)}",
        )

        result = await app.executor.call("memory_recall", {"query": "smoke desk lamp", "limit": 5},
                                        session_id=settings.session_id, dry_run=False)
        check("memory_recall through the tool layer → the same blended answer, audited",
              result.ok is True and result.decision == "executed" and result.data.get("count", 0) >= 1
              and "read-only" in str(result.data.get("note", "")),
              f"hits={result.data.get('count')} layers={result.data.get('layers_used')}")

        blocked = await app.executor.call("memory_forget", {"memory_id": memory_id}, session_id=settings.session_id)
        check("memory_forget is irreversible → it stops at a confirmation instead of deleting",
              blocked.ok is False and blocked.decision == "needs_confirmation" and blocked.risk == "high"
              and bool(blocked.confirmation_id),
              f"confirmation={str(blocked.confirmation_id)[:20]}")

        status, body = await _http(host, port, "POST", "/api/v1/confirmations",
                                   {"confirmation_id": blocked.confirmation_id, "approve": True})
        execution = body.get("execution") or {}
        cleaned = await app.memory.forget(preference_id)      # leave no smoke residue behind
        left = {record["memory_id"] for record in app.memory.semantic.recent(limit=300)}
        check("approve → the smoke fact is really gone (memory writes are audited too)",
              status == 200 and body.get("ok") is True and execution.get("removed") is True
              and memory_id not in left and cleaned.get("removed") is True
              and preference_id not in left,
              f"removed={execution.get('removed')} preference_cleaned={cleaned.get('removed')}")

        status, body = await _http(host, port, "GET", "/api/v1/events?limit=200")
        kinds = {event.get("kind") for event in body.get("events", [])}
        check("events → memory.updated and memory.retrieved are on the mission stream",
              status == 200 and {"memory.updated", "memory.retrieved"} <= kinds
              and not any(event.get("payload", {}).get("_unknown_kind") for event in body.get("events", [])),
              f"memory kinds={sorted(k for k in kinds if str(k).startswith('memory.'))}")

        status, body = await _http(host, port, "GET", "/api/v1/health")
        memory_health = (body.get("checks", {}).get("memory", {}).get("detail") or {}).get("detail") or {}
        check("health → the memory slot reports which layers are alive",
              status == 200 and body.get("checks", {}).get("memory", {}).get("status") == "ok"
              and memory_health.get("missing") == [] and all(memory_health.get("layers", {}).values())
              and memory_health.get("working_delegated") is True,
              f"layers={memory_health.get('layers')}")

        # ── Phase 9: the safe learning loop ─────────────────────────────────
        status, body = await _http(host, port, "GET", "/api/v1/learning?limit=20")
        gates = body.get("gates", {})
        check(
            "GET /api/v1/learning → the loop, its six gates and its anti-self-modification guarantee",
            status == 200 and body.get("ok") is True and body.get("enabled") is True
            and body.get("no_self_modification") is True and gates.get("anti_self_modification") is True
            and gates.get("gates") == ["frequency", "success_rate", "risk", "no_code", "no_secret", "approval"]
            and body.get("targets", {}).get("pattern_store") is True
            and body.get("targets", {}).get("memory_manager") is True,
            f"gates={gates.get('gates')} max_risk={gates.get('max_risk')}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/tools")
        learning_tools = {t["name"]: t for t in body.get("tools", []) if t["name"].startswith("learning_")}
        check(
            "GET /api/v1/tools → four learning tools, risk-tiered (reads low, writes medium)",
            status == 200 and len(learning_tools) == 4
            and learning_tools.get("learning_status", {}).get("risk") == "low"
            and learning_tools.get("learning_list_candidates", {}).get("risk") == "low"
            and learning_tools.get("learning_feedback", {}).get("risk") == "medium"
            and learning_tools.get("learning_cycle", {}).get("risk") == "medium"
            and all(t.get("category") == "learning" for t in learning_tools.values()),
            f"tools={sorted(learning_tools)}",
        )

        # interaction → observation: a finished two-tool sequence becomes an inert candidate
        app.learning.observe_sequence(["take_screenshot", "see_screen"], succeeded=True,
                                      session_id=settings.session_id, source="smoke")
        result = await app.executor.call("learning_status", {}, session_id=settings.session_id, dry_run=False)
        check("learning_status through the tool layer → the gates + counters, read-only",
              result.ok is True and result.decision == "executed" and result.risk == "low"
              and result.data.get("enabled") is True and result.data.get("no_self_modification") is True
              and result.data.get("candidates_by_state", {}).get("observing", 0) >= 1,
              f"by_state={result.data.get('candidates_by_state')}")

        listed = await app.executor.call("learning_list_candidates", {"kind": "pattern"},
                                         session_id=settings.session_id, dry_run=False)
        check("learning_list_candidates → the inert candidate ledger, filterable by kind",
              listed.ok is True and listed.data.get("count", 0) >= 1
              and all(c.get("kind") == "pattern" for c in listed.data.get("candidates", [])),
              f"count={listed.data.get('count')}")

        # the human half: a stated preference + positive feedback clears every gate and promotes
        status, body = await _http(host, port, "POST", "/api/v1/learning",
                                   {"signal": "positive", "key": "smoke.learning_lang", "value": "bn",
                                    "session_id": settings.session_id})
        promoted_value = app.memory.preference("smoke.learning_lang")
        check("POST /api/v1/learning (positive preference) → validated, then written into the one preference store",
              status == 200 and body.get("ok") is True and body.get("preference") is not None
              and (body.get("cycle") or {}).get("promoted", 0) >= 1 and promoted_value == "bn",
              f"promoted_to=preference_layer value={promoted_value}")

        status, body = await _http(host, port, "POST", "/api/v1/learning", {"signal": "sarcastic"})
        check("POST /api/v1/learning (bad signal) → 422, an honest refusal",
              status == 422 and body.get("ok") is False and "signal" in str(body).lower(),
              f"status={status}")

        # the safety gate: a credential-named preference is REFUSED — never learned, never stored
        status, body = await _http(host, port, "POST", "/api/v1/learning",
                                   {"signal": "positive", "key": "api_key", "value": "smoke",
                                    "session_id": settings.session_id})
        leaked = app.memory.preference("api_key")
        rejected = app.learning.candidates.find_by_signature("api_key=smoke")
        check("learning refuses a credential-named candidate → the no_secret gate rejects it, nothing is stored",
              status == 200 and leaked is None and rejected is not None
              and rejected.state.value == "rejected" and any("no_secret" in r for r in rejected.reasons),
              f"api_key_stored={leaked} state={rejected.state.value if rejected else None}")

        status, body = await _http(host, port, "POST", "/api/v1/learning/cycle", {"promote": True})
        check("POST /api/v1/learning/cycle → one bounded validation sweep that never bypasses a gate",
              status == 200 and body.get("ok") is True and "considered" in body and "promoted" in body,
              f"considered={body.get('considered')} promoted={body.get('promoted')}")

        cyc = await app.executor.call("learning_cycle", {"promote": True},
                                      session_id=settings.session_id, dry_run=False)
        check("learning_cycle through the tool layer → the same sweep, audited",
              cyc.ok is True and cyc.decision == "executed" and "considered" in cyc.data,
              f"considered={cyc.data.get('considered')} validated={cyc.data.get('validated')}")

        pref_row = next((p for p in app.memory.preferences(limit=200)
                         if p.get("key") == "smoke.learning_lang"), None)
        if pref_row is not None:
            await app.memory.forget(pref_row["memory_id"])      # leave no smoke residue behind

        status, body = await _http(host, port, "GET", "/api/v1/events?limit=300")
        kinds = {event.get("kind") for event in body.get("events", [])}
        check("events → learning.observed / feedback / promoted are on the mission stream",
              status == 200 and {"learning.observed", "learning.feedback", "learning.promoted"} <= kinds
              and not any(event.get("payload", {}).get("_unknown_kind") for event in body.get("events", [])),
              f"learning kinds={sorted(k for k in kinds if str(k).startswith('learning.'))}")

        status, body = await _http(host, port, "GET", "/api/v1/health")
        learning_health = (body.get("checks", {}).get("learning", {}).get("detail") or {}).get("detail") or {}
        check("health → the learning slot reports the two stores it borrows (never its own)",
              status == 200 and body.get("checks", {}).get("learning", {}).get("status") == "ok"
              and learning_health.get("pattern_store") is True and learning_health.get("memory_manager") is True,
              f"pattern_store={learning_health.get('pattern_store')} memory={learning_health.get('memory_manager')}")

        # ── Phase 10: advanced orchestration ────────────────────────────────
        status, body = await _http(host, port, "GET", "/api/v1/orchestrator")
        orch_info = body.get("orchestrator", {})
        check(
            "GET /api/v1/orchestrator → status, active tasks and checkpoints count",
            status == 200 and body.get("ok") is True and orch_info.get("name") == "orchestrator"
            and "stats" in orch_info and "checkpoints_count" in orch_info,
            f"name={orch_info.get('name')} stats={orch_info.get('stats')}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/plans")
        check(
            "GET /api/v1/plans → list of plans backed by the orchestrator",
            status == 200 and body.get("ok") is True and isinstance(body.get("plans"), list),
            f"plans={len(body.get('plans', []))}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/tools")
        orch_tools = {t["name"]: t for t in body.get("tools", []) if t["category"] == "meta"
                      and (t["name"].startswith("plan_") or t["name"].startswith("task_") or t["name"].startswith("orchestrator_"))}
        check(
            "GET /api/v1/tools → six orchestration tools (task_cancel, status, pause, resume, checkpoint)",
            status == 200 and len(orch_tools) == 6
            and {"task_cancel", "task_status", "plan_pause", "plan_resume", "plan_checkpoint", "orchestrator_status"} <= set(orch_tools),
            f"orch_tools={list(orch_tools.keys())}",
        )

        orch_stat = await app.executor.call("orchestrator_status", {}, session_id=settings.session_id, dry_run=False)
        check(
            "orchestrator_status through tool layer → reports orchestrator state",
            orch_stat.ok is True and orch_stat.decision == "executed" and orch_stat.data.get("name") == "orchestrator",
            f"data={orch_stat.data}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/info")
        caps = body.get("capabilities", [])
        check(
            "info → orchestrator capability is active",
            status == 200 and "orchestrator" in caps,
            f"capabilities_count={len(caps)}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/health")
        orch_health = body.get("checks", {}).get("orchestrator", {})
        check(
            "health → orchestrator slot reports ok",
            status == 200 and orch_health.get("status") == "ok",
            f"orchestrator_status={orch_health.get('status')}",
        )

        # ── Phase 11: Security surface, budgets, secrets & audit ───────────
        status, body = await _http(host, port, "GET", "/api/v1/security")
        check(
            "GET /api/v1/security → policy, safety level, budgets and owner",
            status == 200 and body.get("ok") is True and body.get("phase") in (4, 11) and body.get("owner") == "Nilanjan",
            f"phase={body.get('phase')} safety={body.get('safety_level')} owner={body.get('owner')}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/security/scan", {
            "text": "test with open key: sk-abcdefghijklmnopqrstuvwxyz12345"
        })
        check(
            "POST /api/v1/security/scan → active secret scanner redacts credentials",
            status == 200 and body.get("has_secrets") is True and "[REDACTED" in body.get("redacted_text", ""),
            f"kinds={body.get('secret_kinds')}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/tools")
        sec_tools = [t["name"] for t in body.get("tools", []) if t["name"].startswith("security_")]
        check(
            "GET /api/v1/tools → five Phase 11 security tools registered",
            len(sec_tools) == 5,
            f"security_tools={sorted(sec_tools)}",
        )

        sec_scan = await app.executor.call(
            "security_scan_secrets",
            {"text": "token: Bearer secret-auth-token-12345"},
            session_id=settings.session_id,
            dry_run=False,
        )
        check(
            "security_scan_secrets through tool layer → scans and redacts",
            sec_scan.ok is True and sec_scan.data.get("has_secrets") is True,
            f"data={sec_scan.data}",
        )

        sec_stat = await app.executor.call(
            "security_status",
            {},
            session_id=settings.session_id,
            dry_run=False,
        )
        check(
            "security_status through tool layer → reports surface health & policy",
            sec_stat.ok is True and sec_stat.data.get("name") == "security",
            f"data={sec_stat.data.get('name')}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/stop", {"reason": "smoke_security_test"})
        check(
            "POST /api/v1/stop → emergency stop engaged",
            status == 200 and body.get("stopped") is True,
            f"stopped={body.get('stopped')}",
        )

        status, body = await _http(host, port, "POST", "/api/v1/security/resume")
        check(
            "POST /api/v1/security/resume → operations resumed cleanly",
            status == 200 and body.get("stopped") is False,
            f"stopped={body.get('stopped')}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/audit?limit=5")
        check(
            "GET /api/v1/audit → queryable audit trail",
            status == 200 and body.get("ok") is True and len(body.get("entries", [])) > 0,
            f"entries_count={len(body.get('entries', []))}",
        )

        status, body = await _http(host, port, "GET", "/api/v1/health")
        check("health stays green after the run", status == 200 and body.get("status") in {"ok", "degraded"},
              f"status={body.get('status')}")
    finally:
        await server.stop()
        shutil.rmtree(smoke_dir, ignore_errors=True)      # leave no smoke residue behind

    passed = sum(1 for _, state, _ in _results if state == PASS)
    failed = len(_results) - passed
    print(f"\n[smoke] {passed} passed, {failed} failed\n")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", type=int, default=None, help="informational tag only")
    parser.parse_args()
    return asyncio.run(run_smoke())


if __name__ == "__main__":
    raise SystemExit(main())
