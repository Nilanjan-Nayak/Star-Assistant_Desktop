"""Computer Control Agent — ultra type-safe autonomous desktop automation.

Public surface is intentionally tiny. Import subsystems from their packages
when you need lower-level types (``ActionSpec``, ``GovernorConfig``, …).
"""

from __future__ import annotations

from agent.facade import ComputerControlAgent

__version__: str = "6.0.0"
__all__: list[str] = ["ComputerControlAgent", "__version__"]
