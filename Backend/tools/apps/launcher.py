"""
Application Management Tool.
Provides functionality to launch desktop apps, web services, and terminate running processes.
"""

import subprocess
import webbrowser

from ..registry import register_tool


@register_tool(name="launch_application", description="Launch common Windows desktop applications or websites (e.g. 'youtube', 'google', 'chrome', 'notepad', 'calc', 'vscode', 'whatsapp', 'facebook').")
def launch_application(app_name: str) -> dict:
    """Launch an application or web service by common name."""
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
