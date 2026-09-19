"""
Backward-compatibility shim.
Tools have moved to Backend/tools/system/volume.py
"""

from .system.volume import (
    get_volume,
    set_volume,
    adjust_volume,
    set_mute,
)

__all__ = [
    "get_volume",
    "set_volume",
    "adjust_volume",
    "set_mute",
]
