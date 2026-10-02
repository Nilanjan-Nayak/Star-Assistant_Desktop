#!/usr/bin/env python3
"""Start the STAR 2.0 gateway (HTTP + WebSocket + ops console).

    python scripts/run_gateway.py                 # defaults from env / .env
    python scripts/run_gateway.py --port 9000     # explicit port
    python scripts/run_gateway.py --port 0        # ephemeral port (prints it)

The existing desktop HUD is unaffected: launch it separately with ``python run.py``
and (optionally) set ``STAR_GATEWAY_URL`` so the two talk to each other.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Backend.star.config.settings import get_settings  # noqa: E402
from Backend.star.gateway.server import GatewayServer  # noqa: E402
from Backend.star.main import build_application  # noqa: E402


async def _run(host: str | None, port: int | None) -> None:
    settings = get_settings()
    app = build_application(settings)
    await app.startup()
    server = GatewayServer(app, settings, host=host, port=port)
    await server.start()
    print(f"[STAR 2.0] gateway  → {server.url}")
    print(f"[STAR 2.0] console  → {server.url}/")
    print(f"[STAR 2.0] websocket→ {server.url.replace('http', 'ws')}/ws")
    print(f"[STAR 2.0] dry_run={settings.security.dry_run} safety={settings.security.safety_level}")
    print("[STAR 2.0] Ctrl+C to stop")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.host, args.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
