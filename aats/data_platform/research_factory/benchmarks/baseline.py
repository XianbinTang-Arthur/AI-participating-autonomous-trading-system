"""Factor-only baseline benchmark harness for Research Factory."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from aats.data_platform.research_factory.numeric import require_finite_number
from aats.data_platform.research_factory.specs import METRIC_FIELDS, MetricsSnapshot

NumericValue = int | float | Decimal
DEFAULT_PERIODS_PER_YEAR = 365.0 * 24.0
EXECUTION_REALISM_MISSING = "execution realism is not measured by factor baseline v1"
ICIR_MISSING = "requires rolling IC windows"


def run_factor_baseline(
    dataset: Any,
    factor_values: Sequence[NumericValue | None],
    label_values: Sequence[NumericValue | None],
    cost_config: Mapping[str, NumericValue] | None = None,
) -> MetricsSnapshot:
    """Run a deterministic factor-only long/flat baseline."""
    if dataset is None:
        raise ValueError("dataset is required")
    if not isinstance(factor_values, Sequence) or not isinstance(label_values, Sequence):
        raise ValueError("factor_values and label_values must be sequences")
    if len(factor_values) != len(label_values):
        raise ValueError("factor_values and label_values must have the same length")

    costs = _normalize_cost_config(cost_config or {})
    pairs = _valid_pairs(factor_values, label_values)
    if not pairs:
        return _failed_metrics("no valid factor/label pairs; candidate not generated")

    factors = [pair[0] for pair in pairs]
    labels = [pair[1] for pair in pairs]
    ic = _pearson(factors, labels)
    rank_ic = _pearson(_ranks(factors), _ranks(labels))

    gross_returns, net_returns, signal_changes = _long_flat_returns(
        factor_values,
        label_values,
        trade_cost_bps=costs["trade_cost_bps"],
        funding_bps=costs["funding_bps"],
    )
    annualized_return = _annualize(gross_returns, costs["periods_per_year"])
    net_annualized_return = _annualize(net_returns, costs["periods_per_year"])
    turnover = _mean(signal_changes) if signal_changes else 0.0
    information_ratio = _risk_adjusted_ratio(net_returns, costs["periods_per_year"])
    sharpe = information_ratio
    max_drawdown = _max_drawdown(net_returns)
    cost_adjusted_edge_bps_mean = _mean([value * 10_000.0 for value in net_returns])

    missing_reasons = _base_missing_reasons()
    _add_missing_when_none(
        missing_reasons,
        {
            "ic": ic,
            "rank_ic": rank_ic,
            "information_ratio": information_ratio,
            "sharpe": sharpe,
        },
    )
    return MetricsSnapshot(
        ic=ic,
        rank_ic=rank_ic,
        annualized_return=annualized_return,
        net_annualized_return=net_annualized_return,
        information_ratio=information_ratio,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        turnover=turnover,
        fee_bps_mean=costs["fee_bps"],
        slippage_bps_mean=costs["slippage_bps"],
        funding_bps_mean=costs["funding_bps"],
        cost_adjusted_edge_bps_mean=cost_adjusted_edge_bps_mean,
        missing_reasons=missing_reasons,
    )


def factor_baseline_return_series(
    factor_values: Sequence[NumericValue | None],
    label_values: Sequence[NumericValue | None],
    cost_config: Mapping[str, NumericValue] | None = None,
) -> tuple[float, ...]:
    """Return the deterministic net-return series used by the factor baseline.

    The public helper exists so statistical evidence can be derived from the
    exact same return and cost convention as :func:`run_factor_baseline`.
    It intentionally exposes no holdout-selection behavior; callers decide
    which already-authorized dataset segment is supplied.
    """
    if not isinstance(factor_values, Sequence) or not isinstance(label_values, Sequence):
        raise ValueError("factor_values and label_values must be sequences")
    if len(factor_values) != len(label_values):
        raise ValueError("factor_values and label_values must have the same length")
    costs = _normalize_cost_config(cost_config or {})
    _, net_returns, _ = _long_flat_returns(
        factor_values,
        label_values,
        trade_cost_bps=costs["trade_cost_bps"],
        funding_bps=costs["funding_bps"],
    )
    return tuple(net_returns)


def _normalize_cost_config(cost_config: Mapping[str, NumericValue]) -> dict[str, float]:
    if not isinstance(cost_config, Mapping):
        raise ValueError("cost_config must be a mapping")
    fee_bps = _non_negative_float(cost_config.get("fee_bps", 0.0), "fee_bps")
    slippage_bps = _non_negative_float(cost_config.get("slippage_bps", 0.0), "slippage_bps")
    funding_bps = _float_value(cost_config.get("funding_bps", 0.0), "funding_bps")
    periods_per_year = _positive_float(
        cost_config.get("periods_per_year", DEFAULT_PERIODS_PER_YEAR),
        "periods_per_year",
    )
    return {
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "funding_bps": funding_bps,
        "trade_cost_bps": fee_bps + slippage_bps,
        "periods_per_year": periods_per_year,
    }


def _valid_pairs(
    factor_values: Sequence[NumericValue | None],
    label_values: Sequence[NumericValue | None],
) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for factor_value, label_value in zip(factor_values, label_values, strict=True):
        factor = _optional_float(factor_value)
        label = _optional_float(label_value)
        if factor is not None and label is not None:
            pairs.append((factor, label))
    return pairs


def _long_flat_returns(
    factor_values: Sequence[NumericValue | None],
    label_values: Sequence[NumericValue | None],
    *,
    trade_cost_bps: float,
    funding_bps: float,
) -> tuple[list[float], list[float], list[float]]:
    gross_returns: list[float] = []
    net_returns: list[float] = []
    signal_changes: list[float] = []
    previous_signal = 0.0
    trade_cost_return = trade_cost_bps / 10_000.0
    funding_return = funding_bps / 10_000.0

    for factor_value, label_value in zip(factor_values, label_values, strict=True):
        factor = _optional_float(factor_value)
        label = _optional_float(label_value)
        signal = 1.0 if factor is not None and factor > 0 else 0.0
        turnover = abs(signal - previous_signal)
        signal_changes.append(turnover)
        previous_signal = signal
        if label is None:
            continue
        gross_return = label * signal
        net_return = gross_return - trade_cost_return * turnover - funding_return * signal
        gross_returns.append(gross_return)
        net_returns.append(net_return)

    return gross_returns, net_returns, signal_changes


def _failed_metrics(reason: str) -> MetricsSnapshot:
    missing_reasons = {field_name: reason for field_name in METRIC_FIELDS}
    missing_reasons["candidate_generated"] = "false"
    return MetricsSnapshot(missing_reasons=missing_reasons)


def _base_missing_reasons() -> dict[str, str]:
    return {
        "icir": ICIR_MISSING,
        "rank_icir": ICIR_MISSING,
        "fillable_ratio": EXECUTION_REALISM_MISSING,
        "partial_fill_ratio": EXECUTION_REALISM_MISSING,
    }


def _add_missing_when_none(
    missing_reasons: dict[str, str],
    values_by_metric: Mapping[str, float | None],
) -> None:
    for metric_name, value in values_by_metric.items():
        if value is not None:
            continue
        if metric_name in {"ic", "rank_ic"}:
            missing_reasons[metric_name] = "correlation undefined for constant or insufficient valid pairs"
        else:
            missing_reasons[metric_name] = "risk-adjusted ratio undefined for flat or insufficient returns"


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right):
        raise ValueError("correlation inputs must have the same length")
    if len(left) < 2:
        return None
    left_mean = _mean(left)
    right_mean = _mean(right)
    left_diffs = [value - left_mean for value in left]
    right_diffs = [value - right_mean for value in right]
    numerator = sum(left_diff * right_diff for left_diff, right_diff in zip(left_diffs, right_diffs, strict=True))
    left_denominator = math.sqrt(sum(left_diff * left_diff for left_diff in left_diffs))
    right_denominator = math.sqrt(sum(right_diff * right_diff for right_diff in right_diffs))
    denominator = left_denominator * right_denominator
    if denominator == 0:
        return None
    return numerator / denominator


def _ranks(values: Sequence[float]) -> list[float]:
    sorted_pairs = sorted((value, index) for index, value in enumerate(values))
    ranks = [0.0] * len(values)
    index = 0
    while index < len(sorted_pairs):
        end = index + 1
        while end < len(sorted_pairs) and sorted_pairs[end][0] == sorted_pairs[index][0]:
            end += 1
        average_rank = (index + end - 1) / 2.0
        for _, original_index in sorted_pairs[index:end]:
            ranks[original_index] = average_rank
        index = end
    return ranks


def _annualize(returns: Sequence[float], periods_per_year: float) -> float:
    if not returns:
        return 0.0
    return _mean(returns) * periods_per_year


def _risk_adjusted_ratio(returns: Sequence[float], periods_per_year: float) -> float | None:
    if len(returns) < 2:
        return None
    std_value = _sample_std(returns)
    if std_value == 0:
        return None
    return _mean(returns) / std_value * math.sqrt(periods_per_year)


def _max_drawdown(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for return_value in returns:
        equity *= 1.0 + return_value
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        max_drawdown = min(max_drawdown, drawdown)
    return max_drawdown


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean_value = _mean(values)
    return math.sqrt(sum((value - mean_value) ** 2 for value in values) / (len(values) - 1))


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError("factor and label values must be numeric or None")
    return require_finite_number(value, "factor and label value")


def _float_value(value: Any, field_name: str) -> float:
    return require_finite_number(value, field_name)


def _non_negative_float(value: Any, field_name: str) -> float:
    result = _float_value(value, field_name)
    if result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _positive_float(value: Any, field_name: str) -> float:
    result = _float_value(value, field_name)
    if result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result
