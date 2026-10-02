"""Phase 1 — HTTP/WebSocket framing tests (pure functions, no sockets)."""

from __future__ import annotations

import json

import pytest

from Backend.star.gateway.http import (
    HttpError,
    HttpRequest,
    HttpResponse,
    WSFrameDecoder,
    WSOpcode,
    error_response,
    html_response,
    json_response,
    parse_request_head,
    text_response,
    ws_accept_key,
    ws_client_frame,
    ws_close_frame,
    ws_encode,
)

HEAD = (
    b"POST /api/v1/chat?session_id=hud&kinds=task.* HTTP/1.1\r\n"
    b"Host: 127.0.0.1:8765\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 17\r\n"
    b"Connection: keep-alive\r\n"
    b"\r\n"
)


def test_parse_request_head_extracts_method_path_query_headers() -> None:
    request, length = parse_request_head(HEAD, remote="127.0.0.1:5555")
    assert request.method == "POST"
    assert request.path == "/api/v1/chat"
    assert request.query == {"session_id": "hud", "kinds": "task.*"}
    assert request.header("content-type") == "application/json"
    assert request.header("CONTENT-TYPE") == "application/json", "headers are case-insensitive"
    assert request.content_type == "application/json"
    assert request.remote == "127.0.0.1:5555"
    assert length == 17
    assert request.body == b""


def test_parse_request_head_rejects_garbage() -> None:
    with pytest.raises(HttpError) as exc:
        parse_request_head(b"")
    assert exc.value.status == 400
    with pytest.raises(HttpError):
        parse_request_head(b"GET\r\n\r\n")
    with pytest.raises(HttpError):
        parse_request_head(b"GET / HTTP/1.1\r\nbad-header-line\r\n\r\n")
    with pytest.raises(HttpError):
        parse_request_head(b"GET / HTTP/1.1\r\nContent-Length: abc\r\n\r\n")


def test_websocket_upgrade_detection() -> None:
    head = (
        b"GET /ws HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    request, length = parse_request_head(head)
    assert request.wants_websocket is True
    assert length == 0
    plain, _ = parse_request_head(b"GET /ws HTTP/1.1\r\n\r\n")
    assert plain.wants_websocket is False


def test_request_json_and_query_helpers() -> None:
    request = HttpRequest(
        method="POST",
        target="/x?a=5&b=true",
        path="/x",
        query={"a": "5", "b": "true"},
        headers={},
        body=json.dumps({"text": "হ্যালো star"}).encode(),
    )
    assert request.json() == {"text": "হ্যালো star"}
    assert request.query_int("a", 0) == 5
    assert request.query_int("missing", 7) == 7
    assert request.query_bool("b", False) is True
    with pytest.raises(HttpError) as exc:
        request.query_int("b", 0)
    assert exc.value.status == 422
    with pytest.raises(HttpError):
        HttpRequest(method="POST", target="/x", path="/x", query={}, headers={}, body=b"{oops").json()
    assert HttpRequest(method="GET", target="/x", path="/x", query={}, headers={}).json() == {}


def test_response_serialisation() -> None:
    response = json_response({"ok": True, "text": "ভালো আছি"}, status=201, XTrace="abc")
    raw = response.to_bytes()
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 201 Created")
    assert b"Content-Type: application/json; charset=utf-8" in head
    assert b"Connection: keep-alive" in head
    assert b"X-Content-Type-Options: nosniff" in head
    assert b"XTrace: abc" in head
    assert b"Content-Length: " + str(len(body)).encode() in head
    assert json.loads(body.decode()) == {"ok": True, "text": "ভালো আছি"}, "Bengali must not be escaped"

    closed = text_response("bye").to_bytes(keep_alive=False)
    assert b"Connection: close" in closed
    assert b"text/plain" in closed
    assert b"text/html" in html_response("<h1>hi</h1>").to_bytes()


def test_error_response_shape() -> None:
    response = error_response(404, "nope", code="missing")
    payload = json.loads(response.body.decode())
    assert response.status == 404
    assert payload == {"ok": False, "error": {"status": 404, "message": "nope", "code": "missing"}}
    default = json.loads(error_response(429, "slow down").body.decode())
    assert default["error"]["code"] == "too_many_requests"


def test_ws_accept_key_matches_rfc_6455_vector() -> None:
    assert ws_accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("size", [0, 1, 125, 126, 65535, 65536, 200000])
def test_ws_encode_length_headers(size: int) -> None:
    frame = ws_encode(b"x" * size)
    assert frame[0] == 0x81
    length_byte = frame[1]
    assert length_byte & 0x80 == 0, "server frames must not be masked"
    if size < 126:
        assert length_byte == size
        assert len(frame) == 2 + size
    elif size <= 0xFFFF:
        assert length_byte == 126
        assert int.from_bytes(frame[2:4], "big") == size
    else:
        assert length_byte == 127
        assert int.from_bytes(frame[2:10], "big") == size
    decoded = WSFrameDecoder().feed(frame)
    assert len(decoded) == 1
    assert decoded[0].opcode == WSOpcode.TEXT
    assert len(decoded[0].payload) == size


def test_decoder_unmasks_client_frames() -> None:
    payload = json.dumps({"type": "chat", "text": "volume 40 koro"}).encode()
    frames = WSFrameDecoder().feed(ws_client_frame(payload, mask_key=b"\x01\x02\x03\x04"))
    assert len(frames) == 1
    assert frames[0].json() == {"type": "chat", "text": "volume 40 koro"}


def test_decoder_handles_byte_by_byte_delivery() -> None:
    raw = ws_client_frame(b"ping-me")
    decoder = WSFrameDecoder()
    out = []
    for byte in raw:
        out.extend(decoder.feed(bytes([byte])))
    assert [frame.payload for frame in out] == [b"ping-me"]


def test_decoder_reassembles_fragments_and_interleaves_control() -> None:
    first = bytes([0x01, 0x03]) + b"abc"          # text, fin=0
    ping = ws_encode(b"", WSOpcode.PING)
    cont = bytes([0x80, 0x03]) + b"def"            # continuation, fin=1
    frames = WSFrameDecoder().feed(first + ping + cont)
    assert [frame.opcode for frame in frames] == [WSOpcode.PING, WSOpcode.TEXT]
    assert frames[-1].payload == b"abcdef"


def test_decoder_rejects_protocol_violations() -> None:
    decoder = WSFrameDecoder()
    with pytest.raises(HttpError):
        decoder.feed(bytes([0x80, 0x02]) + b"orphan continuation")
    decoder2 = WSFrameDecoder()
    with pytest.raises(HttpError):
        decoder2.feed(bytes([0x09, 0x00]))  # fragmented ping
    decoder3 = WSFrameDecoder(max_message_bytes=8)
    with pytest.raises(HttpError) as exc:
        decoder3.feed(ws_encode(b"0123456789abcdef"))
    assert exc.value.status == 413


def test_close_frame_carries_code_and_reason() -> None:
    frame = WSFrameDecoder().feed(ws_close_frame(1001, "going away"))[0]
    assert frame.opcode == WSOpcode.CLOSE
    assert frame.close_code() == 1001
    assert b"going away" in frame.payload
    assert frame.is_control is True
    assert WSFrameDecoder().feed(ws_close_frame())[0].close_code() == 1000


def test_frame_text_is_lossy_safe() -> None:
    frame = WSFrameDecoder().feed(ws_encode(b"\xff\xfe bad utf8"))[0]
    assert isinstance(frame.text, str)


def test_http_response_default_is_json_no_store() -> None:
    response = HttpResponse()
    raw = response.to_bytes()
    assert b"Cache-Control: no-store" in raw
    assert b"Content-Length: 0" in raw
