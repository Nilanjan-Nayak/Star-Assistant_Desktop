"""STAR 2.0 — the modular monolith that grows *around* the existing Star HUD.

Layer order (imports only flow down)::

    gateway → brain → orchestrator → agents → tools → workspace → (agent/ low-level base)
    cross-cutting: config · security · memory · learning · observability · database · voice

Nothing in this package may rewrite ``Frontend/`` or fork ``agent/``; both are
adapted. See ``docs/STAR_2_0_ARCHITECTURE.md``.
"""

from __future__ import annotations

from typing import Final

__version__: Final[str] = "2.0.0"
__codename__: Final[str] = "STAR 2.0"
PROTOCOL_VERSION: Final[str] = "star.mission.v1"

__all__ = ["__version__", "__codename__", "PROTOCOL_VERSION"]
