"""STAR 2.0 workspace package (Phase 7) — the invisible background desktop.

Blueprint §8: *"The target is not simply minimizing a window. Star should have a
controlled execution workspace separate from the visible Star UI"* — isolated
browser/session, screen capture, perception, motor scratch space, task
checkpoints and a verifier, all away from the user's visible project tree.

* :mod:`.session` — :class:`WorkspaceSession` / :class:`Checkpoint` / the states.
* :mod:`.isolation` — the strategy seam (``null`` / ``process`` /
  ``windows_virtual_desktop``) with honest capability flags and fallbacks.
* :mod:`.manager` — :class:`WorkspaceManager`: the jail, quotas, TTL, checkpoints,
  destroy-with-verification, events and audit.
* :mod:`.tools` — nine typed Phase 4 tools so every workspace call inherits
  validation, permissions, confirmation, audit and dry-run.

Exports are lazy (PEP 562) to keep ``import Backend.star.workspace`` cheap.
"""

from __future__ import annotations

__all__ = [
    "Checkpoint",
    "IsolationBackend",
    "NullIsolation",
    "ProcessIsolation",
    "VirtualDesktopIsolation",
    "WorkspaceError",
    "WorkspaceKind",
    "WorkspaceManager",
    "WorkspaceSession",
    "WorkspaceState",
    "WorkspaceToolkit",
    "build_isolation",
    "build_workspace",
    "register_workspace_tools",
]

_LAZY = {
    "Checkpoint": ("Backend.star.workspace.session", "Checkpoint"),
    "WorkspaceKind": ("Backend.star.workspace.session", "WorkspaceKind"),
    "WorkspaceSession": ("Backend.star.workspace.session", "WorkspaceSession"),
    "WorkspaceState": ("Backend.star.workspace.session", "WorkspaceState"),
    "IsolationBackend": ("Backend.star.workspace.isolation", "IsolationBackend"),
    "NullIsolation": ("Backend.star.workspace.isolation", "NullIsolation"),
    "ProcessIsolation": ("Backend.star.workspace.isolation", "ProcessIsolation"),
    "VirtualDesktopIsolation": ("Backend.star.workspace.isolation", "VirtualDesktopIsolation"),
    "build_isolation": ("Backend.star.workspace.isolation", "build_isolation"),
    "WorkspaceError": ("Backend.star.workspace.manager", "WorkspaceError"),
    "WorkspaceManager": ("Backend.star.workspace.manager", "WorkspaceManager"),
    "build_workspace": ("Backend.star.workspace.manager", "build_workspace"),
    "WorkspaceToolkit": ("Backend.star.workspace.tools", "WorkspaceToolkit"),
    "register_workspace_tools": ("Backend.star.workspace.tools", "register_workspace_tools"),
}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        module_name, attribute = _LAZY[name]
        return getattr(importlib.import_module(module_name), attribute)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
