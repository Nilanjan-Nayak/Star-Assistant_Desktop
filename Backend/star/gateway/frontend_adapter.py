"""STAR 2.0 — optional, zero-touch gateway adapter for the existing Star HUD.

CORE RULE, enforced by the repository layout: **``Frontend/`` is never touched.**
This file used to live at ``Frontend/star_gateway.py``; it now lives in the STAR
2.0 backend (``Backend/star/gateway/frontend_adapter.py``) so that
``git diff main -- Frontend/`` and ``git diff main -- Backend/bridge.py`` are
both *empty*. Not one line of the shipped HUD or its bridge is modified.

Integration is done from the outside, the way Qt allows it: the mirror
**connects to the bridge's existing signals** (``log_emitted``,
``response_ready``, ``state_changed``, ``speaking_started/finished``) instead of
asking for hooks inside them. The user's own query is recognised from the HUD's
existing ``"▸ <query>"`` log line, so nothing has to be added to ``bridge.py``
either.

Opt in from any launcher (``run.py``, a script, or a REPL) with one call::

    from Backend.star.gateway.frontend_adapter import attach_to_bridge
    mirror = attach_to_bridge(bridge)        # no-op unless STAR_GATEWAY_URL is set
    ...
    mirror.shutdown()                        # optional; Qt signals disconnect on exit

* :class:`StarGatewayClient`  — blocking stdlib HTTP calls (used from a worker thread)
* :class:`StarGatewayStream`  — background WebSocket listener (falls back to polling)
* :class:`StarGatewayMirror`  — the object a launcher holds; every method is a
  no-op when the gateway is disabled or unreachable

Deliberately **Qt-free** so it can be unit-tested headless and reused by scripts.
Enable it with ``STAR_GATEWAY_URL=http://127.0.0.1:8765`` (see ``.env.example``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "StarGatewayClient",
    "StarGatewayStream",
    "StarGatewayMirror",
    "attach_to_bridge",
    "attach_gateway_mirror",
    "gateway_url_from_env",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

EventCallback = Callable[[dict[str, Any]], None]
LogCallback = Callable[[str, str], None]

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: the HUD logs the user's own line as ``"▸ <query>"`` — our zero-touch query source
_QUERY_LOG_PREFIX = "▸ "
#: lines this adapter prints into the HUD log; never mirror them back out
_OWN_LOG_PREFIXES = ("[Gateway]", "[Confirm]", "[Safety]", "[Task]", "[Star]")


def gateway_url_from_env(default: str = "") -> str:
    return (os.getenv("STAR_GATEWAY_URL", default) or "").strip().rstrip("/")


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP client (stdlib only)
# ─────────────────────────────────────────────────────────────────────────────


class StarGatewayClient:
    """Blocking JSON client. Always call it from a worker thread, never the UI thread."""

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout = timeout
        self.last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Returns ``{"ok": bool, ...}``; ``transport=True`` marks a *connection*
        failure (as opposed to a valid HTTP error response), which is what the
        mirror uses to decide whether to disable itself."""
        if not self.enabled:
            return {"ok": False, "error": "gateway disabled", "transport": True}
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — http(s) only, local gateway
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "star-hud/1.0 (+star-2.0)")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8", errors="replace")
            self.last_error = ""
            return json.loads(raw) if raw else {"ok": True}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            self.last_error = f"HTTP {exc.code}: {body[:200]}"
            return {"ok": False, "status": exc.code, "error": self.last_error, "transport": False}
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.last_error = repr(exc)
            return {"ok": False, "error": self.last_error, "transport": True}

    # ── endpoints ───────────────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/health")

    def info(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/info")

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/status")

    def chat(self, text: str, *, language: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text, "source": "hud"}
        if language:
            payload["language"] = language
        if session_id:
            payload["session_id"] = session_id
        return self._request("POST", "/api/v1/chat", payload)

    def events(self, *, since: int = 0, limit: int = 50, kinds: str = "") -> dict[str, Any]:
        query = urllib.parse.urlencode({"since": since, "limit": limit, **({"kinds": kinds} if kinds else {})})
        return self._request("GET", f"/api/v1/events?{query}")

    def remember(self, text: str, *, kind: str = "fact", key: str | None = None, value: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/api/v1/memory", {"text": text, "kind": kind, "key": key, "value": value})

    def confirm(self, confirmation_id: str, *, approve: bool, note: str = "") -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/confirmations", {"confirmation_id": confirmation_id, "approve": approve, "note": note}
        )

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/tasks/{urllib.parse.quote(task_id)}/cancel")

    def emergency_stop(self, reason: str = "hud") -> dict[str, Any]:
        return self._request("POST", "/api/v1/stop", {"reason": reason})

    def ui_state(self, state: str, **extra: Any) -> dict[str, Any]:
        """Mirror a HUD reactor state so the console/audit can follow the UI."""
        return self._request("POST", "/api/v1/ui", {"type": "ui.state", "state": state, **extra})

    def tools(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/tools")

    def tasks(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/tasks")


# ─────────────────────────────────────────────────────────────────────────────
#  Minimal WebSocket client (RFC 6455) — streaming events into the HUD
# ─────────────────────────────────────────────────────────────────────────────


def _ws_encode(payload: bytes, opcode: int = 0x1) -> bytes:
    """Client → server frames MUST be masked."""
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
    elif size <= 0xFFFF:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


class _WSReader:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def _recv(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed by peer")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read_frame(self) -> tuple[int, bytes]:
        b0, b1 = self._recv(2)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = int(struct.unpack("!H", self._recv(2))[0])
        elif length == 127:
            length = int(struct.unpack("!Q", self._recv(8))[0])
        payload = self._recv(length) if length else b""
        if b1 & 0x80:  # server frames should not be masked, but tolerate it
            mask = self._recv(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload


class StarGatewayStream:
    """Background thread: WebSocket first, HTTP polling fallback. Never raises."""

    def __init__(
        self,
        base_url: str,
        on_event: EventCallback,
        *,
        on_status: Callable[[bool, str], None] | None = None,
        poll_interval: float = 1.5,
        session_id: str = "hud",
        prefer_ws: bool = True,
    ) -> None:
        self.client = StarGatewayClient(base_url)
        self.on_event = on_event
        self.on_status = on_status or (lambda ok, detail: None)
        self.poll_interval = poll_interval
        self.session_id = session_id
        self.prefer_ws = prefer_ws
        self._running = False
        self._thread: threading.Thread | None = None
        self._since = 0
        self.connected = False

    # ── control ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.client.enabled or self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="star-gateway-stream")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self.connected = False

    # ── loops ───────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while self._running:
            if self.prefer_ws:
                try:
                    self._run_websocket()
                except Exception as exc:  # noqa: BLE001 — any failure degrades to polling
                    self.connected = False
                    self.on_status(False, f"ws: {exc}")
            if not self._running:
                break
            self._poll_once()
            time.sleep(self.poll_interval)

    def _run_websocket(self, *, max_messages: int = 100000) -> None:
        parsed = urllib.parse.urlsplit(self.client.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme == "https":
            raise RuntimeError("wss needs the 'websockets' extra; falling back to polling")
        path = f"/ws?session_id={urllib.parse.quote(self.session_id)}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        with socket.create_connection((host, port), timeout=self.client.timeout) as sock:
            sock.settimeout(45.0)
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("handshake failed")
                response += chunk
            head, _, rest = response.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0].decode("iso-8859-1")
            if " 101 " not in status_line:
                raise ConnectionError(f"unexpected handshake status: {status_line}")
            expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()  # noqa: S324
            if expected.lower() not in head.decode("iso-8859-1").lower():
                raise ConnectionError("bad Sec-WebSocket-Accept")

            reader = _WSReader(sock)
            if rest:
                reader._buf.extend(rest)  # noqa: SLF001 — reuse the buffered tail
            self.connected = True
            self.on_status(True, "websocket")
            for _ in range(max_messages):
                if not self._running:
                    break
                opcode, payload = reader.read_frame()
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    sock.sendall(_ws_encode(payload, 0xA))
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                try:
                    message = json.loads(payload.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                self._dispatch(message)
        self.connected = False

    def _poll_once(self) -> None:
        result = self.client.events(since=self._since, limit=80)
        events = result.get("events") if isinstance(result, dict) else None
        if not isinstance(events, list):
            self.on_status(False, self.client.last_error or "poll failed")
            return
        self.on_status(True, "polling")
        for event in events:
            if isinstance(event, dict):
                self._since = max(self._since, int(event.get("seq") or 0))
                self._emit(event)

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "event":
            event = message.get("event") or {}
            self._since = max(self._since, int(event.get("seq") or 0))
            self._emit(event)
        elif kind == "hello":
            for event in message.get("replay") or []:
                self._emit(event)
            self._emit({"kind": "gateway.hello", "phase": "gateway", "payload": message.get("info") or {}})
        elif kind == "result":
            self._emit({"kind": "gateway.result", "phase": "gateway", "payload": message})
        elif kind == "error":
            self._emit({"kind": "gateway.error", "phase": "gateway", "payload": message})

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 — a bad HUD callback must not kill the stream
            pass

    # ── outbound ────────────────────────────────────────────────────────────
    def send(self, message: dict[str, Any]) -> bool:
        """Best-effort WS send is not available from the poll fallback; use HTTP."""
        _ = message
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Mirror — the object Backend/bridge.py holds
# ─────────────────────────────────────────────────────────────────────────────


class StarGatewayMirror:
    """Fire-and-forget mirror between the HUD and the STAR 2.0 gateway.

    Every public method is safe to call from the Qt UI thread: work happens on a
    single background worker thread with a bounded queue. If the gateway is not
    reachable the mirror disables itself after ``max_failures`` and stays silent.
    """

    def __init__(
        self,
        base_url: str,
        *,
        on_event: EventCallback | None = None,
        on_log: LogCallback | None = None,
        session_id: str = "hud",
        timeout: float = 4.0,
        max_failures: int = 6,
        use_stream: bool = True,
        prefer_ws: bool = True,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.client = StarGatewayClient(self.base_url, timeout=timeout)
        self.session_id = session_id
        self.on_event = on_event or (lambda event: None)
        self.on_log = on_log or (lambda text, tag: None)
        self.max_failures = max_failures
        self.enabled = bool(self.base_url)
        self.failures = 0
        self.disabled_reason = ""
        self._queue: list[tuple[Callable[[], Any], str]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = False
        self._worker: threading.Thread | None = None
        self.stream: StarGatewayStream | None = None
        self.hello: dict[str, Any] = {}
        if self.enabled:
            self._start_worker()
            if use_stream:
                self.stream = StarGatewayStream(
                    self.base_url,
                    self._on_stream_event,
                    on_status=self._on_stream_status,
                    session_id=session_id,
                    prefer_ws=prefer_ws,
                )
                self.stream.start()

    # ── worker ──────────────────────────────────────────────────────────────
    def _start_worker(self) -> None:
        self._running = True
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="star-gateway-mirror")
        self._worker.start()

    def _worker_loop(self) -> None:
        while self._running:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    job, label = self._queue.pop(0)
                if not self.enabled:
                    continue
                try:
                    result = job()
                    if isinstance(result, dict) and result.get("transport"):
                        raise ConnectionError(result.get("error") or "gateway unreachable")
                    self.failures = 0
                except Exception as exc:  # noqa: BLE001
                    self._fail(label, exc)

    def _enqueue(self, job: Callable[[], Any], label: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            if len(self._queue) > 200:  # never let a dead gateway grow the queue
                del self._queue[:100]
            self._queue.append((job, label))
        self._wake.set()

    def _fail(self, label: str, exc: Exception) -> None:
        self.failures += 1
        if self.failures >= self.max_failures and self.enabled:
            self.enabled = False
            self.disabled_reason = f"{label}: {exc}"
            self.on_log(f"[Gateway] mirror disabled ({self.disabled_reason})", "alert")
            if self.stream is not None:
                self.stream.stop()

    def _on_stream_event(self, event: dict[str, Any]) -> None:
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001
            pass

    def _on_stream_status(self, ok: bool, detail: str) -> None:
        if ok and not self.hello:
            self.hello = {"connected": True, "transport": detail, "at": time.time()}
            self.on_log(f"[Gateway] connected ({detail})", "ok")

    # ── public, UI-thread-safe API ──────────────────────────────────────────
    # ── zero-touch Qt signal handlers ───────────────────────────────────────
    def on_bridge_log(self, text: Any, tag: str = "cyan") -> None:
        """``log_emitted(str, str)`` handler.

        Two jobs: recognise the user's own query from the HUD's existing
        ``"▸ <query>"`` line (so ``bridge.py`` needs no hook), and skip the lines
        this mirror itself printed, so nothing echoes back to the gateway.
        """
        if not isinstance(text, str):
            return
        if text.startswith(_QUERY_LOG_PREFIX) and tag == "cyan":
            query = text[len(_QUERY_LOG_PREFIX):].strip()
            if query:
                self.mirror_query(query)
            return
        if text.startswith(_OWN_LOG_PREFIXES):
            return
        self.mirror_log(text, tag)

    def on_bridge_response(self, text: Any) -> None:
        """``response_ready(str)`` handler."""
        if isinstance(text, str) and text.strip():
            self.mirror_response(text)

    def on_bridge_state(self, state: Any) -> None:
        """``state_changed(str)`` handler."""
        if isinstance(state, str) and state:
            self.mirror_state(state)

    def mirror_query(self, text: str, *, language: str | None = None) -> None:
        self._enqueue(lambda: self.client.ui_state("think", text=text[:400], language=language), "mirror_query")

    def mirror_response(self, text: str, *, source: str = "brain") -> None:
        self._enqueue(lambda: self.client.ui_state("speak", text=text[:400], source=source), "mirror_response")

    def mirror_state(self, state: str, **extra: Any) -> None:
        self._enqueue(lambda: self.client.ui_state(state, **extra), "mirror_state")

    def mirror_log(self, text: str, tag: str = "cyan") -> None:
        self._enqueue(lambda: self.client.ui_state("log", text=text[:400], tag=tag), "mirror_log")

    def send_text(self, text: str, *, language: str | None = None, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Ask the STAR 2.0 brain to handle a request (async, callback on worker thread)."""

        def _job() -> None:
            result = self.client.chat(text, language=language, session_id=self.session_id)
            if callback is not None:
                callback(result)

        self._enqueue(_job, "send_text")

    def stop_all(self, reason: str = "hud") -> None:
        self._enqueue(lambda: self.client.emergency_stop(reason), "stop_all")

    def resolve(self, confirmation_id: str, *, approve: bool) -> None:
        self._enqueue(lambda: self.client.confirm(confirmation_id, approve=approve), "resolve")

    def probe(self) -> dict[str, Any]:
        """Synchronous health probe (call from a worker thread, not the UI thread)."""
        return self.client.health()

    def shutdown(self) -> None:
        self._running = False
        self.enabled = False
        self._wake.set()
        if self.stream is not None:
            self.stream.stop()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.5)
        self._worker = None


def connect_bridge_signals(bridge: Any, mirror: StarGatewayMirror) -> tuple[str, ...]:
    """Connect the mirror to whatever Qt signals the bridge already exposes.

    Purely external: ``signal.connect(handler)`` never edits the emitting class.
    Missing signals (or a plain test double) are skipped, so this is safe on any
    object and in any environment — with or without Qt installed.
    """
    connected: list[str] = []

    def _connect(name: str, handler: Callable[..., Any]) -> None:
        signal = getattr(bridge, name, None)
        connect_fn = getattr(signal, "connect", None)
        if not callable(connect_fn):
            return
        try:
            connect_fn(handler)
            connected.append(name)
        except Exception:  # noqa: BLE001 — a signal we cannot bind is simply unused
            pass

    _connect("log_emitted", mirror.on_bridge_log)
    _connect("response_ready", mirror.on_bridge_response)
    _connect("state_changed", mirror.on_bridge_state)
    _connect("speaking_started", lambda: mirror.mirror_state("speak"))
    _connect("speaking_finished", lambda: mirror.mirror_state("idle"))
    mirror.connected_signals = tuple(connected)          # type: ignore[attr-defined]
    return tuple(connected)


def attach_to_bridge(bridge: Any, url: str = "", **kwargs: Any) -> StarGatewayMirror | None:
    """Create a mirror bound to an ``AssistantBridge``-like object — zero-touch.

    ``bridge`` only needs ``log_emitted.emit(text, tag)``-ish behaviour; anything
    missing is ignored, so this also works with plain test doubles. Returns
    ``None`` when no URL is configured (the default) — i.e. a total no-op.
    """
    base_url = url or gateway_url_from_env()
    if not base_url:
        return None

    def _log(text: str, tag: str) -> None:
        emitter = getattr(bridge, "log_emitted", None)
        emit = getattr(emitter, "emit", None)
        if callable(emit):
            try:
                emit(text, tag)
            except Exception:  # noqa: BLE001
                pass

    def _on_event(event: dict[str, Any]) -> None:
        kind = str(event.get("kind", ""))
        payload = event.get("payload") or {}
        text = ""
        if kind == "confirmation.requested":
            text = f"[Confirm] {payload.get('tool')} — risk {payload.get('risk')}"
        elif kind == "safety.blocked":
            text = f"[Safety] blocked: {payload.get('reason')}"
        elif kind == "task.completed":
            text = f"[Task] completed: {str(payload.get('goal') or payload.get('summary') or '')[:80]}"
        elif kind == "task.failed":
            text = f"[Task] failed: {str(payload.get('error') or '')[:80]}"
        elif kind == "response.spoken":
            text = f"[Star] {str(payload.get('text') or '')[:120]}"
        if text:
            _log(text, "gold" if kind.startswith(("confirmation", "safety")) else "cyan")

    mirror = StarGatewayMirror(base_url, on_event=_on_event, on_log=_log, **kwargs)
    bound = connect_bridge_signals(bridge, mirror)
    detail = f" signals={','.join(bound)}" if bound else " (duck-typed bridge)"
    _log(f"[Gateway] STAR 2.0 mirror → {base_url}{detail}", "ok")
    return mirror


#: back-compatible name (Phase 1 docs/tests) — same zero-touch behaviour
def attach_gateway_mirror(bridge: Any, url: str = "", **kwargs: Any) -> StarGatewayMirror | None:
    return attach_to_bridge(bridge, url, **kwargs)
