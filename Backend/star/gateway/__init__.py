"""STAR 2.0 gateway — HTTP + WebSocket surface for the Star brain.

Public objects::

    GatewayServer   start/stop the asyncio server (bind 0.0.0.0 for previews)
    build_router    route table → HttpResponse
    AgentEventBridge  agent.core.events.BUS → StarEventBus
    CONSOLE_HTML    the developer ops console (diagnostics, not the Star HUD)
"""

from __future__ import annotations

from Backend.star.gateway.api import Router, build_router
from Backend.star.gateway.events import AgentEventBridge
from Backend.star.gateway.server import GatewayServer
from Backend.star.gateway.websocket import WebSocketSession

__all__ = ["AgentEventBridge", "GatewayServer", "Router", "WebSocketSession", "build_router"]
