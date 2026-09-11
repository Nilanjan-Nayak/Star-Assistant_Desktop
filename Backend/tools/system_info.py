"""
System Information & Application Launcher Tool.
Provides time, battery, CPU/RAM telemetry, and app launcher.
"""

import os
import subprocess
import datetime
import psutil

from .registry import register_tool


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


@register_tool(name="launch_application", description="Launch common Windows desktop applications or websites (e.g. 'youtube', 'google', 'chrome', 'notepad', 'calc', 'vscode', 'whatsapp', 'facebook').")
def launch_application(app_name: str) -> dict:
    """Launch an application or web service by common name."""
    import webbrowser

    app_key = app_name.lower().strip()
    web_map = {
        "youtube": "https://www.youtube.com",
        "google": "https://www.google.com",
        "facebook": "https://www.facebook.com",
        "whatsapp": "https://web.whatsapp.com",
        "instagram": "https://www.instagram.com",
        "gmail": "https://mail.google.com",
        "github": "https://www.github.com",
        "chatgpt": "https://chatgpt.com",
        "spotify": "https://open.spotify.com",
        "netflix": "https://www.netflix.com",
    }

    if app_key in web_map or app_key.startswith("http://") or app_key.startswith("https://"):
        url = web_map.get(app_key, app_key)
        try:
            webbrowser.open(url)
            return {"launched": app_name, "message": f"Opened {url} in browser"}
        except Exception as e:
            return {"launched": app_name, "error": str(e)}

    app_map = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "calc": "calc.exe",
        "explorer": "explorer.exe",
        "files": "explorer.exe",
        "browser": "start chrome || start msedge",
        "chrome": "start chrome",
        "edge": "start msedge",
        "taskmgr": "taskmgr.exe",
        "cmd": "start cmd",
        "terminal": "start powershell",
        "powershell": "start powershell",
        "code": "code",
        "vscode": "code",
        "paint": "mspaint.exe",
        "settings": "start ms-settings:",
    }
    target = app_map.get(app_key, app_name.strip())
    try:
        if "start " in target:
            subprocess.Popen(target, shell=True)
        else:
            subprocess.Popen([target], shell=True)
        return {"launched": app_name, "message": f"{app_name} opened"}
    except Exception as e:
        return {"launched": app_name, "error": str(e)}


@register_tool(name="close_application", description="Close running desktop applications (e.g. 'chrome', 'notepad', 'calc', 'paint', 'whatsapp', 'taskmgr').")
def close_application(app_name: str) -> dict:
    """Close an application process cleanly or forcefully via taskkill."""
    proc_map = {
        "chrome": "chrome.exe",
        "notepad": "notepad.exe",
        "calculator": "CalculatorApp.exe",
        "calc": "CalculatorApp.exe",
        "paint": "mspaint.exe",
        "whatsapp": "WhatsApp.exe",
        "taskmgr": "Taskmgr.exe",
        "code": "Code.exe",
        "vscode": "Code.exe",
        "spotify": "Spotify.exe",
        "mail": "HxOutlook.exe",
    }
    exe = proc_map.get(app_name.lower().strip(), f"{app_name.strip()}.exe")
    try:
        subprocess.Popen(f"taskkill /im {exe} /f", shell=True)
        return {"closed": app_name, "message": f"{app_name} closed"}
    except Exception as e:
        return {"closed": app_name, "error": str(e)}


@register_tool(name="search_web", description="Search YouTube or Google in the default browser.")
def search_web(query: str, target: str = "youtube") -> dict:
    """Open YouTube or Google search query in browser."""
    import urllib.parse
    import webbrowser

    encoded = urllib.parse.quote_plus(query.strip())
    if target.lower() == "youtube" or "youtube" in target.lower():
        url = f"https://www.youtube.com/results?search_query={encoded}"
    else:
        url = f"https://www.google.com/search?q={encoded}"

    try:
        webbrowser.open(url)
        return {"searched": query, "url": url}
    except Exception as e:
        return {"searched": query, "error": str(e)}


@register_tool(name="take_screenshot", description="Capture desktop screen and save to Pictures/Screenshots.")
def take_screenshot() -> dict:
    """Take a full screenshot of the desktop screen."""
    from pathlib import Path
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
    from pathlib import Path
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


@register_tool(name="calculate_math", description="Evaluate a mathematical expression safely.")
def calculate_math(expression: str) -> dict:
    """Evaluate an arithmetic expression (e.g. '15 * 8', '500 + 350')."""
    import re
    import ast
    import operator

    bengali_digits = {"০": "0", "১": "1", "২": "2", "৩": "3", "৪": "4", "৫": "5", "৬": "6", "৭": "7", "৮": "8", "৯": "9"}
    expr_str = expression
    for bn, en in bengali_digits.items():
        expr_str = expr_str.replace(bn, en)

    ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
    }

    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            left = _eval(node.left)
            right = _eval(node.right)
            return ops[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        raise ValueError("Unsupported operation")

    clean_expr = expr_str.replace("x", "*").replace("×", "*").replace("÷", "/")
    clean_expr = re.sub(r"[^0-9+\-*/(). ]", "", clean_expr)
    try:
        parsed = ast.parse(clean_expr, mode="eval")
        result = _eval(parsed.body)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return {"expression": expression, "result": result}
    except Exception as e:
        return {"expression": expression, "error": str(e)}

