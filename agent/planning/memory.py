from __future__ import annotations

import logging
from pathlib import Path

from agent.core.enums import AgentState
from agent.core.logging import get_logger, log_ctx
from agent.planning.episode import Episode

_log = get_logger("agent.memory")


class EpisodicMemory:
    def __init__(self, path: Path = Path("episodes.jsonl"), max_recall: int = 5) -> None:
        self.path = path
        self.max_recall = max_recall
        self._episodes: list[Episode] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._episodes.append(Episode.model_validate_json(line))
        except Exception as exc:
            log_ctx(_log, logging.WARNING, "memory.load.failed", error=str(exc))

    def save(self, episode: Episode) -> None:
        self._episodes.append(episode)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    episode.model_dump_json(exclude={"step_count", "succeeded"}) + "\n"
                )
        except Exception as exc:
            log_ctx(_log, logging.WARNING, "memory.save.failed", error=str(exc))

    def recall_similar(self, goal: str) -> list[Episode]:
        goal_tokens = set(goal.lower().split())
        scored = [
            (len(goal_tokens & set(ep.goal.lower().split())), ep)
            for ep in self._episodes
            if ep.final_state is AgentState.SUCCEEDED
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [ep for score, ep in scored[: self.max_recall] if score > 0]

    @property
    def size(self) -> int:
        return len(self._episodes)
