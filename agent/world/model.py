"""WorldModel — the agent's belief about the current screen."""

from __future__ import annotations

from collections import deque
from typing import Deque

from agent.world.capture import CaptureBackend
from agent.world.differ import ScreenDiffer
from agent.world.snapshot import ScreenSnapshot


class WorldModel:
    def __init__(
        self,
        capture: CaptureBackend,
        differ: ScreenDiffer,
        history_size: int = 20,
    ) -> None:
        if history_size < 1:
            raise ValueError("history_size must be >= 1")
        self.capture = capture
        self.differ = differ
        self._current: ScreenSnapshot | None = None
        self._history: Deque[ScreenSnapshot] = deque(maxlen=history_size)

    def observe(self) -> ScreenSnapshot:
        snap = self.capture.grab()
        if self._current is not None:
            self._history.append(self._current)
        self._current = snap
        return snap

    def seed(self, snapshot: ScreenSnapshot) -> None:
        """Inject a snapshot (tests, replay) without touching the capture backend."""
        if self._current is not None:
            self._history.append(self._current)
        self._current = snapshot

    @property
    def current(self) -> ScreenSnapshot:
        if self._current is None:
            return self.observe()
        return self._current

    @property
    def history(self) -> tuple[ScreenSnapshot, ...]:
        return tuple(self._history)

    def changed_since(self, snap: ScreenSnapshot, min_ratio: float = 0.02) -> bool:
        cur = self.observe()
        return self.differ.diff_ratio(snap, cur) >= min_ratio
