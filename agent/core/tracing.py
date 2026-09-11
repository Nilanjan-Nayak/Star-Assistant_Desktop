"""Lightweight span context manager with nested parent ids."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from agent.core.clock import DEFAULT_CLOCK, Clock
from agent.core.ids import SpanId, new_span_id
from agent.core.logging import get_logger, log_ctx
from agent.core.metrics import METRICS

_log = get_logger("agent.trace")
_PARENT: ContextVar[SpanId | None] = ContextVar("span_parent", default=None)


@contextmanager
def span(
    name: str,
    /,
    clock: Clock = DEFAULT_CLOCK,
    **attrs: object,
) -> Iterator[dict[str, object]]:
    t0 = clock.monotonic()
    sid = new_span_id()
    parent = _PARENT.get()
    ctx: dict[str, object] = {
        "name": name,
        "span_id": sid,
        "parent_id": parent,
        "attrs": dict(attrs),
    }
    token = _PARENT.set(sid)
    try:
        yield ctx
    except Exception as exc:
        ctx["error"] = repr(exc)
        raise
    finally:
        _PARENT.reset(token)
        dur_ms = (clock.monotonic() - t0) * 1000.0
        ctx["duration_ms"] = dur_ms
        METRICS.observe(f"span.{name}.duration_ms", dur_ms)
        log_ctx(_log, logging.DEBUG, f"span:{name}", **ctx)
