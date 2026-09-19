"""
Backward-compatibility shim.
Tools have moved to:
  - Backend/tools/system/telemetry.py (get_system_status, lock_workstation, open_special_folder)
  - Backend/tools/apps/launcher.py (launch_application, close_application)
  - Backend/tools/media/web_media.py (search_web, play_media)
  - Backend/tools/agent/vision.py (take_screenshot)
  - Backend/tools/utilities/calculator.py (calculate_math)
"""

from .system.telemetry import (
    get_system_status,
    lock_workstation,
    open_special_folder,
)
from .apps.launcher import (
    launch_application,
    close_application,
)
from .media.web_media import (
    search_web,
    play_media,
)
from .agent.vision import (
    take_screenshot,
)
from .utilities.calculator import (
    calculate_math,
)

__all__ = [
    "get_system_status",
    "lock_workstation",
    "open_special_folder",
    "launch_application",
    "close_application",
    "search_web",
    "play_media",
    "take_screenshot",
    "calculate_math",
]
