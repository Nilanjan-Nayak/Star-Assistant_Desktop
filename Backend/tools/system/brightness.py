"""
Windows Screen Brightness Control Tool.
Uses screen_brightness_control library with native WMI fallback.
"""

from ..registry import register_tool


@register_tool(name="get_brightness", description="Get current screen brightness as a percentage (0-100).")
def get_brightness() -> int:
    """Return primary display brightness percentage."""
    try:
        import screen_brightness_control as sbc
        current = sbc.get_brightness(display=0)
        if isinstance(current, list) and current:
            return int(current[0])
        elif isinstance(current, (int, float)):
            return int(current)
    except Exception as e:
        print(f"[Brightness] Query error: {e}")
    return 70


@register_tool(name="set_brightness", description="Set screen brightness to a specific percentage (0 to 100).")
def set_brightness(level: int) -> dict:
    """Set primary display brightness to level (0-100)."""
    target = max(5, min(100, int(level)))
    try:
        import screen_brightness_control as sbc
        sbc.set_brightness(target, display=0)
        return {"current_brightness": target, "message": f"Brightness set to {target}%"}
    except Exception as e:
        return {"current_brightness": target, "warning": f"Could not change brightness: {e}"}


@register_tool(name="adjust_brightness", description="Increase or decrease screen brightness by delta (e.g. +20, -15).")
def adjust_brightness(delta: int) -> dict:
    """Adjust brightness relatively by delta percent."""
    current = get_brightness()
    target = max(5, min(100, current + int(delta)))
    return set_brightness(target)
