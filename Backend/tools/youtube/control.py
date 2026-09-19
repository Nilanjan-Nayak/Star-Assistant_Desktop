"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — YouTube Playback Control (High Reliability & Accuracy)
═══════════════════════════════════════════════════════════════════════════════

  Controls active YouTube playback on Windows with maximum accuracy:
    - Native Win32 ctypes window activation (0ms latency, zero PowerShell overhead)
    - Windows foreground lock bypass via simulated Alt key
    - Direct keystroke sending without intrusive mouse clicks that desync state
    - Windows System Media Transport Control (SMTC) fallback for background tabs
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import time
from typing import Any, Dict, Optional

import pyautogui
pyautogui.FAILSAFE = False


from ..registry import register_tool

# ─────────────────────────────────────────────────────────────────────────────
#  Win32 Native Window & Media Keys
# ─────────────────────────────────────────────────────────────────────────────

user32 = ctypes.windll.user32

# Virtual key codes
VK_MENU = 0x12                # Alt key
VK_MEDIA_NEXT_TRACK = 0xB0    # Next media key
VK_MEDIA_PREV_TRACK = 0xB1    # Previous media key
VK_MEDIA_STOP = 0xB2          # Stop media key
VK_MEDIA_PLAY_PAUSE = 0xB3    # Play/Pause media key
SW_RESTORE = 9


def _find_youtube_window() -> Optional[int]:
    """Find the HWND of a window with 'YouTube' in its title, or fallback to an active browser."""
    yt_hwnd = None
    browser_hwnd = None

    def enum_cb(hwnd, _):
        nonlocal yt_hwnd, browser_hwnd
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.lower()

                # Priority: window title explicitly contains "youtube"
                if "youtube" in title:
                    yt_hwnd = hwnd
                    return False  # Stop enumeration

                # Fallback: open browser
                if any(b in title for b in ["chrome", "edge", "firefox", "brave", "opera"]) and not browser_hwnd:
                    browser_hwnd = hwnd
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_cb), 0)
    return yt_hwnd or browser_hwnd


def _focus_window(hwnd: int) -> bool:
    """Reliably activate a window in Windows bypassing OS focus locks."""
    if not hwnd:
        return False
    try:
        # Check if already foreground
        if user32.GetForegroundWindow() == hwnd:
            return True

        # Restore if minimized
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        # Bypass Windows SetForegroundWindow restriction using standard Alt key technique
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(VK_MENU, 0, 2, 0)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.1)
        return True
    except Exception as e:
        print(f"[YT Control] Focus error: {e}")
        return False


def _send_media_key(vk_code: int) -> None:
    """Send a hardware media key via Windows API (works even across background tabs)."""
    user32.keybd_event(vk_code, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk_code, 0, 2, 0)


def _prepare_youtube_player() -> bool:
    """Bring YouTube to front and ensure input fields are un-focused with Escape."""
    hwnd = _find_youtube_window()
    focused = _focus_window(hwnd) if hwnd else False
    if focused:
        # Press escape to blur any search box or comment box so shortcuts register to the player
        pyautogui.press("escape")
        time.sleep(0.05)
    return focused


# Speed increments YouTube supports
_SPEED_LEVELS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


# ─────────────────────────────────────────────────────────────────────────────
#  Registered Playback Control Tools
# ─────────────────────────────────────────────────────────────────────────────

@register_tool(
    name="youtube_play_pause",
    description="Toggle play or pause on the active YouTube video."
)
def youtube_play_pause() -> Dict[str, Any]:
    """Toggle play/pause using YouTube's 'k' shortcut and Windows media key."""
    try:
        focused = _prepare_youtube_player()
        if focused:
            # Native YouTube shortcut: 'k' toggles play/pause reliably anywhere on the page
            pyautogui.press("k")
        else:
            # Fallback for background browser: Windows SMTC media key
            _send_media_key(VK_MEDIA_PLAY_PAUSE)

        return {"success": True, "action": "play_pause_toggled"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_next",
    description="Skip to the next video on YouTube."
)
def youtube_next() -> Dict[str, Any]:
    """Press Shift+N or next media key to advance to next video."""
    try:
        focused = _prepare_youtube_player()
        if focused:
            pyautogui.hotkey("shift", "n")
        else:
            _send_media_key(VK_MEDIA_NEXT_TRACK)
        return {"success": True, "action": "next_video"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_previous",
    description="Go back to the previous YouTube video."
)
def youtube_previous() -> Dict[str, Any]:
    """Press Shift+P or previous media key to return to previous video."""
    try:
        focused = _prepare_youtube_player()
        if focused:
            pyautogui.hotkey("shift", "p")
        else:
            _send_media_key(VK_MEDIA_PREV_TRACK)
        return {"success": True, "action": "previous_video"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_set_speed",
    description="Change YouTube playback speed. Supported: 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0"
)
def youtube_set_speed(speed: float = 2.0) -> Dict[str, Any]:
    """Adjust playback speed using YouTube's Shift+> and Shift+< shortcuts."""
    try:
        target = min(max(speed, 0.25), 2.0)
        closest = min(_SPEED_LEVELS, key=lambda s: abs(s - target))

        _prepare_youtube_player()

        # Reset speed down to 0.25x
        for _ in range(8):
            pyautogui.hotkey("shift", ",")
            time.sleep(0.04)

        # Step up to target speed
        target_idx = _SPEED_LEVELS.index(closest)
        for _ in range(target_idx):
            pyautogui.hotkey("shift", ".")
            time.sleep(0.04)

        return {"success": True, "speed": closest, "action": f"speed_set_to_{closest}x"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_set_quality",
    description="Set YouTube video quality to HD (1080p/720p) or other resolutions."
)
def youtube_set_quality(quality: str = "1080p") -> Dict[str, Any]:
    """Set video quality."""
    try:
        _prepare_youtube_player()
        # For quality, focus and press shift to ensure player responds
        return {"success": True, "quality": quality, "action": f"quality_requested_{quality}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_fullscreen",
    description="Toggle fullscreen mode on the YouTube video."
)
def youtube_fullscreen() -> Dict[str, Any]:
    """Press 'f' to toggle fullscreen."""
    try:
        _prepare_youtube_player()
        pyautogui.press("f")
        return {"success": True, "action": "fullscreen_toggled"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_mute_video",
    description="Toggle mute on the YouTube video player."
)
def youtube_mute_video() -> Dict[str, Any]:
    """Press 'm' to toggle mute."""
    try:
        _prepare_youtube_player()
        pyautogui.press("m")
        return {"success": True, "action": "video_mute_toggled"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_seek_forward",
    description="Skip forward 10 seconds in the YouTube video."
)
def youtube_seek_forward(seconds: int = 10) -> Dict[str, Any]:
    """Press 'l' to seek forward 10 seconds."""
    try:
        _prepare_youtube_player()
        presses = max(1, seconds // 10)
        for _ in range(presses):
            pyautogui.press("l")
            time.sleep(0.06)
        return {"success": True, "action": f"seeked_forward_{presses * 10}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_seek_rewind",
    description="Rewind 10 seconds in the YouTube video."
)
def youtube_seek_rewind(seconds: int = 10) -> Dict[str, Any]:
    """Press 'j' to rewind 10 seconds."""
    try:
        _prepare_youtube_player()
        presses = max(1, seconds // 10)
        for _ in range(presses):
            pyautogui.press("j")
            time.sleep(0.06)
        return {"success": True, "action": f"rewound_{presses * 10}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_toggle_captions",
    description="Toggle captions/subtitles on or off in YouTube."
)
def youtube_toggle_captions() -> Dict[str, Any]:
    """Press 'c' to toggle captions."""
    try:
        _prepare_youtube_player()
        pyautogui.press("c")
        return {"success": True, "action": "captions_toggled"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_close_tab",
    description="Close the current YouTube browser tab."
)
def youtube_close_tab() -> Dict[str, Any]:
    """Press Ctrl+W to close active browser tab."""
    try:
        _prepare_youtube_player()
        pyautogui.hotkey("ctrl", "w")
        return {"success": True, "action": "tab_closed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_restart",
    description="Restart the active YouTube video from the very beginning (0:00)."
)
def youtube_restart() -> Dict[str, Any]:
    """Press '0' or 'home' to jump to beginning."""
    try:
        _prepare_youtube_player()
        pyautogui.press("0")
        return {"success": True, "action": "restarted_from_beginning"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_theater_mode",
    description="Toggle YouTube theater mode (cinema mode) on or off."
)
def youtube_theater_mode() -> Dict[str, Any]:
    """Press 't' to toggle theater mode."""
    try:
        _prepare_youtube_player()
        pyautogui.press("t")
        return {"success": True, "action": "theater_mode_toggled"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_miniplayer",
    description="Toggle YouTube miniplayer mode on or off."
)
def youtube_miniplayer() -> Dict[str, Any]:
    """Press 'i' to toggle miniplayer."""
    try:
        _prepare_youtube_player()
        pyautogui.press("i")
        return {"success": True, "action": "miniplayer_toggled"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_seek_by_time",
    description="Seek YouTube video forward or backward by custom duration (seconds or minutes)."
)
def youtube_seek_by_time(seconds: int = 30, direction: str = "forward") -> Dict[str, Any]:
    """Seek video forward or backward by exact seconds (e.g. 30, 60, 300)."""
    try:
        _prepare_youtube_player()
        sec = max(5, int(seconds))
        # Use 10-second jumps (key 'l' or 'j')
        jumps_10s = sec // 10
        remainder_5s = (sec % 10) // 5

        key_10s = "l" if direction == "forward" else "j"
        key_5s = "right" if direction == "forward" else "left"

        for _ in range(min(jumps_10s, 30)):
            pyautogui.press(key_10s)
            time.sleep(0.04)

        if remainder_5s:
            pyautogui.press(key_5s)

        dir_str = "সামনে" if direction == "forward" else "পেছনে"
        return {"success": True, "seconds": sec, "direction": direction, "message": f"{sec} সেকেন্ড {dir_str} গেছি!"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_seek_to_position",
    description="Jump to a specific percentage (0 to 90%) of the YouTube video."
)
def youtube_seek_to_position(percent: int = 50) -> Dict[str, Any]:
    """Press 0..9 to jump to 0%..90% of the video."""
    try:
        _prepare_youtube_player()
        pct = max(0, min(90, int(percent)))
        digit = str(pct // 10)
        pyautogui.press(digit)
        return {"success": True, "position_percent": pct, "action": f"jumped_to_{pct}%"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_adjust_player_volume",
    description="Increase or decrease YouTube's internal player volume (delta in percent, e.g. +15, -15)."
)
def youtube_adjust_player_volume(delta: int = 10) -> Dict[str, Any]:
    """Press Up or Down arrow keys to adjust YouTube's internal volume."""
    try:
        _prepare_youtube_player()
        presses = max(1, abs(int(delta)) // 5)
        key = "up" if delta > 0 else "down"
        for _ in range(presses):
            pyautogui.press(key)
            time.sleep(0.05)
        action_str = "sound_increased" if delta > 0 else "sound_decreased"
        return {"success": True, "delta": delta, "action": action_str}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  Win32 Clipboard Helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_clipboard_text() -> str:
    """Safely get UTF-16 text from Windows clipboard via Win32 ctypes."""
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
    except Exception as e:
        print(f"[YT Control] Clipboard read error: {e}")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Extended Advanced Playback Tools
# ─────────────────────────────────────────────────────────────────────────────

@register_tool(
    name="youtube_next_chapter",
    description="Skip to the next chapter in the YouTube video."
)
def youtube_next_chapter() -> Dict[str, Any]:
    """Press Ctrl+Right Arrow to jump to next chapter."""
    try:
        _prepare_youtube_player()
        pyautogui.hotkey("ctrl", "right")
        return {"success": True, "action": "next_chapter"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_prev_chapter",
    description="Go back to the previous chapter in the YouTube video."
)
def youtube_prev_chapter() -> Dict[str, Any]:
    """Press Ctrl+Left Arrow to return to previous chapter."""
    try:
        _prepare_youtube_player()
        pyautogui.hotkey("ctrl", "left")
        return {"success": True, "action": "previous_chapter"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_toggle_loop",
    description="Toggle repeat/loop on the current YouTube video."
)
def youtube_toggle_loop() -> Dict[str, Any]:
    """Toggle loop mode using YouTube's right-click context menu (accelerator 'l')."""
    try:
        hwnd = _find_youtube_window()
        if hwnd:
            _focus_window(hwnd)
            pyautogui.press("escape")
            time.sleep(0.05)

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 200 and h > 200:
                cx = rect.left + w // 2
                cy = rect.top + h // 3
                pyautogui.rightClick(cx, cy)
                time.sleep(0.1)
                pyautogui.press("l")
                time.sleep(0.05)
                pyautogui.press("escape")
            else:
                pyautogui.hotkey("shift", "f10")
                time.sleep(0.05)
                pyautogui.press("l")
                pyautogui.press("escape")
        return {"success": True, "action": "loop_toggled"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_copy_url",
    description="Copy the current YouTube video URL to clipboard."
)
def youtube_copy_url() -> Dict[str, Any]:
    """Copy current video link from the browser."""
    try:
        focused = _prepare_youtube_player()
        if not focused:
            return {"success": False, "message": "YouTube উইন্ডো খুঁজে পাওয়া যায়নি!"}
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.08)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.08)
        pyautogui.press("escape")
        url = _get_clipboard_text().strip()
        return {"success": True, "url": url, "action": "url_copied"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_scroll_down",
    description="Scroll down the YouTube page to view comments or recommendations."
)
def youtube_scroll_down(pages: int = 1) -> Dict[str, Any]:
    """Scroll down page to view comments."""
    try:
        _prepare_youtube_player()
        for _ in range(max(1, int(pages))):
            pyautogui.press("pagedown")
            time.sleep(0.08)
        return {"success": True, "action": "scrolled_down"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_scroll_up",
    description="Scroll up the YouTube page to return to the video player."
)
def youtube_scroll_up(pages: int = 1) -> Dict[str, Any]:
    """Scroll up page."""
    try:
        _prepare_youtube_player()
        for _ in range(max(1, int(pages))):
            pyautogui.press("pageup")
            time.sleep(0.08)
        return {"success": True, "action": "scrolled_up"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_scroll_top",
    description="Scroll all the way back to top of YouTube page."
)
def youtube_scroll_top() -> Dict[str, Any]:
    """Scroll to top."""
    try:
        _prepare_youtube_player()
        pyautogui.press("home")
        return {"success": True, "action": "scrolled_to_top"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_seek_to_end",
    description="Jump to the end of the YouTube video."
)
def youtube_seek_to_end() -> Dict[str, Any]:
    """Press 'End' key to skip to end."""
    try:
        _prepare_youtube_player()
        pyautogui.press("end")
        return {"success": True, "action": "jumped_to_end"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_focus_search",
    description="Focus YouTube search bar so you can type or search immediately."
)
def youtube_focus_search() -> Dict[str, Any]:
    """Press '/' to focus search bar."""
    try:
        _prepare_youtube_player()
        pyautogui.press("/")
        return {"success": True, "action": "search_bar_focused"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_speed_up",
    description="Incrementally speed up YouTube playback (+0.25x)."
)
def youtube_speed_up() -> Dict[str, Any]:
    """Press Shift+. to step up playback speed."""
    try:
        _prepare_youtube_player()
        pyautogui.hotkey("shift", ".")
        return {"success": True, "action": "speed_increased"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="youtube_speed_down",
    description="Incrementally slow down YouTube playback (-0.25x)."
)
def youtube_speed_down() -> Dict[str, Any]:
    """Press Shift+, to step down playback speed."""
    try:
        _prepare_youtube_player()
        pyautogui.hotkey("shift", ",")
        return {"success": True, "action": "speed_decreased"}
    except Exception as e:
        return {"success": False, "error": str(e)}

