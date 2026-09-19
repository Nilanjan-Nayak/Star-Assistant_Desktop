"""
Backward-compatibility shim.
Tools have moved to:
  - Backend/tools/agent/autonomous.py (execute_autonomous_goal, execute_computer_skill, get_agent_metrics)
  - Backend/tools/agent/vision.py (see_screen)
  - Backend/tools/agent/memory.py (remember_fact, recall_memory)
"""

from .agent.autonomous import (
    execute_autonomous_goal,
    execute_computer_skill,
    get_agent_metrics,
)
from .agent.vision import (
    see_screen,
)
from .agent.memory import (
    remember_fact,
    recall_memory,
)

__all__ = [
    "execute_autonomous_goal",
    "execute_computer_skill",
    "get_agent_metrics",
    "see_screen",
    "remember_fact",
    "recall_memory",
]
