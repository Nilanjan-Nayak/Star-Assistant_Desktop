"""
Tool Registry for Star Assistant.
Provides clean decorator-based registration and execution of system capabilities.
"""

import inspect
from typing import Callable, Dict, Any, List

_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_tool(name: str = None, description: str = ""):
    """Decorator to register any python function as an assistant tool."""
    def decorator(fn: Callable):
        tool_name = name or fn.__name__
        sig = inspect.signature(fn)
        doc = description or (fn.__doc__ or "").strip()

        params_schema = {
            "type": "object",
            "properties": {},
            "required": []
        }

        for param_name, param in sig.parameters.items():
            param_type = "string"
            if param.annotation == int or param.annotation == float:
                param_type = "number"
            elif param.annotation == bool:
                param_type = "boolean"

            params_schema["properties"][param_name] = {
                "type": param_type,
                "description": f"Parameter {param_name}"
            }
            if param.default == inspect.Parameter.empty:
                params_schema["required"].append(param_name)

        _REGISTRY[tool_name] = {
            "name": tool_name,
            "description": doc,
            "func": fn,
            "parameters": params_schema
        }
        return fn
    return decorator


def get_all_tools() -> Dict[str, Dict[str, Any]]:
    return _REGISTRY


def get_tool_schemas() -> List[Dict[str, Any]]:
    """Return OpenAI/Ollama function calling tool definitions."""
    schemas = []
    for name, item in _REGISTRY.items():
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": item["description"],
                "parameters": item["parameters"]
            }
        })
    return schemas


def execute_tool(name: str, **kwargs) -> Dict[str, Any]:
    """Execute a registered tool safely with keyword arguments."""
    if name not in _REGISTRY:
        return {"success": False, "error": f"Tool '{name}' not found."}
    
    try:
        fn = _REGISTRY[name]["func"]
        result = fn(**kwargs)
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}
