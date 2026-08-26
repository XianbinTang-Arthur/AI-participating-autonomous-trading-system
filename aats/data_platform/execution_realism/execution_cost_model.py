"""Execution Cost Model (Phase 4-D).

将 slippage + fee + feasibility 合成 execution cost realism 汇总输出。

产出 execution_cost_summary.json，包含:
  - 全局统计（均值、中位数、P95 滑点）
  - 成交可行性分布
  - 与 Phase 2 默认 cost 假设的比较
  - 成本调整后的 edge 分布
"""

from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)


def build_execution_cost_summary(
    slippage_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """构建 execution cost summary。

    Args:
        slippage_rows: 来自 slippage_estimator 的完整结果行。

    Returns:
        汇总字典，供写入 execution_cost_summary.json。
    """
    total = len(slippage_rows)
    if total == 0:
        return _empty_summary()

    # ---- 1. 成交可行性分布 ----
    feasibility_counts: dict[str, int] = {}
    for row in slippage_rows:
        cat = row.get("feasibility_category", "unknown")
        feasibility_counts[cat] = feasibility_counts.get(cat, 0) + 1

    full_fill_count = feasibility_counts.get("fully_fillable", 0)
    partial_fill_count = feasibility_counts.get("partially_fillable", 0)
    not_fillable_count = feasibility_counts.get("not_fillable", 0)
    no_data_count = feasibility_counts.get("insufficient_market_data", 0)

    # ---- 2. 滑点统计 ----
    valid_slippage = _finite_values(slippage_rows, "estimated_slippage_bps")

    valid_total_cost = _finite_values(slippage_rows, "estimated_total_execution_cost_bps")
    valid_fee = _finite_values(slippage_rows, "estimated_fee_bps")
    valid_funding = [
        _funding_bps(row)
        for row in slippage_rows
        if _funding_bps(row) is not None
    ]
    row_turnover = _finite_values(slippage_rows, "turnover")

    valid_cost_adjusted_edge = _finite_values(slippage_rows, "cost_adjusted_edge_bps")

    valid_cost_vs_assumed = _finite_values(slippage_rows, "cost_vs_assumed_bps")

    slippage_stats = _compute_distribution_stats(valid_slippage) if valid_slippage else {}
    cost_stats = _compute_distribution_stats(valid_total_cost) if valid_total_cost else {}
    fee_stats = _compute_distribution_stats(valid_fee) if valid_fee else {}
    funding_stats = _compute_distribution_stats(valid_funding) if valid_funding else {}
    edge_stats = _compute_distribution_stats(valid_cost_adjusted_edge) if valid_cost_adjusted_edge else {}

    # ---- 3. 与 Phase 2 假设的比较 ----
    cost_comparison = {}
    if valid_cost_vs_assumed:
        avg_delta = sum(valid_cost_vs_assumed) / len(valid_cost_vs_assumed)
        cost_comparison = {
            "mean_cost_delta_bps": round(avg_delta, 3),
            "cost_is_underestimated": avg_delta > 0,
            "cost_is_overestimated": avg_delta < 0,
            "interpretation": (
                f"Phase 2 默认 cost 平均{'低估' if avg_delta > 0 else '高估'}了 "
                f"{abs(avg_delta):.2f} bps"
            ),
        }

    # ---- 4. edge 正负统计 ----
    positive_edge_count = sum(1 for e in valid_cost_adjusted_edge if e > 0)
    negative_edge_count = sum(1 for e in valid_cost_adjusted_edge if e <= 0)

    # ---- 5. openings vs closes 分拆 ----
    opening_rows = [r for r in slippage_rows if r.get("candidate_action") == "open"]
    close_rows = [r for r in slippage_rows if r.get("candidate_action") == "close"]
    action_turnover_ratio = round((len(opening_rows) + len(close_rows)) / max(total, 1), 4)
    turnover_stats = (
        _compute_distribution_stats(row_turnover)
        if row_turnover
        else {"mean": action_turnover_ratio, "count": total}
    )

    opening_slippage = _finite_values(opening_rows, "estimated_slippage_bps")
    close_slippage = _finite_values(close_rows, "estimated_slippage_bps")

    summary = {
        "total_candidates": total,
        "total_openings": len(opening_rows),
        "total_closes": len(close_rows),
        # 成交可行性
        "full_fill_count": full_fill_count,
        "full_fill_ratio": round(full_fill_count / max(total, 1), 4),
        "partial_fill_count": partial_fill_count,
        "partial_fill_ratio": round(partial_fill_count / max(total, 1), 4),
        "not_fillable_count": not_fillable_count,
        "not_fillable_ratio": round(not_fillable_count / max(total, 1), 4),
        "insufficient_data_count": no_data_count,
        # 滑点分布
        "slippage": slippage_stats,
        "opening_slippage": _compute_distribution_stats(opening_slippage) if opening_slippage else {},
        "close_slippage": _compute_distribution_stats(close_slippage) if close_slippage else {},
        # 总执行成本分布
        "total_execution_cost": cost_stats,
        "fee": fee_stats,
        "funding": funding_stats,
        "turnover": turnover_stats,
        # 成本调整后的 edge
        "cost_adjusted_edge": edge_stats,
        "positive_edge_count": positive_edge_count,
        "positive_edge_ratio": round(positive_edge_count / max(len(valid_cost_adjusted_edge), 1), 4),
        "negative_edge_count": negative_edge_count,
        # 与 Phase 2 比较
        "cost_comparison_with_phase2": cost_comparison,
        # 模型元数据
        "model_version": "v1_bar_proxy",
        "data_source": "gold_ohlcv_bars",
        "limitations": [
            "V1 无 orderbook depth 数据，spread 基于 bar range 估计",
            "V1 无 trades 数据，impact 基于 sqrt(volume_ratio) 模型",
            "BTC-USDT-SWAP 1 合约 = 0.01 BTC，当前仓位极小",
        ],
    }

    log.info("Execution cost summary: %d candidates, "
             "full_fill=%.1f%%, mean_slippage=%.2f bps, mean_total_cost=%.2f bps",
             total,
             summary["full_fill_ratio"] * 100,
             slippage_stats.get("mean", 0),
             cost_stats.get("mean", 0))

    return summary


def _compute_distribution_stats(values: list[float]) -> dict[str, float]:
    """计算分布统计量（均值、中位数、P5、P25、P75、P95、最大/最小）。"""
    if not values:
        return {}

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    return {
        "mean": round(sum(sorted_vals) / n, 3),
        "median": round(_percentile(sorted_vals, 50), 3),
        "std": round(_std(sorted_vals), 3),
        "min": round(sorted_vals[0], 3),
        "p5": round(_percentile(sorted_vals, 5), 3),
        "p25": round(_percentile(sorted_vals, 25), 3),
        "p75": round(_percentile(sorted_vals, 75), 3),
        "p95": round(_percentile(sorted_vals, 95), 3),
        "max": round(sorted_vals[-1], 3),
        "count": n,
    }


def _percentile(sorted_vals: list[float], p: float) -> float:
    """计算百分位数（线性插值）。"""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = (p / 100) * (n - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _std(values: list[float]) -> float:
    """计算标准差。"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def _empty_summary() -> dict[str, Any]:
    """空数据时的默认 summary。"""
    return {
        "total_candidates": 0,
        "total_openings": 0,
        "total_closes": 0,
        "full_fill_count": 0,
        "full_fill_ratio": 0,
        "partial_fill_count": 0,
        "partial_fill_ratio": 0,
        "not_fillable_count": 0,
        "not_fillable_ratio": 0,
        "insufficient_data_count": 0,
        "slippage": {},
        "total_execution_cost": {},
        "fee": {},
        "funding": {},
        "turnover": {},
        "cost_adjusted_edge": {},
        "positive_edge_count": 0,
        "positive_edge_ratio": 0,
        "negative_edge_count": 0,
        "cost_comparison_with_phase2": {},
        "model_version": "v1_bar_proxy",
        "data_source": "gold_ohlcv_bars",
        "limitations": [],
    }


def _funding_bps(row: dict[str, Any]) -> float | None:
    if row.get("funding_bps") is not None:
        return _finite_number(row["funding_bps"])
    if row.get("funding_adjustment_bps") is not None:
        return _finite_number(row["funding_adjustment_bps"])
    return None


def _finite_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = _finite_number(row.get(key))
        if value is not None:
            values.append(value)
    return values


def _finite_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
