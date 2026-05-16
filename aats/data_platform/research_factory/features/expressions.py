"""Safe factor expression parser for Research Factory feature definitions."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from aats.data_platform.research_factory.numeric import require_finite_number

ALLOWED_FACTOR_FIELDS = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "funding_rate",
    }
)
ALLOWED_FACTOR_FUNCTIONS = frozenset(
    {
        "Ref",
        "Return",
        "Mean",
        "Std",
        "ZScore",
        "Max",
        "Min",
        "Rank",
        "Delta",
    }
)
ALLOWED_BINARY_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
ALLOWED_UNARY_OPS = (ast.UAdd, ast.USub)


@dataclass(frozen=True, slots=True)
class FactorExpression:
    """Parsed, serializable representation of a safe factor expression."""

    expression: str
    normalized_ast: str
    fields: tuple[str, ...]
    functions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "normalized_ast": self.normalized_ast,
            "fields": list(self.fields),
            "functions": list(self.functions),
        }


def parse_factor_expression(expr: str) -> FactorExpression:
    """Parse a factor expression without evaluating Python code."""
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("factor expression must be a non-empty string")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError("factor expression syntax is invalid") from exc

    visitor = _SafeFactorExpressionVisitor()
    visitor.visit(tree)
    return FactorExpression(
        expression=expr.strip(),
        normalized_ast=ast.dump(tree.body, include_attributes=False),
        fields=tuple(visitor.fields),
        functions=tuple(visitor.functions),
    )


class _SafeFactorExpressionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.fields: list[str] = []
        self.functions: list[str] = []

    def visit_Expression(self, node: ast.Expression) -> None:
        self.visit(node.body)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Lambda):
            raise ValueError("factor expressions must not use lambda")
        if not isinstance(node.func, ast.Name):
            raise ValueError("factor functions must be direct whitelist names")
        function_name = node.func.id
        _reject_dunder(function_name)
        if function_name not in ALLOWED_FACTOR_FUNCTIONS:
            raise ValueError(f"unknown factor function: {function_name}")
        if node.keywords:
            raise ValueError("factor functions must not use keyword arguments")
        if function_name == "Ref":
            self._validate_ref_call(node)

        _append_unique(self.functions, function_name)
        for arg in node.args:
            self.visit(arg)

    def visit_Name(self, node: ast.Name) -> None:
        _reject_dunder(node.id)
        if node.id not in ALLOWED_FACTOR_FIELDS:
            raise ValueError(f"unknown factor field: {node.id}")
        _append_unique(self.fields, node.id)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ValueError("factor constants must be numeric")
        require_finite_number(node.value, "factor constant")

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not isinstance(node.op, ALLOWED_BINARY_OPS):
            raise ValueError("factor expression uses a disallowed binary operator")
        self.visit(node.left)
        self.visit(node.right)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if not isinstance(node.op, ALLOWED_UNARY_OPS):
            raise ValueError("factor expression uses a disallowed unary operator")
        self.visit(node.operand)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        raise ValueError("factor expressions must not use attribute access")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        raise ValueError("factor expressions must not use lambda")

    def visit_ListComp(self, node: ast.ListComp) -> None:
        raise ValueError("factor expressions must not use comprehension")

    def visit_SetComp(self, node: ast.SetComp) -> None:
        raise ValueError("factor expressions must not use comprehension")

    def visit_DictComp(self, node: ast.DictComp) -> None:
        raise ValueError("factor expressions must not use comprehension")

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        raise ValueError("factor expressions must not use comprehension")

    def generic_visit(self, node: ast.AST) -> None:
        allowed_nodes = (
            ast.Expression,
            ast.Call,
            ast.Name,
            ast.Constant,
            ast.BinOp,
            ast.UnaryOp,
            ast.Load,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.UAdd,
            ast.USub,
        )
        if not isinstance(node, allowed_nodes):
            raise ValueError(f"factor expression uses disallowed syntax: {type(node).__name__}")
        super().generic_visit(node)

    @staticmethod
    def _validate_ref_call(node: ast.Call) -> None:
        if len(node.args) != 2:
            raise ValueError("Ref(field, n) requires exactly two arguments")
        offset = _numeric_constant(node.args[1])
        if offset is None:
            raise ValueError("Ref offset must be a numeric constant")
        if offset < 0:
            raise ValueError("feature expressions must not use future Ref offsets")


def _numeric_constant(node: ast.AST) -> int | float | None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            return None
        return require_finite_number(node.value, "factor constant")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ALLOWED_UNARY_OPS):
        value = _numeric_constant(node.operand)
        if value is None:
            return None
        if isinstance(node.op, ast.USub):
            return -value
        return value
    return None


def _reject_dunder(name: str) -> None:
    if "__" in name:
        raise ValueError("factor expressions must not use dunder names")


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
