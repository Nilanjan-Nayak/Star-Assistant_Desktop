"""Tiny dependency-free ``.env`` loader.

Only sets variables that are **not** already in the environment, so real env vars
always win over the file (12-factor). Deliberately minimal — no interpolation, no
multiline values, no secret storage. Secrets are never written by Star; they are
only ever read from the environment (see ``Backend/star/security/secrets.py``).
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_dotenv_file", "parse_dotenv"]


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines, ignoring comments and blanks."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_dotenv_file(path: Path | str = ".env", *, override: bool = False) -> dict[str, str]:
    """Load ``path`` into ``os.environ``. Returns the pairs that were applied."""
    file = Path(path)
    if not file.is_file():
        return {}
    try:
        pairs = parse_dotenv(file.read_text(encoding="utf-8"))
    except OSError:
        return {}
    applied: dict[str, str] = {}
    for key, value in pairs.items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied
