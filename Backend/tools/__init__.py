"""
Tools package initialization.
Automatically imports all tool modules to register them.
"""

from .registry import register_tool, execute_tool, get_all_tools, get_tool_schemas

# Import tool modules to trigger decorators
from . import system_volume
from . import system_brightness
from . import system_info
from . import computer_control
