"""Execution Realism 聚合模块 (Phase 4-E).

跨 family / timeframe 做 execution realism 比较，
生成 execution_realism_comparison.csv 的行数据。
"""

from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)


def build_execution_realism_comparison(
    all_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """跨 family/timeframe 构建 execution realism 比较表。

    Args:
        all_results: {ft_key -> {"cost_summary": ..., "slippage_rows": ...}}

    Returns:
        比较行列表，每行对应一个 family/timeframe 组合。
    """
    comparison_rows: list[dict[str, Any]] = []

    for ft_key, data in all_results.items():
        cost_summary = data.get("cost_summary", {})
        slippage_rows = data.get("slippage_rows", [])

        # 从 ft_key 解析 family 和 timeframe
        parts = ft_key.rsplit("_", 1)
        family = parts[0] if len(parts) >= 2 else ft_key
        timeframe = parts[1] if len(parts) >= 2 else ""

        # 基础统计
        candidate_count = cost_summary.get("total_candidates", 0)
        opening_count = cost_summary.get("total_openings", 0)

        # 成交可行性
        full_fill_ratio = cost_summary.get("full_fill_ratio", 0)
        partial_fill_ratio = cost_summary.get("partial_fill_ratio", 0)
        not_fillable_ratio = cost_summary.get("not_fillable_ratio", 0)

        # 滑点
        slippage = cost_summary.get("slippage", {})
        mean_slippage = slippage.get("mean", 0)
        median_slippage = slippage.get("median", 0)
        p95_slippage = slippage.get("p95", 0)

        # 总执行成本
        total_cost = cost_summary.get("total_execution_cost", {})
        mean_total_cost = total_cost.get("mean", 0)

        # 成本调整后的 edge
        edge = cost_summary.get("cost_adjusted_edge", {})
        mean_edge = edge.get("mean", 0)
        positive_edge_ratio = cost_summary.get("positive_edge_ratio", 0)

        # Phase 2 比较
        cost_comp = cost_summary.get("cost_comparison_with_phase2", {})
        cost_delta = cost_comp.get("mean_cost_delta_bps", 0)

        # Top execution failure mode
        top_failure = _identify_top_execution_failure(slippage_rows)

        comparison_rows.append({
            "family": family,
            "timeframe": timeframe,
            "parameter_set": "default",
            "candidate_count": candidate_count,
            "opening_count": opening_count,
            "full_fill_ratio": round(full_fill_ratio, 4),
            "partial_fill_ratio": round(partial_fill_ratio, 4),
            "not_fillable_ratio": round(not_fillable_ratio, 4),
            "mean_slippage_bps": round(mean_slippage, 3),
            "median_slippage_bps": round(median_slippage, 3),
            "p95_slippage_bps": round(p95_slippage, 3),
            "mean_total_execution_cost_bps": round(mean_total_cost, 3),
            "cost_adjusted_edge_proxy": round(mean_edge, 3),
            "positive_edge_ratio": round(positive_edge_ratio, 4),
            "cost_delta_vs_phase2_bps": round(cost_delta, 3),
            "top_execution_failure_mode": top_failure,
        })

    log.info("Built comparison table with %d rows", len(comparison_rows))
    return comparison_rows


def generate_cross_comparison_findings(
    comparison_rows: list[dict[str, Any]],
) -> list[str]:
    """从比较表生成跨 family/timeframe 的发现。

    Returns:
        发现列表（中文字符串）。
    """
    findings: list[str] = []
    if len(comparison_rows) < 2:
        return findings

    # 1. Family 比较 (independent vs directional)
    ind_rows = [r for r in comparison_rows if r["family"] == "independent"]
    dir_rows = [r for r in comparison_rows if r["family"] == "directional"]

    if ind_rows and dir_rows:
        ind_avg_slip = sum(r["mean_slippage_bps"] for r in ind_rows) / len(ind_rows)
        dir_avg_slip = sum(r["mean_slippage_bps"] for r in dir_rows) / len(dir_rows)
        if abs(ind_avg_slip - dir_avg_slip) > 0.1:
            higher = "independent" if ind_avg_slip > dir_avg_slip else "directional"
            findings.append(
                f"{higher} 的平均滑点更高 "
                f"(independent: {ind_avg_slip:.2f} bps, directional: {dir_avg_slip:.2f} bps)"
            )

        ind_avg_edge = sum(r["cost_adjusted_edge_proxy"] for r in ind_rows) / len(ind_rows)
        dir_avg_edge = sum(r["cost_adjusted_edge_proxy"] for r in dir_rows) / len(dir_rows)
        if abs(ind_avg_edge - dir_avg_edge) > 0.5:
            better = "independent" if ind_avg_edge > dir_avg_edge else "directional"
            findings.append(
                f"{better} 的成本调整后 edge 更高 "
                f"(independent: {ind_avg_edge:.2f} bps, directional: {dir_avg_edge:.2f} bps)"
            )

    # 2. Timeframe 比较 (15m vs 1H)
    tf_15m = [r for r in comparison_rows if r["timeframe"].lower() == "15m"]
    tf_1h = [r for r in comparison_rows if r["timeframe"].lower() in ("1h",)]

    if tf_15m and tf_1h:
        avg_15m_fill = sum(r["full_fill_ratio"] for r in tf_15m) / len(tf_15m)
        avg_1h_fill = sum(r["full_fill_ratio"] for r in tf_1h) / len(tf_1h)
        if abs(avg_15m_fill - avg_1h_fill) > 0.01:
            better = "15m" if avg_15m_fill > avg_1h_fill else "1H"
            findings.append(
                f"{better} 的 full fill ratio 更高 "
                f"(15m: {avg_15m_fill:.1%}, 1H: {avg_1h_fill:.1%})"
            )

        avg_15m_openings = sum(r["opening_count"] for r in tf_15m) / len(tf_15m)
        avg_1h_openings = sum(r["opening_count"] for r in tf_1h) / len(tf_1h)
        if avg_15m_openings > 0 and avg_1h_openings > 0:
            ratio = avg_15m_openings / avg_1h_openings
            if ratio > 1.5:
                findings.append(
                    f"15m 的 opening 数量是 1H 的 {ratio:.1f} 倍，"
                    f"但需关注 15m 上 execution cost 是否侵蚀了频率优势"
                )

    # 3. 成本假设检验
    for row in comparison_rows:
        delta = row.get("cost_delta_vs_phase2_bps", 0)
        if abs(delta) > 1.0:
            direction = "低估" if delta > 0 else "高估"
            findings.append(
                f"{row['family']}/{row['timeframe']}: "
                f"Phase 2 默认 cost {direction}了 {abs(delta):.2f} bps"
            )

    # 4. Edge 正率检查
    for row in comparison_rows:
        pe_ratio = row.get("positive_edge_ratio", 0)
        if 0 < pe_ratio < 0.5:
            findings.append(
                f"{row['family']}/{row['timeframe']}: "
                f"成本调整后仅 {pe_ratio:.0%} 的机会有正 edge，"
                f"execution cost 严重侵蚀策略信号"
            )

    return findings


def _identify_top_execution_failure(
    slippage_rows: list[dict[str, Any]],
) -> str:
    """识别主要的执行失败模式。"""
    if not slippage_rows:
        return "no_candidates"

    # 统计各类问题
    no_data = sum(
        1
        for r in slippage_rows
        if r.get("feasibility_category") == "insufficient_market_data"
        or r.get("slippage_data_quality") == "no_data"
    )
    not_fillable = sum(1 for r in slippage_rows
                       if r.get("feasibility_category") == "not_fillable")
    negative_edge = sum(
        1
        for r in slippage_rows
        if (_finite_number(r.get("cost_adjusted_edge_bps"))) is not None
        and _finite_number(r.get("cost_adjusted_edge_bps")) <= 0
    )
    high_slippage = sum(
        1
        for r in slippage_rows
        if (_finite_number(r.get("estimated_slippage_bps"))) is not None
        and _finite_number(r.get("estimated_slippage_bps")) > 5.0
    )

    counts = {
        "insufficient_data": no_data,
        "not_fillable": not_fillable,
        "negative_cost_adjusted_edge": negative_edge,
        "high_slippage_gt_5bps": high_slippage,
    }

    if not any(counts.values()):
        return "none"

    top = max(counts, key=lambda k: counts[k])
    return f"{top}({counts[top]})"


def _finite_number(value: Any) -> float | None:
    """将 CSV/JSON 标量规范化为有限浮点数；空值和非有限值都视为缺失。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
