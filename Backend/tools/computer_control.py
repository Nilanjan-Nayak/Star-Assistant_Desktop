"""
Integration bridge connecting ComputerControlAgent v6.0 to Star Assistant Tool Registry.
Exposes high-level skills, screen OCR perception, memory retention, and autonomous goals.
"""

from typing import Dict, Any, Optional, List
from .registry import register_tool
from ..agent_bridge import get_agent_bridge


@register_tool(
    name="execute_autonomous_goal",
    description="Execute an autonomous multi-step desktop automation goal using ComputerControlAgent v6 ReAct loop."
)
def execute_autonomous_goal(goal: str) -> Dict[str, Any]:
    """
    Run an autonomous goal using ReAct loop, safety governor, and episodic memory.
    Example goals:
      - 'Open YouTube and play lo-fi beats'
      - 'Take a screenshot and save to disk'
      - 'Set volume to 40'
      - 'Read the screen and summarize'
    """
    try:
        bridge = get_agent_bridge()
        res = bridge.run(goal, timeout=120.0)
        return {
            "success": res.get("ok", False),
            "state": res.get("state"),
            "steps_count": res.get("step_count", 0),
            "steps": res.get("steps", []),
            "error": res.get("error")
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="execute_computer_skill",
    description="Directly execute a registered computer control skill: 'volume', 'brightness', 'launch', 'youtube', 'screenshot', or 'see'."
)
def execute_computer_skill(skill: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute a specific typed skill directly via ComputerControlAgent v6."""
    try:
        bridge = get_agent_bridge()
        kwargs = params or {}
        res = bridge.quick(skill, **kwargs)
        return {
            "success": res.get("ok", False),
            "message": res.get("message", ""),
            "data": res.get("data", {})
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


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
    name="remember_fact",
    description="Remember a user preference, personal fact, or instruction in long-term SQLite vector memory."
)
def remember_fact(text: str, kind: str = "fact", key: Optional[str] = None, value: Optional[str] = None) -> Dict[str, Any]:
    """Store text into agent long-term memory."""
    try:
        bridge = get_agent_bridge()
        ok = bridge.remember(text, kind=kind, key=key, value=value)
        return {"success": ok, "text": text}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="recall_memory",
    description="Retrieve relevant memories and preferences about the user given a search query."
)
def recall_memory(query: str) -> Dict[str, Any]:
    """Search vector and key-value memory for context."""
    try:
        bridge = get_agent_bridge()
        ctx = bridge.get_memory_context(query)
        return {"success": bool(ctx), "context": ctx}
    except Exception as e:
        return {"success": False, "error": str(e)}


@register_tool(
    name="get_agent_metrics",
    description="Get real-time health and diagnostics from ComputerControlAgent v6."
)
def get_agent_metrics() -> Dict[str, Any]:
    """Retrieve telemetry snapshot of ComputerControlAgent."""
    try:
        bridge = get_agent_bridge()
        return {"success": True, "health": bridge.health()}
    except Exception as e:
        return {"success": False, "error": str(e)}
