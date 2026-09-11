# Import for side effect: registers all built-ins via @register_skill
from agent.skills.builtins import brightness, launch, screenshot, see, volume, youtube  # noqa: F401

__all__ = ["brightness", "launch", "screenshot", "see", "volume", "youtube"]
