"""Phase 6-C: family/timeframe 状态决策引擎。"""

from __future__ import annotations

import logging
from typing import Any

from .evidence_bundle import COMBOS, get_phase2_combo_stats, make_combo_key

log = logging.getLogger(__name__)

OPERATIONAL_STATUSES = (
    "keep_active",
    "lower_priority",
    "pause",
    "require_review",
)

RULE_MIN_EXPERIMENTS_WITH_OPENINGS = 1
RULE_MIN_POSITIVE_EDGE_RATIO = 0.15
RULE_MAX_FAILURE_RATIO = 0.85
RULE_SEVERE_EXECUTION_COST = -5.0
RULE_MIN_FILL_RATIO = 0.2


def decide_family_timeframe_status(
    family: str,
    timeframe: str,
    evidence_bundle: dict[str, Any],
    *,
    upgrade_evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为单个 family/timeframe 生成运行状态建议。"""
    del upgrade_evaluation  # 当前决策只依赖 evidence bundle。

    combo_key = make_combo_key(family, timeframe) or f"{family}_{timeframe.lower()}"
    signals: list[dict[str, Any]] = []
    reasons: list[str] = []

    p2 = evidence_bundle.get("phase2_evidence", {})
    p2_stats = get_phase2_combo_stats(p2, family, timeframe)
    if p2_stats.get("available"):
        exp_with_openings = p2_stats.get("experiments_with_openings", 0)
        mean_edge_ratio = p2_stats.get("mean_positive_edge_ratio", 0)
        if exp_with_openings >= RULE_MIN_EXPERIMENTS_WITH_OPENINGS:
            signals.append({
                "source": "phase2",
                "signal": "positive",
                "detail": f"{combo_key} 开仓实验数={exp_with_openings}",
            })
        else:
            signals.append({
                "source": "phase2",
                "signal": "negative",
                "detail": f"{combo_key} 没有实验产生开仓信号",
            })

        if mean_edge_ratio >= RULE_MIN_POSITIVE_EDGE_RATIO:
            signals.append({
                "source": "phase2",
                "signal": "positive",
                "detail": f"{combo_key} edge_ratio={mean_edge_ratio:.3f}",
            })
        else:
            signals.append({
                "source": "phase2",
                "signal": "negative",
                "detail": (
                    f"{combo_key} edge_ratio 不足 "
                    f"({mean_edge_ratio:.3f} < {RULE_MIN_POSITIVE_EDGE_RATIO})"
                ),
            })
    else:
        signals.append({
            "source": "phase2",
            "signal": "absent",
            "detail": f"{combo_key} 缺少 Phase 2 证据",
        })

    p3 = evidence_bundle.get("phase3_evidence", {})
    p3_latest = p3.get("latest_round")
    if p3_latest:
        replay_only_round = bool(p3_latest.get("replay_only", False))
        combo_data = p3_latest.get("combos", {}).get(combo_key, {})
        combo_status = combo_data.get("status", "unknown")
        if combo_status in ("succeeded", "partial_success"):
            signals.append({
                "source": "phase3",
                "signal": "positive",
                "detail": f"归因 {combo_key}={combo_status}",
            })
        elif combo_status == "failed":
            signals.append({
                "source": "phase3",
                "signal": "negative",
                "detail": f"归因 {combo_key} 失败",
            })
            reasons.append("Phase 3 归因失败")
        else:
            signals.append({
                "source": "phase3",
                "signal": "neutral",
                "detail": f"归因 {combo_key}={combo_status}",
            })

        tfm = combo_data.get("top_failure_modes", {})
        if tfm and not replay_only_round:
            total_failures = tfm.get("total_failures", 0)
            total_success = tfm.get("total_success", 0)
            total = total_failures + total_success
            if total > 0:
                failure_ratio = total_failures / total
                if failure_ratio > RULE_MAX_FAILURE_RATIO:
                    signals.append({
                        "source": "phase3",
                        "signal": "severe_negative",
                        "detail": f"failure_ratio={failure_ratio:.0%}",
                    })
                    reasons.append(
                        f"归因 failure 比例 {failure_ratio:.0%} 超过 {RULE_MAX_FAILURE_RATIO:.0%}",
                    )
        elif tfm and replay_only_round:
            signals.append({
                "source": "phase3",
                "signal": "neutral",
                "detail": "replay_only attribution，跳过 failure_ratio 风险判定",
            })
    else:
        signals.append({
            "source": "phase3",
            "signal": "absent",
            "detail": "无 Phase 3 数据",
        })

    p4 = evidence_bundle.get("phase4_evidence", {})
    p4_latest = p4.get("latest_round")
    if p4_latest:
        combo_data = p4_latest.get("combos", {}).get(combo_key, {})
        cost = combo_data.get("cost_summary", {})
        if cost:
            adj_edge = cost.get("cost_adjusted_edge_mean", 0)
            fill_ratio = cost.get("full_fill_ratio", 0)
            if adj_edge < RULE_SEVERE_EXECUTION_COST:
                signals.append({
                    "source": "phase4",
                    "signal": "severe_negative",
                    "detail": f"cost_adj_edge={adj_edge:.1f}bps",
                })
                reasons.append(f"执行成本严重吞噬 edge ({adj_edge:.1f}bps)")
            elif adj_edge >= 0:
                signals.append({
                    "source": "phase4",
                    "signal": "positive",
                    "detail": f"cost_adj_edge={adj_edge:.1f}bps",
                })
            else:
                signals.append({
                    "source": "phase4",
                    "signal": "negative",
                    "detail": f"cost_adj_edge={adj_edge:.1f}bps",
                })

            if fill_ratio < RULE_MIN_FILL_RATIO:
                signals.append({
                    "source": "phase4",
                    "signal": "negative",
                    "detail": f"fill_ratio={fill_ratio:.1%}",
                })
            else:
                signals.append({
                    "source": "phase4",
                    "signal": "positive",
                    "detail": f"fill_ratio={fill_ratio:.1%}",
                })
        else:
            signals.append({
                "source": "phase4",
                "signal": "absent",
                "detail": f"Phase 4 缺少 {combo_key} cost 数据",
            })
    else:
        signals.append({
            "source": "phase4",
            "signal": "absent",
            "detail": "无 Phase 4 数据",
        })

    p5 = evidence_bundle.get("phase5_governance_evidence", {})
    health = p5.get("quality_health")
    if health == "healthy":
        signals.append({
            "source": "phase5",
            "signal": "positive",
            "detail": "治理层 healthy",
        })
    elif health == "degraded":
        signals.append({
            "source": "phase5",
            "signal": "neutral",
            "detail": "治理层 degraded",
        })
    elif health == "unhealthy":
        signals.append({
            "source": "phase5",
            "signal": "negative",
            "detail": "治理层 unhealthy",
        })
        reasons.append("治理层 unhealthy")
    else:
        signals.append({
            "source": "phase5",
            "signal": "absent",
            "detail": f"治理层状态={health or 'unknown'}",
        })

    severe_count = sum(1 for signal in signals if signal["signal"] == "severe_negative")
    negative_count = sum(1 for signal in signals if signal["signal"] == "negative")
    positive_count = sum(1 for signal in signals if signal["signal"] == "positive")
    absent_count = sum(1 for signal in signals if signal["signal"] == "absent")

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
        reasons.insert(0, "多维度正面且无负面")
    elif positive_count > negative_count:
        decision = "keep_active"
        confidence = "medium"
        reasons.insert(0, "正面信号多于负面")
    else:
        decision = "require_review"
        confidence = "low"
        reasons.insert(0, "信号混合，需要人工审查")

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
            "neutral": sum(1 for signal in signals if signal["signal"] == "neutral"),
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
    if combos is None:
        combos = COMBOS

    results = []
    for combo in combos:
        results.append(
            decide_family_timeframe_status(
                combo["family"],
                combo["timeframe"],
                evidence_bundle,
            ),
        )
    return results
