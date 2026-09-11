"""
NLU package — deterministic, multilingual (Bengali / Banglish / English)
command understanding for Star Assistant.
"""

from .command_router import route_command  # noqa: F401

__all__ = ["route_command"]
