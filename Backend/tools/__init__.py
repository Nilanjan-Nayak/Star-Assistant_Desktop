"""
═══════════════════════════════════════════════════════════════════════════════
  STAR ASSISTANT — Tools Package & Auto-Discovery
═══════════════════════════════════════════════════════════════════════════════

  This package provides all tools for Star Assistant.
  Tools are organized into modular categories:
    - system/    : Hardware, volume, brightness, OS telemetry
    - apps/      : Desktop app & process launcher/killer
    - media/     : YouTube controls, history, analytics, web playback
    - agent/     : Autonomous goals, computer skills, OCR vision, memory
    - utilities/ : Math calculation and general helpers

  ⚡ AUTO-DISCOVERY:
  Any .py file added to any subfolder will automatically be imported
  and its @register_tool functions will be registered.
  You DO NOT need to modify this file when adding new tools!
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

# Core registry exports
from .registry import register_tool, execute_tool, get_all_tools, get_tool_schemas

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Auto-Discover & Load All Tool Submodules
# ─────────────────────────────────────────────────────────────────────────────

_TOOLS_DIR = Path(__file__).resolve().parent


def _load_all_tools() -> None:
    """Recursively discover and import all tool modules in subdirectories."""
    # Priority ordered categories, but discovers ANY subfolder
    subdirs = [d for d in _TOOLS_DIR.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))]

    for subdir in subdirs:
        for py_file in subdir.rglob("*.py"):
            if py_file.name.startswith("_"):
                continue

            rel_parts = py_file.relative_to(_TOOLS_DIR).with_suffix("").parts
            module_name = f"Backend.tools.{'.'.join(rel_parts)}"

            try:
                importlib.import_module(module_name)
            except Exception as e:
                logger.warning(f"[Tools Auto-Discovery] Could not load {module_name}: {e}")


# Run discovery on import
_load_all_tools()

# Convenient re-export of frequently used helper
try:
    from .youtube.history import record_youtube_play
except ImportError:
    try:
        from .media.youtube_history import record_youtube_play
    except ImportError:
        pass
