"""Phase 1 — the thin frontend adapter must be additive, optional and safe.

The HUD itself (``Frontend/main.py`` and friends) is never imported here: it needs
PySide6. These tests cover the Qt-free adapter that ``Backend/bridge.py`` opts into.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from Backend.star.config.settings import Settings
from Backend.star.gateway.http import WSFrameDecoder, WSOpcode
from Backend.star.gateway.server import GatewayServer
from Backend.star.main import build_application
from Backend.star.observability.events import StarEventBus, reset_event_bus
from Backend.star.gateway.frontend_adapter import (
    StarGatewayClient,
    StarGatewayMirror,
    StarGatewayStream,
    attach_gateway_mirror,
    attach_to_bridge,
    connect_bridge_signals,
    gateway_url_from_env,
    _ws_encode,
)


class _FakeSignal:
    """Qt-signal stand-in: ``emit`` records, ``connect`` binds — like the real thing."""

    def __init__(self) -> None:
        self.emitted: list[tuple[Any, ...]] = []
        self.slots: list[Any] = []

    def emit(self, *args: Any) -> None:
        self.emitted.append(args)
        for slot in self.slots:
            slot(*args)

    def connect(self, slot: Any) -> None:
        self.slots.append(slot)


class _FakeBridge:
    """Duck-typed stand-in for ``Backend.bridge.AssistantBridge`` (no Qt needed).

    It exposes the *same signal names* the real bridge already has, which is what
    makes the zero-touch adapter testable: nothing here is a hook we added to the
    shipped HUD.
    """

    def __init__(self) -> None:
        self.log_emitted = _FakeSignal()
        self.response_ready = _FakeSignal()
        self.state_changed = _FakeSignal()
        self.speaking_started = _FakeSignal()
        self.speaking_finished = _FakeSignal()


@pytest.fixture
def gateway(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Gateway on a *background thread* — the adapter tests are synchronous
    (they exercise threading code), so the server needs its own running loop."""
    monkeypatch.setenv("STAR_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("STAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STAR_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("STAR_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("STAR_EPISODES_PATH", str(tmp_path / "episodes.jsonl"))
    monkeypatch.setenv("STAR_PATTERNS_PATH", str(tmp_path / "patterns.jsonl"))
    monkeypatch.setenv("STAR_CANDIDATES_PATH", str(tmp_path / "learning" / "candidates.jsonl"))
    monkeypatch.setenv("STAR_FEEDBACK_PATH", str(tmp_path / "learning" / "feedback.jsonl"))
    monkeypatch.setenv("STAR_SESSION_ID", "adapter-test")
    reset_event_bus()
    settings = Settings.from_env(load_env_file=False)
    bus = StarEventBus(history_size=200)
    app = build_application(settings, bus=bus)

    holder: dict[str, Any] = {}
    ready = threading.Event()
    stop_requested = threading.Event()

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main() -> None:
            await app.startup()
            server = GatewayServer(app, settings, host="127.0.0.1", port=0, bus=bus)
            await server.start()
            holder["server"] = server
            ready.set()
            while not stop_requested.is_set():
                await asyncio.sleep(0.05)
            await server.stop()

        try:
            loop.run_until_complete(_main())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name="test-gateway")
    thread.start()
    assert ready.wait(timeout=20), "gateway thread did not come up"
    try:
        yield f"http://127.0.0.1:{holder['server'].port}", app, bus
    finally:
        stop_requested.set()
        thread.join(timeout=20)
        reset_event_bus()


# ── disabled by default ───────────────────────────────────────────────────────


def test_adapter_is_a_noop_without_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAR_GATEWAY_URL", raising=False)
    assert gateway_url_from_env() == ""
    assert attach_gateway_mirror(_FakeBridge(), "") is None
    assert attach_to_bridge(_FakeBridge(), "") is None


def test_mirror_binds_to_existing_signals_without_editing_the_bridge() -> None:
    """Zero-touch: we connect to the bridge's signals, we never modify it."""
    bridge = _FakeBridge()
    mirror = attach_to_bridge(bridge, "http://127.0.0.1:1", use_stream=False)
    assert mirror is not None
    assert set(mirror.connected_signals) == {
        "log_emitted",
        "response_ready",
        "state_changed",
        "speaking_started",
        "speaking_finished",
    }
    assert bridge.log_emitted.slots, "the handler is bound from the outside"

    seen: dict[str, list[Any]] = {"query": [], "response": [], "state": [], "log": []}
    mirror.mirror_query = lambda text, **_kw: seen["query"].append(text)      # type: ignore[method-assign]
    mirror.mirror_response = lambda text, **_kw: seen["response"].append(text)  # type: ignore[method-assign]
    mirror.mirror_state = lambda state, **_kw: seen["state"].append(state)     # type: ignore[method-assign]
    mirror.mirror_log = lambda text, tag="cyan": seen["log"].append((text, tag))  # type: ignore[method-assign]

    bridge.log_emitted.emit("▸ volume 40 koro", "cyan")       # the HUD's own query line
    bridge.log_emitted.emit("[Agent Task] done", "gold")       # a normal HUD log line
    bridge.log_emitted.emit("[Gateway] mirror → x", "ok")      # our own line: must not echo back
    bridge.response_ready.emit("ঠিক আছে বন্ধু!")
    bridge.state_changed.emit("think")
    bridge.speaking_started.emit()
    bridge.speaking_finished.emit()

    assert seen["query"] == ["volume 40 koro"]
    assert seen["response"] == ["ঠিক আছে বন্ধু!"]
    assert seen["state"] == ["think", "speak", "idle"]
    assert seen["log"] == [("[Agent Task] done", "gold")]
    mirror.shutdown()


def test_connect_bridge_signals_tolerates_a_bridge_without_signals() -> None:
    mirror = StarGatewayMirror("http://127.0.0.1:1", use_stream=False)
    assert connect_bridge_signals(object(), mirror) == ()
    assert mirror.connected_signals == ()
    mirror.shutdown()


def test_frontend_directory_stays_untouched() -> None:
    """The core rule, asserted: no STAR 2.0 file lives inside ``Frontend/``."""
    root = Path(__file__).resolve().parents[1]
    frontend = root / "Frontend"
    assert frontend.is_dir(), "the shipped HUD must still be there"
    offenders: list[str] = []
    for path in sorted(frontend.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "star_gateway" in text or "Backend.star" in text or "star2" in text:
            offenders.append(path.name)
    assert offenders == [], f"Frontend/ must not know about STAR 2.0: {offenders}"
    assert (root / "Backend" / "star" / "gateway" / "frontend_adapter.py").is_file()


def test_client_reports_disabled_instead_of_raising() -> None:
    client = StarGatewayClient("")
    assert client.enabled is False
    for call in (client.health, client.info, client.status, client.tools, client.tasks):
        result = call()
        assert result["ok"] is False and result["transport"] is True
        assert result["error"] == "gateway disabled"
    assert client.chat("hi")["ok"] is False


def test_mirror_without_url_never_spawns_threads() -> None:
    mirror = StarGatewayMirror("")
    assert mirror.enabled is False
    mirror.mirror_query("volume barao")
    mirror.mirror_response("ok")
    mirror.mirror_state("think")
    mirror.stop_all()
    mirror.shutdown()
    assert mirror._queue == []  # noqa: SLF001 — asserting nothing was scheduled


# ── live behaviour ────────────────────────────────────────────────────────────


def test_client_talks_to_the_gateway(gateway) -> None:
    url, app, bus = gateway
    client = StarGatewayClient(url, timeout=5.0)
    assert client.enabled is True

    health = client.health()
    assert health["status"] in {"ok", "degraded"}
    info = client.info()
    assert info["protocol"] == "star.mission.v1"

    result = client.chat("volume 30 koro")
    assert result["ok"] is True
    assert result["response"], "the legacy router or echo path must always answer"

    events = client.events(limit=50)
    assert any(event["kind"] == "request.received" for event in events["events"])

    assert client.ui_state("think", text="volume 30 koro")["mirrored"] == "ui.state"
    assert client.emergency_stop("adapter-test")["stopped"] is True
    app.resume()
    assert client.status()["stopped"] is False
    assert isinstance(client.tools()["tools"], list)
    assert client.last_error == ""


def test_mirror_forwards_hud_state_to_the_gateway(gateway) -> None:
    url, app, bus = gateway
    bridge = _FakeBridge()
    mirror = attach_gateway_mirror(bridge, url, use_stream=False, timeout=3.0)
    assert mirror is not None

    mirror.mirror_query("brightness 60 koro")
    mirror.mirror_response("ঠিক আছে, ৬০% করে দিলাম")
    mirror.mirror_state("act", tool="set_brightness")

    deadline = time.time() + 8.0
    while time.time() < deadline:
        kinds = [event.kind for event in bus.history(limit=50)]
        if kinds.count("task.progress") >= 3:
            break
        time.sleep(0.05)

    ui_events = [event for event in bus.history(limit=50) if event.payload.get("ui") == "ui.state"]
    assert len(ui_events) >= 3
    assert {event.payload.get("state") for event in ui_events} >= {"think", "speak", "act"}
    assert bridge.log_emitted.emitted, "the mirror must announce itself in the HUD activity log"

    mirror.shutdown()
    assert mirror.enabled is False


def test_mirror_disables_itself_when_the_gateway_dies() -> None:
    logs: list[tuple[str, str]] = []
    mirror = StarGatewayMirror(
        "http://127.0.0.1:1",  # nothing listens there
        on_log=lambda text, tag: logs.append((text, tag)),
        use_stream=False,
        timeout=0.4,
        max_failures=3,
    )
    for _ in range(6):
        mirror.mirror_state("think")
    deadline = time.time() + 10
    while mirror.enabled and time.time() < deadline:
        time.sleep(0.05)
    assert mirror.enabled is False
    assert mirror.disabled_reason
    assert any("disabled" in text for text, _ in logs)
    mirror.shutdown()


def test_send_text_uses_the_brain_and_calls_back(gateway) -> None:
    url, app, bus = gateway
    received: list[dict[str, Any]] = []
    done = threading.Event()

    def _cb(result: dict[str, Any]) -> None:
        received.append(result)
        done.set()

    mirror = StarGatewayMirror(url, use_stream=False, timeout=5.0)
    mirror.send_text("hey star kemon acho?", language="bn", callback=_cb)
    assert done.wait(timeout=10), "the brain must answer through the mirror"
    assert received[0]["ok"] is True
    mirror.shutdown()


def test_remember_endpoint_stores_a_preference(gateway) -> None:
    """Phase 8 made this endpoint real: the layered manager stores it and reports the layer."""
    url, app, bus = gateway
    client = StarGatewayClient(url)
    result = client.remember("amar pochonder gaan lo-fi", kind="preference", key="music.genre", value="lo-fi")
    assert result["ok"] is True and result["layer"] == "preference"
    assert result["key"] == "music.genre" and result["value"] == "lo-fi"
    snapshot = app.memory_snapshot("gaan")
    assert snapshot["ok"] is True
    assert {"key": "music.genre", "value": "lo-fi"} in [
        {k: v for k, v in item.items() if k in ("key", "value")} for item in snapshot["preferences"]
    ]


# ── websocket client framing ──────────────────────────────────────────────────


def test_ws_encode_produces_masked_frames_the_server_can_read() -> None:
    payload = json.dumps({"type": "chat", "text": "চালাও lo-fi"}).encode("utf-8")
    frame = _ws_encode(payload)
    assert frame[1] & 0x80, "client frames must set the mask bit"
    decoded = WSFrameDecoder().feed(frame)
    assert len(decoded) == 1
    assert decoded[0].opcode == WSOpcode.TEXT
    assert decoded[0].json() == {"type": "chat", "text": "চালাও lo-fi"}


@pytest.mark.parametrize("size", [1, 125, 126, 1000, 70000])
def test_ws_encode_all_length_widths(size: int) -> None:
    frame = _ws_encode(b"y" * size)
    decoded = WSFrameDecoder().feed(frame)
    assert len(decoded) == 1 and len(decoded[0].payload) == size


def test_stream_uses_websocket_when_available(gateway) -> None:
    url, app, bus = gateway
    seen: list[dict[str, Any]] = []
    stream = StarGatewayStream(url, seen.append, poll_interval=0.25)
    stream.start()
    try:
        deadline = time.time() + 12
        while time.time() < deadline and not stream.connected:
            time.sleep(0.05)
        assert stream.connected is True, "the stream should upgrade to a websocket"
        StarGatewayClient(url).chat("ws stream test")
        deadline = time.time() + 12
        while time.time() < deadline and not any(e.get("kind") == "request.received" for e in seen):
            time.sleep(0.05)
        assert any(event.get("kind") == "request.received" for event in seen), seen[-3:]
    finally:
        stream.stop()
    assert stream._thread is None  # noqa: SLF001 — stopped cleanly


def test_stream_falls_back_to_polling(gateway) -> None:
    url, app, bus = gateway
    seen: list[dict[str, Any]] = []
    statuses: list[tuple[bool, str]] = []
    stream = StarGatewayStream(
        url,
        seen.append,
        on_status=lambda ok, detail: statuses.append((ok, detail)),
        poll_interval=0.2,
        prefer_ws=False,
    )
    stream.start()
    try:
        StarGatewayClient(url).chat("poll test")
        deadline = time.time() + 12
        while time.time() < deadline and not any(e.get("kind") == "request.received" for e in seen):
            time.sleep(0.1)
        assert seen, "polling must deliver mission events"
        assert any(event.get("kind") == "request.received" for event in seen), [e.get("kind") for e in seen]
        assert stream.connected is False, "no websocket in poll-only mode"
        assert (True, "polling") in statuses
    finally:
        stream.stop()
