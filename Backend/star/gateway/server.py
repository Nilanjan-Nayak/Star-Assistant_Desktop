"""The gateway server: one asyncio listener for REST + WebSocket + the console.

Zero third-party dependencies (no uvicorn/fastapi), so the Star desktop app can
ship the gateway with the same ``pip install`` it already needs. Binds
``0.0.0.0`` by default so container/preview environments can proxy it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from typing import Any

from Backend.star.config.settings import Settings, get_settings
from Backend.star.contracts import StarApplicationProtocol
from Backend.star.gateway.api import Router, build_router
from Backend.star.gateway.http import (
    HttpError,
    HttpRequest,
    HttpResponse,
    error_response,
    read_request,
)
from Backend.star.gateway.websocket import WebSocketSession
from Backend.star.observability.events import EventPhase, StarEventBus, get_event_bus
from Backend.star.observability.metrics import get_metrics

__all__ = ["GatewayServer"]


class GatewayServer:
    def __init__(
        self,
        app: StarApplicationProtocol,
        settings: Settings | None = None,
        *,
        host: str | None = None,
        port: int | None = None,
        bus: StarEventBus | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.app = app
        self.host = host or self.settings.gateway.host
        self.port = port if port is not None else self.settings.gateway.port
        self.bus = bus or get_event_bus()
        self.router: Router = build_router(app)
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[Any]] = set()
        self.requests = 0
        self.errors = 0
        self.started_at: float | None = None
        self.shutdown_timeout: float = 5.0
        self.idle_timeout: float = 300.0

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._server is not None:
            return
        with contextlib.suppress(RuntimeError):
            self.bus.bind_loop(asyncio.get_running_loop())
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self.host,
            port=self.port,
            reuse_address=True,
            limit=256 * 1024,
        )
        self.started_at = time.time()
        bound = self._server.sockets[0].getsockname() if self._server.sockets else (self.host, self.port)
        self.port = int(bound[1])
        self.bus.emit(
            "agent.ready",
            phase=EventPhase.GATEWAY,
            host=str(bound[0]),
            port=self.port,
            dry_run=self.settings.security.dry_run,
        )
        get_metrics().inc("gateway.started", host=str(bound[0]), port=str(self.port))

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None  # noqa: S101 — start() guarantees it
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Graceful shutdown.

        Order matters: since Python 3.12 ``Server.wait_closed()`` also waits for
        every live connection handler to finish, so connection tasks are
        cancelled *first* and both awaits are time-boxed. A wedged client can
        never keep the gateway (or a test session) alive.
        """
        if self._server is None:
            return
        server, self._server = self._server, None
        server.close()
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self.shutdown_timeout)
        with contextlib.suppress(asyncio.CancelledError, OSError, TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=self.shutdown_timeout)
        self._connections.clear()
        self.bus.emit("session.closed", phase=EventPhase.GATEWAY, reason="server.stop")
        with contextlib.suppress(Exception):
            await self.app.aclose()

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{host}:{self.port}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.is_running,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "connections": len(self._connections),
            "requests": self.requests,
            "errors": self.errors,
            "uptime_s": round(time.time() - self.started_at, 2) if self.started_at else 0.0,
        }

    # ── connection handling ─────────────────────────────────────────────────
    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        peer = writer.get_extra_info("peername")
        remote = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) and peer else "unknown"
        try:
            await self._serve_http(reader, writer, remote)
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError, OSError):
            pass
        except Exception as exc:  # noqa: BLE001 — never let one socket kill the listener
            self.errors += 1
            self.bus.emit("task.failed", phase=EventPhase.GATEWAY, error=repr(exc), remote=remote)
        finally:
            with contextlib.suppress(OSError):
                writer.close()
                await writer.wait_closed()
            if task is not None:
                self._connections.discard(task)

    async def _serve_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, remote: str) -> None:
        for _ in range(1000):  # keep-alive cap; a client that loops forever gets dropped
            try:
                request = await asyncio.wait_for(
                    read_request(reader, max_body=self.settings.gateway.max_body_bytes, remote=remote),
                    timeout=self.idle_timeout,
                )
            except TimeoutError:
                return
            if request is None:
                return

            if request.wants_websocket:
                session = WebSocketSession(self.app, request, reader, writer, bus=self.bus)
                await session.run()
                return

            response = await self._respond(request)
            keep_alive = request.version.startswith("1.1") and request.header("connection").lower() != "close"
            response = self._with_cors(response, request)
            try:
                writer.write(response.to_bytes(keep_alive=keep_alive))
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                return
            if not keep_alive:
                return

    async def _respond(self, request: HttpRequest) -> HttpResponse:
        self.requests += 1
        started = time.perf_counter()
        metrics = get_metrics()
        try:
            if request.method == "OPTIONS":
                return HttpResponse(status=204, body=b"", content_type="text/plain")
            response = await self.router.dispatch(request)
            metrics.observe("gateway.request.ms", (time.perf_counter() - started) * 1000.0, path=request.path)
            return response
        except HttpError as exc:
            self.errors += 1
            return error_response(exc.status, exc.message, code=exc.code)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            metrics.inc("gateway.error", path=request.path)
            self.bus.emit("task.failed", phase=EventPhase.GATEWAY, error=repr(exc), path=request.path)
            return error_response(500, f"internal gateway error: {exc!r}")

    def _with_cors(self, response: HttpResponse, request: HttpRequest) -> HttpResponse:
        origin = request.header("origin")
        allowed = self.settings.gateway.allowed_origins
        if origin and ("*" in allowed or origin in allowed):
            response.headers.setdefault("Access-Control-Allow-Origin", origin)
            response.headers.setdefault("Vary", "Origin")
            response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
            response.headers.setdefault("Access-Control-Max-Age", "600")
        return response


async def find_free_port(host: str = "127.0.0.1") -> int:
    """Utility for tests/scripts: ask the OS for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def describe_server(server: GatewayServer) -> dict[str, Any]:
    """Convenience for scripts: server + app health in one dict."""
    return {"server": server.snapshot(), "app": server.app.health()}
