"""Replay diagnostics: structured analysis of replay decisions.

Phase 2 设计决策 §12：
- Diagnostics Engine 必须是独立模块
- 不允许把统计逻辑散落在 report builder 或 parameter scan runner 里
- 核心不是只看数量，而是看参数如何影响真实历史机会结构

第一批必须支持的诊断指标（§12.2）：
- opening_count
- blocked_count
- selectable_ratio
- execution_compatible_ratio
- blocking_reasons_top_n
- score_distribution
- expected_edge_distribution
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from typing import Any

from aats.data_platform.replay.core.replay_context import ReplayDecision

log = logging.getLogger(__name__)


def compute_diagnostics(
    decisions: list[ReplayDecision],
    *,
    top_n_blocking: int = 10,
) -> dict[str, Any]:
    """对一组 replay decisions 计算完整诊断指标。

    返回字典可直接用于：
    - 写入 experiment_summaries（upsert_experiment_summary）
    - 写入 diagnostics.json（落文件）
    - 传递给 report builder
    """
    if not decisions:
        return _empty_diagnostics()

    total = len(decisions)

    # --- 基础计数 ---
    opening = sum(1 for d in decisions if d.action == "open")
    blocked = sum(1 for d in decisions if d.action == "blocked")
    hold = sum(1 for d in decisions if d.action == "hold")
    close = sum(1 for d in decisions if d.action == "close")
    selectable = sum(1 for d in decisions if d.selectable)
    exec_compat = sum(1 for d in decisions if d.execution_compatible)

    # --- 比率 ---
    selectable_ratio = selectable / total if total > 0 else 0.0
    exec_compat_ratio = exec_compat / total if total > 0 else 0.0

    # --- 评分分布 ---
    long_scores = [d.long_score for d in decisions]
    short_scores = [d.short_score for d in decisions]

    # --- 边际分布 ---
    edges = [d.expected_net_edge_bps for d in decisions]
    positive_edges = [e for e in edges if e > 0]

    # --- blocking reasons 分析 ---
    reason_counter: Counter[str] = Counter()
    for d in decisions:
        for reason in d.blocking_reasons:
            reason_counter[reason] += 1
    top_blocking = [
        {"reason": r, "count": c}
        for r, c in reason_counter.most_common(top_n_blocking)
    ]

    # --- 状态分布 ---
    state_counter: Counter[str] = Counter()
    for d in decisions:
        state_counter[d.state] += 1

    # --- 动作分布 ---
    action_counter: Counter[str] = Counter()
    for d in decisions:
        action_counter[d.action] += 1

    return {
        "total_bars": total,
        "opening_count": opening,
        "blocked_count": blocked,
        "hold_count": hold,
        "close_count": close,
        "selectable_count": selectable,
        "execution_compatible_count": exec_compat,
        "selectable_ratio": round(selectable_ratio, 6),
        "execution_compatible_ratio": round(exec_compat_ratio, 6),
        # 评分
        "mean_long_score": _safe_mean(long_scores),
        "mean_short_score": _safe_mean(short_scores),
        "max_long_score": round(max(long_scores), 6) if long_scores else None,
        "max_short_score": round(max(short_scores), 6) if short_scores else None,
        # 边际
        "mean_expected_edge_bps": _safe_mean(edges),
        "median_expected_edge_bps": _safe_median(edges),
        "p25_expected_edge_bps": _safe_percentile(edges, 25),
        "p75_expected_edge_bps": _safe_percentile(edges, 75),
        "positive_edge_count": len(positive_edges),
        "positive_edge_ratio": round(len(positive_edges) / total, 6) if total > 0 else 0.0,
        "mean_positive_edge_bps": _safe_mean(positive_edges),
        # 结构分析
        "top_blocking_reasons": top_blocking,
        "state_distribution": dict(state_counter),
        "action_distribution": dict(action_counter),
        # 评分分布（用于报告中的直方图描述）
        "score_distribution": _compute_distribution_summary(
            [max(d.long_score, d.short_score) for d in decisions],
            label="dominant_score",
        ),
        "expected_edge_distribution": _compute_distribution_summary(
            edges, label="expected_edge_bps",
        ),
    }


def compare_diagnostics(
    diagnostics_list: list[dict[str, Any]],
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """比较多组诊断结果，生成对比摘要。

    用于 Parameter Scan Engine 的 comparison summary。
    """
    if labels is None:
        labels = [f"exp_{i}" for i in range(len(diagnostics_list))]

    comparison_rows: list[dict[str, Any]] = []
    for label, diag in zip(labels, diagnostics_list):
        comparison_rows.append({
            "label": label,
            "total_bars": diag.get("total_bars", 0),
            "opening_count": diag.get("opening_count", 0),
            "blocked_count": diag.get("blocked_count", 0),
            "selectable_ratio": diag.get("selectable_ratio", 0),
            "execution_compatible_ratio": diag.get("execution_compatible_ratio", 0),
            "mean_expected_edge_bps": diag.get("mean_expected_edge_bps"),
            "median_expected_edge_bps": diag.get("median_expected_edge_bps"),
            "positive_edge_ratio": diag.get("positive_edge_ratio", 0),
            "top_blocking_reason": (
                diag["top_blocking_reasons"][0]["reason"]
                if diag.get("top_blocking_reasons") else "none"
            ),
        })

    return {
        "experiment_count": len(diagnostics_list),
        "comparison": comparison_rows,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_diagnostics() -> dict[str, Any]:
    return {
        "total_bars": 0,
        "opening_count": 0,
        "blocked_count": 0,
        "hold_count": 0,
        "close_count": 0,
        "selectable_count": 0,
        "execution_compatible_count": 0,
        "selectable_ratio": 0.0,
        "execution_compatible_ratio": 0.0,
        "mean_long_score": None,
        "mean_short_score": None,
        "max_long_score": None,
        "max_short_score": None,
        "mean_expected_edge_bps": None,
        "median_expected_edge_bps": None,
        "p25_expected_edge_bps": None,
        "p75_expected_edge_bps": None,
        "positive_edge_count": 0,
        "positive_edge_ratio": 0.0,
        "mean_positive_edge_bps": None,
        "top_blocking_reasons": [],
        "state_distribution": {},
        "action_distribution": {},
        "score_distribution": {},
        "expected_edge_distribution": {},
    }


def _safe_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.mean(values), 6)


def _safe_median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.median(values), 6)


def _safe_percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    sorted_v = sorted(values)
    idx = int(len(sorted_v) * pct / 100)
    idx = max(0, min(idx, len(sorted_v) - 1))
    return round(sorted_v[idx], 6)


def _compute_distribution_summary(
    values: list[float],
    *,
    label: str,
    bins: int = 5,
) -> dict[str, Any]:
    """计算分布统计摘要。"""
    if not values:
        return {"label": label, "count": 0}

    sorted_v = sorted(values)
    n = len(sorted_v)
    return {
        "label": label,
        "count": n,
        "min": round(sorted_v[0], 6),
        "p25": round(sorted_v[int(n * 0.25)], 6),
        "median": round(sorted_v[int(n * 0.50)], 6),
        "p75": round(sorted_v[int(n * 0.75)], 6),
        "max": round(sorted_v[-1], 6),
        "mean": round(statistics.mean(values), 6),
        "std": round(statistics.stdev(values), 6) if n > 1 else 0.0,
    }
