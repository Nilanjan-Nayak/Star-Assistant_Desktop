"""STAR 2.0 computer package (Phase 6) — screen, mouse and keyboard.

Thin by design: the *implementation* of physical control already exists in this
repository (``agent/motor``, ``agent/world``, ``agent/safety``, ``agent/perception``)
and is reused, not rebuilt. What lives here is the STAR 2.0 surface around it:

* :mod:`.guardrails` — bounds, forbidden regions, text/hotkey blocklists, and the
  reused six-invariant :class:`~agent.safety.governor.SafetyGovernor`.
* :mod:`.motor` — picks the backend: ``NullBackend`` (records, moves nothing) in
  dry-run/headless, ``PyAutoGUIBackend`` only when dry-run is off and it imports.
* :mod:`.tools` — typed Phase 4 tool specs so every action inherits validation,
  permissions, confirmation, audit and dry-run.
* :mod:`.agent` — :class:`ComputerAgent`, the goal-oriented worker.

Exports are lazy (PEP 562) so importing the package stays cheap and never pulls
in pyautogui.
"""

from __future__ import annotations

__all__ = [
    "ActionVerdict",
    "ComputerAgent",
    "ComputerGuardrails",
    "ComputerToolkit",
    "MotorStack",
    "build_computer",
    "build_motor",
    "describe_action",
    "register_computer_tools",
]

_LAZY = {
    "ActionVerdict": ("Backend.star.computer.guardrails", "ActionVerdict"),
    "ComputerGuardrails": ("Backend.star.computer.guardrails", "ComputerGuardrails"),
    "MotorStack": ("Backend.star.computer.motor", "MotorStack"),
    "build_motor": ("Backend.star.computer.motor", "build_motor"),
    "describe_action": ("Backend.star.computer.motor", "describe_action"),
    "ComputerToolkit": ("Backend.star.computer.tools", "ComputerToolkit"),
    "register_computer_tools": ("Backend.star.computer.tools", "register_computer_tools"),
    "ComputerAgent": ("Backend.star.computer.agent", "ComputerAgent"),
    "build_computer": ("Backend.star.computer.agent", "build_computer"),
}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        module_name, attribute = _LAZY[name]
        return getattr(importlib.import_module(module_name), attribute)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
