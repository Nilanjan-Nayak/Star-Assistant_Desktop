"""Structured JSON logging with bindable context and a correlation id."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Final, TextIO

from agent.core.ids import CorrelationId, new_correlation_id

_CORRELATION: ContextVar[CorrelationId | None] = ContextVar("correlation_id", default=None)

_RESERVED: Final[frozenset[str]] = frozenset(
    {"ts", "level", "logger", "msg", "exc", "correlation_id"}
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = _CORRELATION.get()
        if cid is not None:
            payload["correlation_id"] = str(cid)
        extra = getattr(record, "extra_ctx", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key not in _RESERVED:
                    payload[str(key)] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def get_logger(name: str, *, stream: TextIO | None = None) -> logging.Logger:
    lg = logging.getLogger(name)
    if not lg.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(_JsonFormatter())
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


def log_ctx(logger: logging.Logger, level: int, msg: str, /, **ctx: object) -> None:
    logger.log(level, msg, extra={"extra_ctx": ctx})


def bind_correlation(cid: CorrelationId | None = None) -> Token[CorrelationId | None]:
    return _CORRELATION.set(cid or new_correlation_id())


def reset_correlation(token: Token[CorrelationId | None]) -> None:
    _CORRELATION.reset(token)


def current_correlation() -> CorrelationId | None:
    return _CORRELATION.get()


class BoundLogger:
    """Logger with permanently attached structured context."""

    def __init__(self, logger: logging.Logger, **ctx: object) -> None:
        self._logger = logger
        self._ctx = dict(ctx)

    def bind(self, **ctx: object) -> BoundLogger:
        merged = dict(self._ctx)
        merged.update(ctx)
        return BoundLogger(self._logger, **merged)

    def _emit(self, level: int, msg: str, **ctx: object) -> None:
        payload = dict(self._ctx)
        payload.update(ctx)
        log_ctx(self._logger, level, msg, **payload)

    def debug(self, msg: str, **ctx: object) -> None:
        self._emit(logging.DEBUG, msg, **ctx)

    def info(self, msg: str, **ctx: object) -> None:
        self._emit(logging.INFO, msg, **ctx)

    def warning(self, msg: str, **ctx: object) -> None:
        self._emit(logging.WARNING, msg, **ctx)

    def error(self, msg: str, **ctx: object) -> None:
        self._emit(logging.ERROR, msg, **ctx)
