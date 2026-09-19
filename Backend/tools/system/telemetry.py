"""
Windows System Telemetry & Workstation Controls.
Provides PC status (battery, CPU, RAM, time), screen lock, and system folder access.
"""

import os
import subprocess
import datetime
from pathlib import Path
import psutil

from ..registry import register_tool


@register_tool(name="get_system_status", description="Get PC telemetry: current time, battery percentage, CPU usage, and RAM.")
def get_system_status() -> dict:
    """Return system telemetry snapshot."""
    now = datetime.datetime.now()
    time_str = now.strftime("%I:%M %p")
    date_str = now.strftime("%A, %d %B %Y")

    cpu_usage = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()

    battery_info = "Desktop (AC)"
    battery = psutil.sensors_battery()
    if battery is not None:
        charging = "Charging" if battery.power_plugged else "On Battery"
        battery_info = f"{battery.percent}% ({charging})"

    return {
        "time": time_str,
        "date": date_str,
        "cpu_usage": f"{cpu_usage}%",
        "ram_usage": f"{ram.percent}%",
        "battery": battery_info,
    }


@register_tool(name="lock_workstation", description="Lock Windows desktop immediately.")
def lock_workstation() -> dict:
    """Lock the Windows PC."""
    try:
        subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
        return {"locked": True}
    except Exception as e:
        return {"locked": False, "error": str(e)}


@register_tool(name="open_special_folder", description="Open common user folders (e.g. 'downloads', 'documents', 'desktop', 'pictures', 'music', 'videos').")
def open_special_folder(folder: str) -> dict:
    """Open a common user folder in Windows File Explorer."""
    folder_key = folder.lower().strip()
    home = Path.home()
    folder_map = {
        "downloads": home / "Downloads",
        "documents": home / "Documents",
        "desktop": home / "Desktop",
        "pictures": home / "Pictures",
        "music": home / "Music",
        "videos": home / "Videos",
    }
    target = folder_map.get(folder_key, home / folder_key)
    try:
        if target.exists():
            os.startfile(str(target))
            return {"opened": folder, "path": str(target)}
        else:
            os.startfile(str(home))
            return {"opened": "home", "path": str(home)}
    except Exception as e:
        return {"error": str(e)}
