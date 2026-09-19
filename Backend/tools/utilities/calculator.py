"""
Mathematical Expression Evaluator Tool.
Supports arithmetic calculation with Bengali and English digits.
"""

import ast
import operator
import re

from ..registry import register_tool


@register_tool(name="calculate_math", description="Evaluate a mathematical expression safely.")
def calculate_math(expression: str) -> dict:
    """Evaluate an arithmetic expression (e.g. '15 * 8', '500 + 350')."""
    bengali_digits = {
        "০": "0", "১": "1", "২": "2", "৩": "3", "৪": "4",
        "৫": "5", "৬": "6", "৭": "7", "৮": "8", "৯": "9"
    }
    expr_str = expression
    for bn, en in bengali_digits.items():
        expr_str = expr_str.replace(bn, en)

    ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
    }

    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            left = _eval(node.left)
            right = _eval(node.right)
            return ops[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        raise ValueError("Unsupported operation")

    clean_expr = expr_str.replace("x", "*").replace("×", "*").replace("÷", "/")
    clean_expr = re.sub(r"[^0-9+\-*/(). ]", "", clean_expr)
    try:
        parsed = ast.parse(clean_expr, mode="eval")
        result = _eval(parsed.body)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return {"expression": expression, "result": result}
    except Exception as e:
        return {"expression": expression, "error": str(e)}
