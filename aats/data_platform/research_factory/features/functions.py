"""Pure Python factor expression evaluator for Research Factory rows."""

from __future__ import annotations

import ast
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aats.data_platform.research_factory.features.expressions import (
    ALLOWED_BINARY_OPS,
    ALLOWED_UNARY_OPS,
    FactorExpression,
    parse_factor_expression,
)
from aats.data_platform.research_factory.numeric import require_finite_number

NumericValue = int | float | Decimal


@dataclass(frozen=True, slots=True)
class FactorEvaluationResult:
    """Evaluated factor values plus per-row missing reasons."""

    values: tuple[float | None, ...]
    missing_reasons: Mapping[int, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing_reasons = {
            row_index: tuple(reasons)
            for row_index, reasons in self.missing_reasons.items()
        }
        object.__setattr__(self, "missing_reasons", missing_reasons)


def evaluate_factor_expression(
    expression: str | FactorExpression,
    rows: Sequence[Mapping[str, Any]],
) -> FactorEvaluationResult:
    """Evaluate a safe factor expression against immutable row mappings."""
    expression_text = expression.expression if isinstance(expression, FactorExpression) else expression
    parsed = parse_factor_expression(expression_text)
    if not isinstance(rows, Sequence):
        raise ValueError("rows must be a sequence")
    row_tuple = tuple(rows)
    if not all(isinstance(row, Mapping) for row in row_tuple):
        raise ValueError("rows must contain mapping rows")

    tree = ast.parse(parsed.expression, mode="eval")
    evaluator = _FactorRowEvaluator(row_tuple)
    values: list[float | None] = []
    for row_index in range(len(row_tuple)):
        values.append(evaluator.evaluate(tree.body, row_index))
    return FactorEvaluationResult(
        values=tuple(values),
        missing_reasons=evaluator.missing_reasons,
    )


class _FactorRowEvaluator:
    def __init__(self, rows: tuple[Mapping[str, Any], ...]) -> None:
        self.rows = rows
        self.missing_reasons: defaultdict[int, list[str]] = defaultdict(list)

    def evaluate(self, node: ast.AST, row_index: int) -> float | None:
        if isinstance(node, ast.Constant):
            return self._constant(node, row_index)
        if isinstance(node, ast.Name):
            return self._field_value(node.id, row_index)
        if isinstance(node, ast.Call):
            return self._call(node, row_index)
        if isinstance(node, ast.BinOp):
            return self._bin_op(node, row_index)
        if isinstance(node, ast.UnaryOp):
            return self._unary_op(node, row_index)
        self._add_missing(row_index, f"unsupported syntax: {type(node).__name__}")
        return None

    def _call(self, node: ast.Call, row_index: int) -> float | None:
        if not isinstance(node.func, ast.Name):
            self._add_missing(row_index, "unsupported call target")
            return None
        function_name = node.func.id
        if function_name == "Ref":
            return self._ref(node, row_index)
        if function_name == "Return":
            return self._return(node, row_index)
        if function_name == "Mean":
            return self._rolling(node, row_index, "Mean")
        if function_name == "Std":
            return self._rolling(node, row_index, "Std")
        if function_name == "ZScore":
            return self._zscore(node, row_index)
        if function_name == "Max":
            return self._rolling(node, row_index, "Max")
        if function_name == "Min":
            return self._rolling(node, row_index, "Min")
        if function_name == "Rank":
            return self._rank(node, row_index)
        if function_name == "Delta":
            return self._delta(node, row_index)
        self._add_missing(row_index, f"unsupported function: {function_name}")
        return None

    def _ref(self, node: ast.Call, row_index: int) -> float | None:
        field_name = _field_arg(node, row_index, self._add_missing)
        offset = _window_arg(node, row_index, self._add_missing)
        if field_name is None or offset is None:
            return None
        if offset < 0:
            self._add_missing(row_index, "future Ref offsets are not allowed")
            return None
        target_index = row_index - offset
        if target_index < 0:
            self._add_missing(row_index, f"insufficient history for Ref({field_name}, {offset})")
            return None
        return self._field_value(field_name, target_index, reason_row_index=row_index)

    def _return(self, node: ast.Call, row_index: int) -> float | None:
        field_name = _field_arg(node, row_index, self._add_missing)
        window = _window_arg(node, row_index, self._add_missing)
        if field_name is None or window is None:
            return None
        current = self._field_value(field_name, row_index)
        past = self._ref_value(field_name, window, row_index)
        if current is None or past is None:
            return None
        if past == 0:
            self._add_missing(row_index, f"division by zero in Return({field_name}, {window})")
            return None
        return current / past - 1.0

    def _delta(self, node: ast.Call, row_index: int) -> float | None:
        field_name = _field_arg(node, row_index, self._add_missing)
        window = _window_arg(node, row_index, self._add_missing)
        if field_name is None or window is None:
            return None
        current = self._field_value(field_name, row_index)
        past = self._ref_value(field_name, window, row_index)
        if current is None or past is None:
            return None
        return current - past

    def _zscore(self, node: ast.Call, row_index: int) -> float | None:
        series_node = _series_arg(node, row_index, self._add_missing)
        window = _window_arg(node, row_index, self._add_missing)
        if series_node is None or window is None:
            return None
        current = self._series_value(series_node, row_index, reason_row_index=row_index)
        values = self._rolling_values(series_node, window, row_index, function_name="ZScore")
        if current is None or values is None:
            return None
        std_value = _std(values)
        if std_value == 0:
            self._add_missing(row_index, f"zero std in ZScore({_series_label(series_node)}, {window})")
            return None
        return (current - _mean(values)) / std_value

    def _rank(self, node: ast.Call, row_index: int) -> float | None:
        series_node = _series_arg(node, row_index, self._add_missing)
        window = _window_arg(node, row_index, self._add_missing)
        if series_node is None or window is None:
            return None
        current = self._series_value(series_node, row_index, reason_row_index=row_index)
        values = self._rolling_values(series_node, window, row_index, function_name="Rank")
        if current is None or values is None:
            return None
        if len(values) == 1:
            return 1.0
        lower_or_equal = sum(1 for value in values if value <= current)
        return (lower_or_equal - 1) / (len(values) - 1)

    def _rolling(self, node: ast.Call, row_index: int, function_name: str) -> float | None:
        series_node = _series_arg(node, row_index, self._add_missing)
        window = _window_arg(node, row_index, self._add_missing)
        if series_node is None or window is None:
            return None
        values = self._rolling_values(series_node, window, row_index, function_name=function_name)
        if values is None:
            return None
        if function_name == "Mean":
            return _mean(values)
        if function_name == "Std":
            return _std(values)
        if function_name == "Max":
            return max(values)
        if function_name == "Min":
            return min(values)
        self._add_missing(row_index, f"unsupported rolling function: {function_name}")
        return None

    def _bin_op(self, node: ast.BinOp, row_index: int) -> float | None:
        if not isinstance(node.op, ALLOWED_BINARY_OPS):
            self._add_missing(row_index, "disallowed binary operator")
            return None
        left = self.evaluate(node.left, row_index)
        right = self.evaluate(node.right, row_index)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                self._add_missing(row_index, "division by zero")
                return None
            return left / right
        self._add_missing(row_index, "unsupported binary operator")
        return None

    def _unary_op(self, node: ast.UnaryOp, row_index: int) -> float | None:
        if not isinstance(node.op, ALLOWED_UNARY_OPS):
            self._add_missing(row_index, "disallowed unary operator")
            return None
        value = self.evaluate(node.operand, row_index)
        if value is None:
            return None
        if isinstance(node.op, ast.USub):
            return -value
        return value

    def _constant(self, node: ast.Constant, row_index: int) -> float | None:
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            self._add_missing(row_index, "non-numeric constant")
            return None
        try:
            return require_finite_number(node.value, "factor constant")
        except ValueError as exc:
            self._add_missing(row_index, str(exc))
            return None

    def _ref_value(self, field_name: str, offset: int, row_index: int) -> float | None:
        if offset < 0:
            self._add_missing(row_index, "future offsets are not allowed")
            return None
        target_index = row_index - offset
        if target_index < 0:
            self._add_missing(row_index, f"insufficient history for {field_name} window {offset}")
            return None
        return self._field_value(field_name, target_index, reason_row_index=row_index)

    def _rolling_values(
        self,
        series_node: ast.AST,
        window: int,
        row_index: int,
        *,
        function_name: str,
    ) -> tuple[float, ...] | None:
        if window <= 0:
            self._add_missing(row_index, "rolling window must be positive")
            return None
        start_index = row_index - window + 1
        if start_index < 0:
            self._add_missing(
                row_index,
                f"insufficient history for {_series_label(series_node)} window {window}",
            )
            return None
        values: list[float] = []
        for source_index in range(start_index, row_index + 1):
            value = self._series_value(series_node, source_index, reason_row_index=row_index)
            if value is None:
                self._add_missing(
                    row_index,
                    f"nested expression missing in {function_name}({_series_label(series_node)}, {window})",
                )
                return None
            values.append(value)
        return tuple(values)

    def _series_value(
        self,
        series_node: ast.AST,
        row_index: int,
        *,
        reason_row_index: int,
    ) -> float | None:
        if isinstance(series_node, ast.Name):
            return self._field_value(series_node.id, row_index, reason_row_index=reason_row_index)
        return self.evaluate(series_node, row_index)

    def _field_value(
        self,
        field_name: str,
        row_index: int,
        *,
        reason_row_index: int | None = None,
    ) -> float | None:
        reason_index = row_index if reason_row_index is None else reason_row_index
        row = self.rows[row_index]
        if field_name not in row:
            self._add_missing(reason_index, f"field {field_name!r} missing")
            return None
        value = row[field_name]
        if value is None:
            self._add_missing(reason_index, f"field {field_name!r} is null")
            return None
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            self._add_missing(reason_index, f"field {field_name!r} is non-numeric")
            return None
        try:
            return require_finite_number(value, f"field {field_name!r}")
        except ValueError as exc:
            self._add_missing(reason_index, str(exc))
            return None

    def _add_missing(self, row_index: int, reason: str) -> None:
        if reason not in self.missing_reasons[row_index]:
            self.missing_reasons[row_index].append(reason)


def _field_arg(
    node: ast.Call,
    row_index: int,
    add_missing: Any,
) -> str | None:
    if not node.args or not isinstance(node.args[0], ast.Name):
        add_missing(row_index, "first function argument must be a field")
        return None
    return node.args[0].id


def _series_arg(
    node: ast.Call,
    row_index: int,
    add_missing: Any,
) -> ast.AST | None:
    if not node.args:
        add_missing(row_index, "function requires a series argument")
        return None
    return node.args[0]


def _series_label(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def _window_arg(
    node: ast.Call,
    row_index: int,
    add_missing: Any,
) -> int | None:
    if len(node.args) < 2:
        add_missing(row_index, "function requires a window argument")
        return None
    value = _numeric_constant(node.args[1])
    if value is None or int(value) != value:
        add_missing(row_index, "window argument must be an integer")
        return None
    return int(value)


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


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _std(values: Sequence[float]) -> float:
    mean_value = _mean(values)
    return math.sqrt(sum((value - mean_value) ** 2 for value in values) / len(values))
