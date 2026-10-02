"""Dependency-free HTTP/1.1 request/response + RFC 6455 WebSocket framing.

Why hand-rolled?  The blueprint demands a *modular monolith* that must run on the
developer's Windows desktop **without adding a web framework dependency** to a
repo whose only hard requirements today are ``pydantic`` + ``typing-extensions``.
Everything here is stdlib ``asyncio`` streams, and every piece is unit-testable
without opening a socket (``tests/test_star2_http.py``).

Scope: exactly what the Star gateway needs — keep-alive JSON requests, static
HTML, and a bidirectional WebSocket event stream. No TLS, no HTTP/2, no CGI.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl, unquote, urlsplit

__all__ = [
    "HttpError",
    "HttpRequest",
    "HttpResponse",
    "STATUS_TEXT",
    "error_response",
    "html_response",
    "json_response",
    "parse_request_head",
    "read_request",
    "text_response",
    "WSFrame",
    "WSFrameDecoder",
    "WSOpcode",
    "WS_GUID",
    "ws_accept_key",
    "ws_close_frame",
    "ws_encode",
    "ws_text_frame",
    "ws_client_frame",
]

STATUS_TEXT: Final[dict[int, str]] = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    409: "Conflict",
    413: "Payload Too Large",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}


class HttpError(Exception):
    """Raised by handlers; converted into a JSON error response by the server."""

    def __init__(self, status: int, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or _default_code(status)


# ─────────────────────────────────────────────────────────────────────────────
#  Request
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    target: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""
    version: str = "1.1"
    remote: str = ""

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def content_type(self) -> str:
        return self.header("content-type").split(";")[0].strip()

    @property
    def wants_websocket(self) -> bool:
        return (
            self.method == "GET"
            and self.header("upgrade").lower() == "websocket"
            and "upgrade" in self.header("connection").lower()
            and bool(self.header("sec-websocket-key"))
        )

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, f"invalid JSON body: {exc}") from exc

    def query_int(self, name: str, default: int) -> int:
        raw = self.query.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError:
            raise HttpError(422, f"query parameter {name!r} must be an integer") from None

    def query_bool(self, name: str, default: bool) -> bool:
        raw = self.query.get(name)
        if raw is None or not raw.strip():
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_request_head(head: bytes, *, remote: str = "") -> tuple[HttpRequest, int]:
    """Parse ``METHOD TARGET HTTP/1.1\\r\\nheaders\\r\\n\\r\\n`` → (request, content_length).

    The returned request has an empty body; the caller reads exactly
    ``content_length`` more bytes (or switches to chunked/WebSocket handling).
    """
    try:
        text = head.decode("iso-8859-1")
    except UnicodeDecodeError as exc:  # pragma: no cover - iso-8859-1 never fails
        raise HttpError(400, "malformed request encoding") from exc
    lines = text.split("\r\n")
    if not lines or not lines[0].strip():
        raise HttpError(400, "empty request line")
    parts = lines[0].split()
    if len(parts) < 2:
        raise HttpError(400, f"malformed request line: {lines[0]!r}")
    method, target = parts[0].upper(), parts[1]
    version = parts[2].lstrip("HTTP/") if len(parts) > 2 else "1.1"

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        if ":" not in line:
            raise HttpError(400, f"malformed header: {line!r}")
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()

    split = urlsplit(target)
    path = unquote(split.path or "/")
    query = {k: v for k, v in parse_qsl(split.query, keep_blank_values=True)}
    request = HttpRequest(
        method=method,
        target=target,
        path=path,
        query=query,
        headers=headers,
        version=version or "1.1",
        remote=remote,
    )
    length_raw = headers.get("content-length", "0")
    try:
        length = int(length_raw)
    except ValueError as exc:
        raise HttpError(400, "invalid content-length") from exc
    if length < 0:
        raise HttpError(400, "negative content-length")
    return request, length


async def read_request(
    reader: Any,
    *,
    max_body: int = 2 * 1024 * 1024,
    remote: str = "",
) -> HttpRequest | None:
    """Read one full request. ``None`` means the peer closed the connection."""
    import asyncio

    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError as exc:
        raise HttpError(400, "request head too large") from exc

    request, length = parse_request_head(bytes(head), remote=remote)
    if length > max_body:
        raise HttpError(413, f"body of {length} bytes exceeds limit {max_body}")
    if length:
        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise HttpError(400, "truncated request body") from exc
    elif request.header("transfer-encoding").lower() == "chunked":
        body = await _read_chunked(reader, max_body=max_body)
    else:
        body = b""

    return HttpRequest(
        method=request.method,
        target=request.target,
        path=request.path,
        query=request.query,
        headers=request.headers,
        body=body,
        version=request.version,
        remote=request.remote,
    )


async def _read_chunked(reader: Any, *, max_body: int) -> bytes:
    import asyncio

    out = bytearray()
    while True:
        line = await reader.readline()
        if not line:
            raise HttpError(400, "truncated chunked body")
        size_raw = line.split(b";")[0].strip()
        try:
            size = int(size_raw, 16)
        except ValueError as exc:
            raise HttpError(400, "invalid chunk size") from exc
        if size == 0:
            await reader.readline()  # trailing CRLF
            return bytes(out)
        if len(out) + size > max_body:
            raise HttpError(413, "chunked body exceeds limit")
        try:
            out += await reader.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise HttpError(400, "truncated chunk") from exc
        await reader.readexactly(2)  # CRLF after each chunk


# ─────────────────────────────────────────────────────────────────────────────
#  Response
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class HttpResponse:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    def to_bytes(self, *, keep_alive: bool = True) -> bytes:
        reason = STATUS_TEXT.get(self.status, "Unknown")
        lines = [f"HTTP/1.1 {self.status} {reason}"]
        headers = {
            "Content-Type": self.content_type,
            "Content-Length": str(len(self.body)),
            "Connection": "keep-alive" if keep_alive else "close",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            **self.headers,
        }
        lines.extend(f"{k}: {v}" for k, v in headers.items())
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")
        return head + self.body


def json_response(payload: Any, status: int = 200, **headers: str) -> HttpResponse:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return HttpResponse(status=status, body=body, headers=dict(headers))


def text_response(text: str, status: int = 200, **headers: str) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        headers=dict(headers),
    )


def html_response(html: str, status: int = 200, **headers: str) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=html.encode("utf-8"),
        content_type="text/html; charset=utf-8",
        headers=dict(headers),
    )


def _default_code(status: int) -> str:
    return STATUS_TEXT.get(status, "error").lower().replace(" ", "_")


def error_response(status: int, message: str, *, code: str | None = None, **extra: Any) -> HttpResponse:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"status": status, "message": message, "code": code or _default_code(status)},
    }
    payload.update(extra)
    return json_response(payload, status=status)


# ─────────────────────────────────────────────────────────────────────────────
#  WebSocket (RFC 6455)
# ─────────────────────────────────────────────────────────────────────────────

WS_GUID: Final[str] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_BYTES: Final[int] = 8 * 1024 * 1024


class WSOpcode:
    CONTINUATION: Final[int] = 0x0
    TEXT: Final[int] = 0x1
    BINARY: Final[int] = 0x2
    CLOSE: Final[int] = 0x8
    PING: Final[int] = 0x9
    PONG: Final[int] = 0xA


def ws_accept_key(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key.strip() + WS_GUID).encode("ascii")).digest()  # noqa: S324
    return base64.b64encode(digest).decode("ascii")


def ws_encode(payload: bytes, opcode: int = WSOpcode.TEXT, *, fin: bool = True) -> bytes:
    """Server → client frames are never masked (RFC 6455 §5.1)."""
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", first, size)
    elif size <= 0xFFFF:
        header = struct.pack("!BBH", first, 126, size)
    else:
        header = struct.pack("!BBQ", first, 127, size)
    return header + payload


def ws_text_frame(text: str) -> bytes:
    return ws_encode(text.encode("utf-8"), WSOpcode.TEXT)


def ws_client_frame(payload: bytes, opcode: int = WSOpcode.TEXT, *, mask_key: bytes | None = None) -> bytes:
    """Client → server frame. RFC 6455 §5.3 requires masking; used by tests/scripts."""
    import os

    key = mask_key or os.urandom(4)
    if len(key) != 4:
        raise ValueError("mask key must be exactly 4 bytes")
    first = 0x80 | (opcode & 0x0F)
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", first, 0x80 | size)
    elif size <= 0xFFFF:
        header = struct.pack("!BBH", first, 0x80 | 126, size)
    else:
        header = struct.pack("!BBQ", first, 0x80 | 127, size)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return header + key + masked


def ws_close_frame(code: int = 1000, reason: str = "") -> bytes:
    payload = struct.pack("!H", code) + reason.encode("utf-8")[:123]
    return ws_encode(payload, WSOpcode.CLOSE)


@dataclass(frozen=True, slots=True)
class WSFrame:
    opcode: int
    payload: bytes

    @property
    def is_control(self) -> bool:
        return self.opcode >= 0x8

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise HttpError(400, f"invalid websocket JSON: {exc}") from exc

    def close_code(self) -> int:
        if len(self.payload) >= 2:
            return int(struct.unpack("!H", self.payload[:2])[0])
        return 1005


class _NeedMore:
    """Sentinel: the decoder needs more bytes before it can emit anything."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<need-more>"


_NEED_MORE: Final[_NeedMore] = _NeedMore()


class WSFrameDecoder:
    """Incremental decoder: ``feed(bytes)`` → zero or more complete frames.

    Handles 7/16/64-bit lengths, client masking, interleaved control frames and
    fragmented data messages (reassembled into one frame).
    """

    def __init__(self, *, max_message_bytes: int = MAX_FRAME_BYTES) -> None:
        self._buf = bytearray()
        self._frag_opcode: int | None = None
        self._frag_payload = bytearray()
        self.max_message_bytes = max_message_bytes

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[WSFrame]:
        """Return every message completed by ``data``.

        A ``None`` from :meth:`_take_one` means "fragment buffered, message not
        finished yet" — that must NOT stop the loop, because a control frame or
        another complete frame may follow in the same buffer (RFC 6455 §5.4).
        Only ``_NEED_MORE`` stops it.
        """
        self._buf.extend(data)
        frames: list[WSFrame] = []
        while True:
            frame = self._take_one()
            if frame is _NEED_MORE:
                break
            if frame is not None:
                frames.append(frame)
        return frames

    def _take_one(self) -> WSFrame | _NeedMore | None:
        buf = self._buf
        if len(buf) < 2:
            return _NEED_MORE
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < offset + 2:
                return _NEED_MORE
            length = int(struct.unpack("!H", bytes(buf[offset : offset + 2]))[0])
            offset += 2
        elif length == 127:
            if len(buf) < offset + 8:
                return _NEED_MORE
            length = int(struct.unpack("!Q", bytes(buf[offset : offset + 8]))[0])
            offset += 8
        if length > self.max_message_bytes:
            raise HttpError(413, f"websocket frame of {length} bytes exceeds limit")
        mask_key = b""
        if masked:
            if len(buf) < offset + 4:
                return _NEED_MORE
            mask_key = bytes(buf[offset : offset + 4])
            offset += 4
        if len(buf) < offset + length:
            return _NEED_MORE
        payload = bytearray(buf[offset : offset + length])
        del buf[: offset + length]
        if masked and mask_key:
            for i in range(len(payload)):
                payload[i] ^= mask_key[i % 4]

        if opcode in (WSOpcode.CLOSE, WSOpcode.PING, WSOpcode.PONG):
            if not fin:
                raise HttpError(400, "fragmented control frame")
            return WSFrame(opcode=opcode, payload=bytes(payload))

        if opcode == WSOpcode.CONTINUATION:
            if self._frag_opcode is None:
                raise HttpError(400, "continuation frame without a start frame")
            self._frag_payload.extend(payload)
            if len(self._frag_payload) > self.max_message_bytes:
                raise HttpError(413, "fragmented websocket message too large")
            if fin:
                complete = WSFrame(opcode=self._frag_opcode, payload=bytes(self._frag_payload))
                self._frag_opcode = None
                self._frag_payload.clear()
                return complete
            return None

        if not fin:
            self._frag_opcode = opcode
            self._frag_payload = bytearray(payload)
            return None
        return WSFrame(opcode=opcode, payload=bytes(payload))
