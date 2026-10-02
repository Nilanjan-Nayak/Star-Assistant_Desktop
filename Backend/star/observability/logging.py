"""Structured JSONL logging for STAR 2.0.

Reuses ``agent.core.logging.get_logger`` (same JSON formatter, same correlation-id
injection) and adds a rotating-ish file sink under ``logs/`` so a run can be
replayed after the fact. Redaction is applied here as a last line of defence —
the real secret policy lives in ``Backend/star/security/secrets.py``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.core.logging import get_logger

__all__ = [
    "JsonlSink",
    "StarJsonlHandler",
    "attach_file_logging",
    "get_sink",
    "is_secret_key",
    "redact",
    "redact_payload",
    "star_logger",
]

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(sk|pk|api[_-]?key|apikey|token|secret|password|passwd|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
)


def redact(text: str) -> str:
    """Replace anything that looks like a credential with ``[REDACTED]``."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


#: keys whose *values* are credentials no matter what they look like
_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd|credential|authorization|private[_-]?key|session[_-]?key)"
)

REDACTED = "[REDACTED]"


def is_secret_key(key: Any) -> bool:
    """``True`` when a mapping key names a credential (``api_key``, ``token``, …)."""
    return bool(_SECRET_KEY_RE.search(str(key or "")))


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Scrub credentials from a payload before it is logged, audited or streamed.

    Two rules, both needed:
      * a value that *looks like* a credential is redacted (:func:`redact`)
      * a value stored under a credential-named **key** is redacted whatever it
        looks like — ``{"token": "abc123"}`` has no ``sk-`` shape but is a secret
    """
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if is_secret_key(key):
            cleaned[key] = REDACTED if isinstance(value, (str, bytes, dict, list)) else value
        elif isinstance(value, str):
            cleaned[key] = redact(value)
        elif isinstance(value, dict):
            cleaned[key] = redact_payload(value)
        elif isinstance(value, (list, tuple)):
            cleaned[key] = [
                redact_payload(item) if isinstance(item, dict) else redact(item) if isinstance(item, str) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


class JsonlSink:
    """Append-only JSONL sink. Never raises — logging must not break a task."""

    def __init__(self, path: Path | str, *, max_bytes: int = 8 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        try:
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                self.path.replace(self.path.with_suffix(".1.jsonl"))
        except OSError:
            pass

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"raw": line})
        return out


class StarJsonlHandler(logging.Handler):
    """Logging handler that writes redacted JSONL through a :class:`JsonlSink`."""

    def __init__(self, sink: JsonlSink, *, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        ctx = getattr(record, "extra_ctx", None) or {}
        self.sink.write(
            {
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": redact(record.getMessage()),
                "ctx": redact_payload({str(k): v for k, v in ctx.items()}) if isinstance(ctx, dict) else {},
                "correlation": getattr(record, "correlation_id", None),
            }
        )


_SINK: JsonlSink | None = None


def star_logger(name: str = "star") -> logging.Logger:
    return get_logger(f"star2.{name}")


def attach_file_logging(logs_dir: Path | str, *, level: int = logging.INFO) -> JsonlSink:
    """Send every ``star2.*`` and ``agent.*`` log record to ``logs/star2.jsonl``."""
    global _SINK
    sink = JsonlSink(Path(logs_dir) / "star2.jsonl")
    _SINK = sink

    handler: StarJsonlHandler | None = None
    for name in ("star2", "agent"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        existing = next((h for h in logger.handlers if isinstance(h, StarJsonlHandler)), None)
        if isinstance(existing, StarJsonlHandler):
            existing.sink = sink          # re-point the same handler (tests reload settings)
            handler = existing
            continue
        if handler is None:
            handler = StarJsonlHandler(sink, level=level)
        logger.addHandler(handler)
    return sink


def get_sink() -> JsonlSink | None:
    return _SINK
