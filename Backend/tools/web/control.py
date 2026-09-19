"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — Web Browser Control & Tab Management
═══════════════════════════════════════════════════════════════════════════════

  Provides complete control of web browsers (Chrome, Edge, Firefox, Brave):
    - Opening URLs and websites
    - Tab management (new tab, close tab, reopen closed tab, switch tabs)
    - Navigation (back, forward, refresh, hard refresh)
    - Zoom (in, out, reset)
    - Scrolling (up, down, top of page, bottom of page)
    - In-page search (Ctrl+F)
    - Copying current tab URL via Win32 ctypes
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import time
import urllib.parse
import webbrowser
from typing import Any, Dict, Optional

import pyautogui
pyautogui.FAILSAFE = False


from ..registry import register_tool

user32 = ctypes.windll.user32
VK_MENU = 0x12
SW_RESTORE = 9


def _find_browser_window() -> Optional[int]:
    """Find the HWND of an active web browser window."""
    browser_hwnd = None

    def enum_cb(hwnd, _):
        nonlocal browser_hwnd
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.lower()
                if any(b in title for b in ["chrome", "edge", "firefox", "brave", "opera"]):
                    browser_hwnd = hwnd
                    return False
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_cb), 0)
    return browser_hwnd


def _focus_browser() -> bool:
    """Bring the active browser window to the foreground."""
    hwnd = _find_browser_window()
    if not hwnd:
        return False
    try:
        if user32.GetForegroundWindow() == hwnd:
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(VK_MENU, 0, 2, 0)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.08)
        return True
    except Exception as e:
        print(f"[Web Control] Focus error: {e}")
        return False


def _get_clipboard_text() -> str:
    """Safely get text from Windows clipboard via Win32 ctypes."""
    try:
        k32 = ctypes.windll.kernel32
        if user32.OpenClipboard(0):
            try:
                CF_UNICODETEXT = 13
                if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    h = user32.GetClipboardData(CF_UNICODETEXT)
                    if h:
                        ptr = k32.GlobalLock(h)
                        if ptr:
                            val = ctypes.c_wchar_p(ptr).value or ""
                            k32.GlobalUnlock(h)
                            return val
            finally:
                user32.CloseClipboard()
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Registered Browser Automation Tools
# ─────────────────────────────────────────────────────────────────────────────

@register_tool(
    name="web_open_url",
    description="Open a website URL in the default web browser (automatically adds https:// if omitted)."
)
def web_open_url(url: str) -> Dict[str, Any]:
    """Open a URL in default browser."""
    clean_url = url.strip()
    if not clean_url.startswith(("http://", "https://")):
        clean_url = f"https://{clean_url}"
    try:
        webbrowser.open(clean_url)
        return {"success": True, "url": clean_url, "message": f"ওয়েবসাইট খোলা হয়েছে: {clean_url}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_new_tab",
    description="Open a new blank tab in the web browser, or navigate to an optional URL."
)
def web_new_tab(url: str = "") -> Dict[str, Any]:
    """Press Ctrl+T to open a new tab."""
    try:
        if url:
            return web_open_url(url)
        _focus_browser()
        pyautogui.hotkey("ctrl", "t")
        return {"success": True, "action": "new_tab_opened"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_close_tab",
    description="Close the currently active web browser tab."
)
def web_close_tab() -> Dict[str, Any]:
    """Press Ctrl+W to close active browser tab."""
    try:
        _focus_browser()
        pyautogui.hotkey("ctrl", "w")
        return {"success": True, "action": "tab_closed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_reopen_tab",
    description="Reopen the last closed tab in the browser (Ctrl+Shift+T)."
)
def web_reopen_tab() -> Dict[str, Any]:
    """Press Ctrl+Shift+T to restore closed tab."""
    try:
        _focus_browser()
        pyautogui.hotkey("ctrl", "shift", "t")
        return {"success": True, "action": "tab_restored"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_reload_page",
    description="Reload / refresh the current webpage (optional hard reload ignoring cache)."
)
def web_reload_page(hard: bool = False) -> Dict[str, Any]:
    """Press Ctrl+R or Ctrl+Shift+R to refresh the page."""
    try:
        _focus_browser()
        if hard:
            pyautogui.hotkey("ctrl", "shift", "r")
        else:
            pyautogui.hotkey("ctrl", "r")
        return {"success": True, "hard_reload": hard, "action": "page_reloaded"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_navigate_back",
    description="Go back to the previous webpage in browser history (Alt+Left Arrow)."
)
def web_navigate_back() -> Dict[str, Any]:
    """Press Alt+Left to go back."""
    try:
        _focus_browser()
        pyautogui.hotkey("alt", "left")
        return {"success": True, "action": "navigated_back"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_navigate_forward",
    description="Go forward to the next webpage in browser history (Alt+Right Arrow)."
)
def web_navigate_forward() -> Dict[str, Any]:
    """Press Alt+Right to go forward."""
    try:
        _focus_browser()
        pyautogui.hotkey("alt", "right")
        return {"success": True, "action": "navigated_forward"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_zoom_in",
    description="Zoom in on the current webpage (Ctrl + Plus)."
)
def web_zoom_in() -> Dict[str, Any]:
    """Zoom in."""
    try:
        _focus_browser()
        pyautogui.hotkey("ctrl", "+")
        return {"success": True, "action": "zoomed_in"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_zoom_out",
    description="Zoom out on the current webpage (Ctrl + Minus)."
)
def web_zoom_out() -> Dict[str, Any]:
    """Zoom out."""
    try:
        _focus_browser()
        pyautogui.hotkey("ctrl", "-")
        return {"success": True, "action": "zoomed_out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_zoom_reset",
    description="Reset zoom on the current webpage to 100% (Ctrl + 0)."
)
def web_zoom_reset() -> Dict[str, Any]:
    """Reset zoom."""
    try:
        _focus_browser()
        pyautogui.hotkey("ctrl", "0")
        return {"success": True, "action": "zoom_reset"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_scroll_page",
    description="Scroll the active webpage ('down', 'up', 'top', or 'bottom')."
)
def web_scroll_page(direction: str = "down", amount: str = "page") -> Dict[str, Any]:
    """Scroll webpage smoothly."""
    try:
        _focus_browser()
        dir_l = direction.lower()
        if dir_l in ["top", "shuru", "upore"]:
            pyautogui.press("home")
        elif dir_l in ["bottom", "sesh", "niche"]:
            pyautogui.press("end")
        elif dir_l in ["up", "opore"]:
            pyautogui.press("pageup")
        else:  # down default
            pyautogui.press("pagedown")
        return {"success": True, "direction": direction, "action": f"scrolled_{direction}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_find_in_page",
    description="Search for specific text inside the current webpage (Ctrl+F)."
)
def web_find_in_page(query: str) -> Dict[str, Any]:
    """Open Ctrl+F find bar and enter query."""
    try:
        _focus_browser()
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.08)
        pyautogui.write(query)
        pyautogui.press("enter")
        return {"success": True, "query": query, "action": "search_in_page_triggered"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_copy_current_url",
    description="Copy the URL of the currently active browser tab to clipboard."
)
def web_copy_current_url() -> Dict[str, Any]:
    """Focus address bar (Ctrl+L), copy (Ctrl+C), and read via Win32 ctypes."""
    try:
        _focus_browser()
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.06)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.06)
        pyautogui.press("escape")
        url = _get_clipboard_text().strip()
        return {"success": True, "url": url, "action": "url_copied"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="web_switch_tab",
    description="Switch browser tabs: 'next' (Ctrl+Tab), 'prev' (Ctrl+Shift+Tab), or specific tab index 1..9."
)
def web_switch_tab(direction: str = "next", index: Optional[int] = None) -> Dict[str, Any]:
    """Switch between browser tabs."""
    try:
        _focus_browser()
        if index is not None and 1 <= index <= 9:
            pyautogui.hotkey("ctrl", str(index))
            return {"success": True, "tab_index": index}
        if direction.lower() in ["prev", "previous", "ager"]:
            pyautogui.hotkey("ctrl", "shift", "tab")
        else:
            pyautogui.hotkey("ctrl", "tab")
        return {"success": True, "direction": direction}
    except Exception as e:
        return {"success": False, "error": str(e)}
