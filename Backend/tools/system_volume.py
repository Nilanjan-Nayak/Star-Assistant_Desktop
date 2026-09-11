"""
Windows Master Volume Control Tool.
Uses pycaw / Windows Core Audio Endpoint API.
"""

from .registry import register_tool

_volume_endpoint = None


def _get_endpoint():
    global _volume_endpoint
    if _volume_endpoint is not None:
        return _volume_endpoint
    try:
        from pycaw.pycaw import AudioUtilities
        spk = AudioUtilities.GetSpeakers()
        if hasattr(spk, "EndpointVolume"):
            _volume_endpoint = spk.EndpointVolume
            return _volume_endpoint
        # Legacy fallback
        from comtypes import CLSCTX_ALL
        from ctypes import cast, POINTER
        from pycaw.pycaw import IAudioEndpointVolume
        interface = spk.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        _volume_endpoint = cast(interface, POINTER(IAudioEndpointVolume))
        return _volume_endpoint
    except Exception as e:
        print(f"[Volume] Could not initialize pycaw: {e}")
        return None


@register_tool(name="get_volume", description="Get the current Windows master volume level as a percentage (0-100).")
def get_volume() -> int:
    """Return current volume in percentage (0-100)."""
    ep = _get_endpoint()
    if ep:
        current_scalar = ep.GetMasterVolumeLevelScalar()
        return int(round(current_scalar * 100))
    return 50


@register_tool(name="set_volume", description="Set Windows master volume to an exact percentage (0 to 100).")
def set_volume(level: int) -> dict:
    """Set master volume scalar 0..100."""
    target = max(0, min(100, int(level)))
    ep = _get_endpoint()
    if ep:
        ep.SetMasterVolumeLevelScalar(target / 100.0, None)
        # Unmute if muted when explicitly setting volume
        if target > 0 and ep.GetMute():
            ep.SetMute(0, None)
        return {"current_volume": target, "message": f"Volume set to {target}%"}
    return {"current_volume": target, "error": "Hardware endpoint unavailable"}


@register_tool(name="adjust_volume", description="Increase or decrease Windows master volume by a relative delta (e.g. +20, -10).")
def adjust_volume(delta: int) -> dict:
    """Adjust master volume relatively by delta percent."""
    current = get_volume()
    target = max(0, min(100, current + int(delta)))
    return set_volume(target)


@register_tool(name="set_mute", description="Mute or unmute Windows master audio output.")
def set_mute(mute: bool) -> dict:
    """Set mute state."""
    ep = _get_endpoint()
    if ep:
        ep.SetMute(1 if mute else 0, None)
        state_str = "Muted" if mute else "Unmuted"
        return {"muted": mute, "message": f"Audio {state_str}"}
    return {"muted": mute, "error": "Hardware endpoint unavailable"}
