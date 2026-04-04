"""Phase 6-C: Family/Timeframe 状态决策引擎.

规则化、可解释的决策引擎，为每个 family/timeframe 组合
输出 operational status 建议。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ── 状态定义 ─────────────────────────────────────────────────────────

OPERATIONAL_STATUSES = (
    "keep_active",
    "lower_priority",
    "pause",
    "require_review",
)

# ── 决策规则配置 ─────────────────────────────────────────────────────

# Phase 2 阈值
RULE_MIN_EXPERIMENTS_WITH_OPENINGS = 1
RULE_MIN_POSITIVE_EDGE_RATIO = 0.15

# Phase 3 阈值
RULE_MAX_FAILURE_RATIO = 0.7          # failure 比例 > 70% 则考虑 pause
RULE_STRATEGY_BLOCKED_RATIO = 0.5     # strategy blocked > 50% 则降权

# Phase 4 阈值
RULE_SEVERE_EXECUTION_COST = -5.0     # cost-adjusted edge < -5 bps 则严重
RULE_MIN_FILL_RATIO = 0.2             # fill ratio < 20% 则降权

# Phase 5 阈值
RULE_REQUIRE_HEALTHY_GOVERNANCE = True


# ── 决策逻辑 ─────────────────────────────────────────────────────────


def decide_family_timeframe_status(
    family: str,
    timeframe: str,
    evidence_bundle: dict[str, Any],
    *,
    upgrade_evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为单个 family/timeframe 生成状态建议.

    Parameters
    ----------
    family : str
    timeframe : str
    evidence_bundle : dict
    upgrade_evaluation : dict | None
        来自 candidate_selector 的评估结果（如有）

    Returns
    -------
    dict  包含 decision / confidence / reasons / signals
    """
    combo_key = f"{family}_{timeframe.lower()}"
    signals: list[dict[str, Any]] = []
    reasons: list[str] = []

    # ── Phase 2 信号 ──
    p2 = evidence_bundle.get("phase2_evidence", {})
    p2_agg = p2.get("aggregate_stats", {})

    exp_with_openings = p2_agg.get("experiments_with_openings", 0)
    mean_edge_ratio = p2_agg.get("mean_positive_edge_ratio", 0)

    if exp_with_openings >= RULE_MIN_EXPERIMENTS_WITH_OPENINGS:
        signals.append({"source": "phase2", "signal": "positive", "detail": f"有 {exp_with_openings} 个实验产生开仓"})
    else:
        signals.append({"source": "phase2", "signal": "negative", "detail": "无实验产生开仓信号"})

    if mean_edge_ratio >= RULE_MIN_POSITIVE_EDGE_RATIO:
        signals.append({"source": "phase2", "signal": "positive", "detail": f"edge_ratio={mean_edge_ratio:.3f}"})
    else:
        signals.append({"source": "phase2", "signal": "negative", "detail": f"edge_ratio 不足 ({mean_edge_ratio:.3f})"})

    # ── Phase 3 信号 ──
    p3 = evidence_bundle.get("phase3_evidence", {})
    p3_latest = p3.get("latest_round")

    if p3_latest:
        combo_data = p3_latest.get("combos", {}).get(combo_key, {})
        combo_status = combo_data.get("status", "unknown")

        if combo_status in ("succeeded", "partial_success"):
            signals.append({"source": "phase3", "signal": "positive", "detail": f"归因 {combo_key}={combo_status}"})
        elif combo_status == "failed":
            signals.append({"source": "phase3", "signal": "negative", "detail": f"归因 {combo_key} 失败"})
            reasons.append("Phase 3 归因失败")
        else:
            signals.append({"source": "phase3", "signal": "neutral", "detail": f"归因 {combo_key}={combo_status}"})

        # failure modes 分析
        tfm = combo_data.get("top_failure_modes", {})
        if tfm:
            total_f = tfm.get("total_failures", 0)
            total_s = tfm.get("total_success", 0)
            total = total_f + total_s
            if total > 0:
                failure_ratio = total_f / total
                if failure_ratio > RULE_MAX_FAILURE_RATIO:
                    signals.append({"source": "phase3", "signal": "severe_negative", "detail": f"failure 比例 {failure_ratio:.0%} 极高"})
                    reasons.append(f"归因 failure 比例 {failure_ratio:.0%} 超过 {RULE_MAX_FAILURE_RATIO:.0%}")
    else:
        signals.append({"source": "phase3", "signal": "absent", "detail": "无 Phase 3 数据"})

    # ── Phase 4 信号 ──
    p4 = evidence_bundle.get("phase4_evidence", {})
    p4_latest = p4.get("latest_round")

    if p4_latest:
        combo_data = p4_latest.get("combos", {}).get(combo_key, {})
        cost = combo_data.get("cost_summary", {})

        if cost:
            adj_edge = cost.get("cost_adjusted_edge_mean", 0)
            ffr = cost.get("full_fill_ratio", 0)

            if adj_edge < RULE_SEVERE_EXECUTION_COST:
                signals.append({"source": "phase4", "signal": "severe_negative", "detail": f"cost_adj_edge={adj_edge:.1f}bps 严重负面"})
                reasons.append(f"执行成本严重吞噬 edge ({adj_edge:.1f}bps)")
            elif adj_edge >= 0:
                signals.append({"source": "phase4", "signal": "positive", "detail": f"cost_adj_edge={adj_edge:.1f}bps"})
            else:
                signals.append({"source": "phase4", "signal": "negative", "detail": f"cost_adj_edge={adj_edge:.1f}bps 为负"})

            if ffr < RULE_MIN_FILL_RATIO:
                signals.append({"source": "phase4", "signal": "negative", "detail": f"fill_ratio={ffr:.1%} 不足"})
            else:
                signals.append({"source": "phase4", "signal": "positive", "detail": f"fill_ratio={ffr:.1%}"})
        else:
            signals.append({"source": "phase4", "signal": "absent", "detail": f"Phase 4 无 {combo_key} cost 数据"})
    else:
        signals.append({"source": "phase4", "signal": "absent", "detail": "无 Phase 4 数据"})

    # ── Phase 5 信号 ──
    p5 = evidence_bundle.get("phase5_governance_evidence", {})
    health = p5.get("quality_health")

    if health == "healthy":
        signals.append({"source": "phase5", "signal": "positive", "detail": "治理层 healthy"})
    elif health == "degraded":
        signals.append({"source": "phase5", "signal": "neutral", "detail": "治理层 degraded"})
    elif health == "unhealthy":
        signals.append({"source": "phase5", "signal": "negative", "detail": "治理层 unhealthy"})
        reasons.append("治理层 unhealthy")
    else:
        signals.append({"source": "phase5", "signal": "absent", "detail": f"治理层状态 {health or 'unknown'}"})

    # ── 综合决策 ──
    severe_count = sum(1 for s in signals if s["signal"] == "severe_negative")
    negative_count = sum(1 for s in signals if s["signal"] == "negative")
    positive_count = sum(1 for s in signals if s["signal"] == "positive")
    absent_count = sum(1 for s in signals if s["signal"] == "absent")

    if severe_count > 0:
        decision = "pause"
        confidence = "high"
        reasons.insert(0, "存在严重负面信号")
    elif absent_count >= 2:
        decision = "require_review"
        confidence = "low"
        reasons.insert(0, "多个维度缺少证据")
    elif negative_count > positive_count:
        decision = "lower_priority"
        confidence = "medium"
        reasons.insert(0, "负面信号多于正面")
    elif positive_count >= 3 and negative_count == 0:
        decision = "keep_active"
        confidence = "high"
        reasons.insert(0, "多维度正面信号且无负面")
    elif positive_count > negative_count:
        decision = "keep_active"
        confidence = "medium"
        reasons.insert(0, "正面信号多于负面")
    else:
        decision = "require_review"
        confidence = "low"
        reasons.insert(0, "信号混合，需人工审查")

    return {
        "family": family,
        "timeframe": timeframe,
        "combo_key": combo_key,
        "decision": decision,
        "confidence": confidence,
        "signal_summary": {
            "positive": positive_count,
            "negative": negative_count,
            "severe_negative": severe_count,
            "neutral": sum(1 for s in signals if s["signal"] == "neutral"),
            "absent": absent_count,
        },
        "signals": signals,
        "reasons": reasons,
    }


def decide_all_family_timeframes(
    evidence_bundle: dict[str, Any],
    *,
    combos: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """对所有 family/timeframe 组合做决策."""
    from .evidence_bundle import COMBOS as DEFAULT_COMBOS

    if combos is None:
        combos = DEFAULT_COMBOS

    results = []
    for combo in combos:
        result = decide_family_timeframe_status(
            combo["family"],
            combo["timeframe"],
            evidence_bundle,
        )
        results.append(result)

    return results
