"""
Backward-compatibility shim.
Tools have moved to Backend/tools/system/brightness.py
"""

from .system.brightness import (
    get_brightness,
    set_brightness,
    adjust_brightness,
)

__all__ = [
    "get_brightness",
    "set_brightness",
    "adjust_brightness",
]
