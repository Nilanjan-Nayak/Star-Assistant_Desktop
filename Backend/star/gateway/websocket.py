"""WebSocket session: the streaming half of the gateway (``star.mission.v1``).

Client → server messages::

    {"type": "chat",       "text": "...", "session_id": "...", "language": "bn|en"}
    {"type": "confirm",    "confirmation_id": "...", "approve": true, "note": "..."}
    {"type": "cancel",     "task_id": "..."}
    {"type": "stop",       "reason": "..."}
    {"type": "remember",   "text": "...", "kind": "preference", "key": "...", "value": "..."}
    {"type": "subscribe",  "kinds": ["task.*", "safety.blocked"]}
    {"type": "replay",     "limit": 50}
    {"type": "ping"}

Server → client messages::

    {"type": "hello",  ...info}          {"type": "event",  "event": {...}}
    {"type": "result", "request_id", ...} {"type": "ack", ...}  {"type": "error", ...}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from Backend.star import PROTOCOL_VERSION
from Backend.star.contracts import StarApplicationProtocol
from Backend.star.gateway.http import (
    HttpError,
    HttpRequest,
    WSFrame,
    WSFrameDecoder,
    WSOpcode,
    ws_accept_key,
    ws_close_frame,
    ws_encode,
    ws_text_frame,
)
from Backend.star.observability.events import EventPhase, StarEventBus, get_event_bus, new_event

__all__ = ["WebSocketSession", "matches_kinds"]

_HANDSHAKE = (
    "HTTP/1.1 101 Switching Protocols\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Accept: {accept}\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "\r\n"
)


def _compile_kinds(kinds: list[str]) -> frozenset[str] | None:
    if not kinds:
        return None
    return frozenset(k for k in (str(x).strip() for x in kinds) if k)


def matches_kinds(kind: str, patterns: frozenset[str] | None) -> bool:
    """``None``/empty matches everything; ``task.*`` matches the ``task`` family."""
    if not patterns:
        return True
    for pattern in patterns:
        if pattern == "*" or pattern == kind:
            return True
        if pattern.endswith(".*") and kind.startswith(pattern[:-1]):
            return True
    return False


class WebSocketSession:
    def __init__(
        self,
        app: StarApplicationProtocol,
        request: HttpRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        bus: StarEventBus | None = None,
    ) -> None:
        self.app = app
        self.request = request
        self.reader = reader
        self.writer = writer
        self.bus = bus or get_event_bus()
        self.decoder = WSFrameDecoder()
        self.session_id = request.query.get("session_id") or app.info().get("session_id", "star-default")
        self.kinds: frozenset[str] | None = None
        self.closed = False
        self.idle_timeout: float = 120.0
        self._sub = None
        self._pump: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def run(self) -> None:
        accept = ws_accept_key(self.request.header("sec-websocket-key"))
        self.writer.write(_HANDSHAKE.format(accept=accept).encode("ascii"))
        await self.writer.drain()

        self._sub = self.bus.subscribe(maxsize=512)
        self._pump = asyncio.create_task(self._pump_events(), name="ws-event-pump")

        await self.send(
            {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "session_id": self.session_id,
                "info": self.app.info(),
                "health": self.app.health(),
                "replay": [event.public() for event in self.bus.history(limit=25)],
            }
        )
        self.bus.emit(
            "session.started",
            phase=EventPhase.GATEWAY,
            transport="websocket",
            remote=self.request.remote,
            session_id=self.session_id,
        )

        try:
            await self._read_loop()
        except (HttpError, ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad client must not kill the server
            pass
        finally:
            await self.close()

    async def close(self, code: int = 1000, reason: str = "bye") -> None:
        if self.closed:
            return
        self.closed = True
        if self._sub is not None:
            self._sub.close()
        if self._pump is not None:
            self._pump.cancel()
        try:
            async with self._write_lock:
                self.writer.write(ws_close_frame(code, reason))
                await self.writer.drain()
        except (ConnectionResetError, OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except (ConnectionResetError, OSError):
                pass
        self.bus.emit("session.closed", phase=EventPhase.GATEWAY, session_id=self.session_id)

    # ── I/O ─────────────────────────────────────────────────────────────────
    async def send(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        payload = json.dumps(message, ensure_ascii=False, default=str)
        try:
            async with self._write_lock:
                self.writer.write(ws_text_frame(payload))
                await self.writer.drain()
        except (ConnectionResetError, OSError):
            self.closed = True

    async def send_event(self, event: Any) -> None:
        await self.send({"type": "event", "event": event.public()})

    async def _pump_events(self) -> None:
        assert self._sub is not None  # noqa: S101 — invariant of run()
        while not self.closed:
            try:
                event = await asyncio.wait_for(self._sub.queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            if matches_kinds(event.kind, self.kinds):
                await self.send_event(event)

    async def _read_loop(self) -> None:
        """Read frames until EOF/close. An idle peer is pinged once, then dropped.

        Without this a client that vanishes mid-session would pin the connection
        task forever (and, on Python 3.12+, ``Server.wait_closed()`` with it).
        """
        idle_warnings = 0
        while not self.closed:
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), timeout=self.idle_timeout)
            except TimeoutError:
                idle_warnings += 1
                if idle_warnings > 1:
                    await self.close(code=1001, reason="idle timeout")
                    return
                with contextlib.suppress(ConnectionResetError, OSError):
                    async with self._write_lock:
                        self.writer.write(ws_encode(b"star-ping", WSOpcode.PING))
                        await self.writer.drain()
                continue
            if not chunk:
                return
            idle_warnings = 0
            for frame in self.decoder.feed(chunk):
                if not await self._handle_frame(frame):
                    return

    async def _handle_frame(self, frame: WSFrame) -> bool:
        if frame.opcode == WSOpcode.CLOSE:
            return False
        if frame.opcode == WSOpcode.PING:
            async with self._write_lock:
                self.writer.write(ws_encode(frame.payload, WSOpcode.PONG))
                await self.writer.drain()
            return True
        if frame.opcode == WSOpcode.PONG:
            return True
        if frame.opcode != WSOpcode.TEXT:
            await self.send({"type": "error", "error": "only text frames are supported"})
            return True
        try:
            message = json.loads(frame.text)
        except json.JSONDecodeError as exc:
            await self.send({"type": "error", "error": f"invalid JSON: {exc}"})
            return True
        if not isinstance(message, dict):
            await self.send({"type": "error", "error": "message must be a JSON object"})
            return True
        await self.dispatch(message)
        return True

    # ── command dispatch ────────────────────────────────────────────────────
    async def dispatch(self, message: dict[str, Any]) -> None:
        kind = str(message.get("type") or message.get("action") or "").lower()
        request_id = str(message.get("request_id") or "")
        try:
            if kind in {"", "chat", "message", "text", "query"}:
                text = str(message.get("text") or message.get("query") or "").strip()
                if not text:
                    raise HttpError(422, "field 'text' is required")
                result = await self.app.chat(
                    text,
                    session_id=str(message.get("session_id") or self.session_id),
                    language=message.get("language"),
                    source=str(message.get("source") or "websocket"),
                )
                await self.send({"type": "result", "request_id": request_id, **result})
            elif kind == "confirm":
                cid = str(message.get("confirmation_id") or message.get("id") or "")
                if not cid:
                    raise HttpError(422, "field 'confirmation_id' is required")
                approve = bool(message.get("approve", False))
                result = await self.app.resolve_confirmation(
                    cid, approve=approve, note=str(message.get("note") or "")
                )
                await self.send({"type": "result", "request_id": request_id, **result})
            elif kind == "cancel":
                task_id = str(message.get("task_id") or "")
                if not task_id:
                    raise HttpError(422, "field 'task_id' is required")
                await self.send({"type": "result", "request_id": request_id, **await self.app.cancel_task(task_id)})
            elif kind in {"stop", "emergency_stop", "estop"}:
                await self.send(
                    {
                        "type": "result",
                        "request_id": request_id,
                        **await self.app.emergency_stop(reason=str(message.get("reason") or "websocket")),
                    }
                )
            elif kind == "remember":
                text = str(message.get("text") or "").strip()
                if not text:
                    raise HttpError(422, "field 'text' is required")
                await self.send(
                    {
                        "type": "result",
                        "request_id": request_id,
                        **await self.app.remember(
                            text,
                            kind=str(message.get("kind") or "fact"),
                            key=message.get("key"),
                            value=message.get("value"),
                        ),
                    }
                )
            elif kind == "subscribe":
                self.kinds = _compile_kinds(list(message.get("kinds") or []))
                await self.send({"type": "ack", "request_id": request_id, "kinds": sorted(self.kinds or ["*"])})
            elif kind == "replay":
                limit = min(int(message.get("limit") or 50), 500)
                await self.send(
                    {
                        "type": "ack",
                        "request_id": request_id,
                        "events": [event.public() for event in self.bus.history(limit=limit)],
                    }
                )
            elif kind in {"ping", "heartbeat"}:
                await self.send({"type": "ack", "request_id": request_id, "pong": True, "health": self.app.health()})
            elif kind in {"status", "snapshot"}:
                await self.send({"type": "result", "request_id": request_id, **self.app.status()})
            else:
                # Unknown verbs are still forwarded: the application owns the
                # growing command surface (voice.*, workspace.*, learning.*).
                result = await self.app.handle_message(message)
                await self.send({"type": "result", "request_id": request_id, **result})
        except HttpError as exc:
            await self.send({"type": "error", "request_id": request_id, "error": exc.message, "status": exc.status})
        except Exception as exc:  # noqa: BLE001
            await self.send({"type": "error", "request_id": request_id, "error": repr(exc), "status": 500})
            self.bus.publish(
                new_event(
                    "task.failed",
                    phase=EventPhase.GATEWAY,
                    session_id=self.session_id,
                    error=repr(exc),
                    ws_message_type=kind,
                )
            )
