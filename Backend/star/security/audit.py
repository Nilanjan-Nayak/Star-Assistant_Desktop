"""Append-only audit trail for every tool decision and OS action.

Blueprint §7 Phase 4 ("audit trails") and Phase 11 ("audit log with redaction").
Built on the Phase 1 observability primitives — :class:`JsonlSink` for rotation-safe
JSONL writes and :func:`redact_payload` for credential scrubbing — so there is one
redaction implementation in the codebase, not two.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque

from Backend.star.observability.events import EventPhase, StarEventBus
from Backend.star.observability.logging import JsonlSink, redact_payload, star_logger

__all__ = ["AuditLog", "fingerprint"]

_log = star_logger("audit")


def fingerprint(tool: str, arguments: dict[str, Any] | None = None, session_id: str = "") -> str:
    """Stable hash of a call — used to match confirmations to requests."""
    canonical = json.dumps(
        {"tool": str(tool or ""), "args": _canonical(arguments or {}), "session": str(session_id or "")},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    return {str(key): arguments[key] for key in sorted(arguments)}


class AuditLog:
    """One JSONL line per decision, plus an in-memory ring buffer for the API."""

    def __init__(
        self,
        path: Path | str,
        *,
        bus: StarEventBus | None = None,
        sink: JsonlSink | None = None,
        buffer: int = 500,
        emit_events: bool = True,
    ) -> None:
        self.path = Path(path)
        self.bus = bus or StarEventBus()
        self.sink = sink or JsonlSink(self.path)
        self.emit_events = emit_events
        self._buffer: Deque[dict[str, Any]] = deque(maxlen=buffer)
        self._seq = 0
        self.counts: dict[str, int] = {}

    # ── writing ───────────────────────────────────────────────────────────
    def record(self, entry: dict[str, Any], *, kind: str = "tool.decision") -> dict[str, Any]:
        """Redact, stamp, persist and publish one audit entry. Never raises."""
        self._seq += 1
        clean = redact_payload({key: value for key, value in entry.items() if value is not None})
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "seq": self._seq,
            "kind": kind,
            **clean,
        }
        args = record.get("arguments")
        if isinstance(args, dict):
            record["arguments"] = _truncate(args)
            record["arguments_fingerprint"] = fingerprint(
                str(record.get("tool") or ""), args, str(record.get("session_id") or "")
            )
        self._buffer.append(record)
        decision = str(record.get("decision") or "unknown")
        self.counts[decision] = self.counts.get(decision, 0) + 1
        try:
            self.sink.write(record)          # JsonlSink swallows OSError by design
            record["persisted"] = self.path.is_file()
        except Exception as exc:  # noqa: BLE001 — auditing must never break an action
            _log.warning("audit write failed: %s", exc)
            record["persisted"] = False
        if not record["persisted"]:
            _log.warning("audit entry %d not persisted to %s", record["seq"], self.path)
        if self.emit_events:
            self.bus.emit(
                "audit.recorded",
                phase=EventPhase.SECURITY,
                entry_kind=kind,
                tool=record.get("tool"),
                decision=decision,
                risk=record.get("risk"),
                session_id=str(record.get("session_id") or ""),
                request_id=record.get("request_id"),
                ok=record.get("ok"),
            )
        return record

    # ── reading ───────────────────────────────────────────────────────────
    def tail(self, *, limit: int = 50) -> list[dict[str, Any]]:
        items = list(self._buffer)
        return items[-limit:]

    def query(
        self,
        *,
        tool: str | None = None,
        session_id: str | None = None,
        decision: str | None = None,
        risk: str | None = None,
        since_seq: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for record in self._buffer:
            if record.get("seq", 0) <= since_seq:
                continue
            if tool and record.get("tool") != tool:
                continue
            if session_id and record.get("session_id") != session_id:
                continue
            if decision and record.get("decision") != decision:
                continue
            if risk and record.get("risk") != risk:
                continue
            out.append(record)
        return out[-limit:]

    def read_file(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Read persisted lines (survives restarts). Tolerates a missing/corrupt file."""
        if not self.path.exists():
            return []
        lines: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as exc:
            _log.warning("audit read failed: %s", exc)
        return lines

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "entries": self._seq,
            "buffer": len(self._buffer),
            "decisions": dict(self.counts),
            "exists": self.path.is_file(),
            "size_bytes": self.path.stat().st_size if self.path.is_file() else 0,
        }

    def health(self) -> dict[str, Any]:
        writable = self.path.is_file() or self._seq == 0
        return {
            "status": "ok" if writable else "degraded",
            "detail": {"entries": self._seq, "path": str(self.path), "decisions": dict(self.counts)},
        }


def _truncate(arguments: dict[str, Any], *, max_items: int = 24, max_len: int = 400) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for index, (key, value) in enumerate(arguments.items()):
        if index >= max_items:
            out["..."] = f"{len(arguments) - max_items} more"
            break
        if isinstance(value, str) and len(value) > max_len:
            out[key] = value[:max_len] + f"...<{len(value)} chars>"
        else:
            out[key] = value
    return out
