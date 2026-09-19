"""
Vision and Screen Perception Tools.
Provides desktop screen capture and OCR text reading.
"""

import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from ..registry import register_tool
from ...agent_bridge import get_agent_bridge


@register_tool(
    name="see_screen",
    description="Capture screen and use OCR to read textual elements visible on the monitor."
)
def see_screen(query: str = "*", save_path: str = "see.png") -> Dict[str, Any]:
    """Capture screen pixels, perform OCR extraction, and find matching elements."""
    try:
        bridge = get_agent_bridge()
        res = bridge.see(query=query, save_path=save_path)
        return {
            "success": res.get("ok", False),
            "message": res.get("message", ""),
            "data": res.get("data", {})
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="take_screenshot",
    description="Capture desktop screen and save to Pictures/Screenshots."
)
def take_screenshot() -> dict:
    """Take a full screenshot of the desktop screen."""
    pic_dir = Path.home() / "Pictures" / "Screenshots"
    pic_dir.mkdir(parents=True, exist_ok=True)
    filename = f"screenshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    filepath = pic_dir / filename

    try:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen:
            pixmap = screen.grabWindow(0)
            if pixmap.save(str(filepath)):
                return {"screenshot_path": str(filepath), "filename": filename}
    except Exception:
        pass

    try:
        import PIL.ImageGrab as ImageGrab
        shot = ImageGrab.grab()
        shot.save(filepath)
        return {"screenshot_path": str(filepath), "filename": filename}
    except Exception as e:
        return {"error": str(e)}
