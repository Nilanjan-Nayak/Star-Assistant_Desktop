"""
Memory and Knowledge Retention Tools.
Allows the assistant to remember facts, user preferences, and recall them later.
"""

from typing import Dict, Any, Optional

from ..registry import register_tool
from ...agent_bridge import get_agent_bridge


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
