"""Phase 6-B: 参数升级候选筛选.

从 parameter registry 和 evidence bundle 中筛选最值得
推荐进入下一轮 live test 的参数集。

规则化、可解释的评分引擎。
"""

from __future__ import annotations

import logging
from typing import Any

from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)

from .evidence_bundle import get_phase2_combo_stats, make_combo_key
from .promotion_policy import (
    P2_MIN_OPENING_COUNT,
    P2_MIN_POSITIVE_EDGE_RATIO,
    phase2_combo_meets_promotion_gate,
)

log = logging.getLogger(__name__)

# ── 决策类型 ─────────────────────────────────────────────────────────

DECISIONS = ("promote_candidate", "hold", "reject")
CONFIDENCE_LEVELS = ("high", "medium", "low", "insufficient")


# ── 评分维度 ─────────────────────────────────────────────────────────

# Phase 3 归因维度
P3_STRATEGY_BLOCKED_THRESHOLD = 0.5  # strategy blocked 占比 > 50% 则降权

# Phase 4 执行维度
P4_MIN_COST_ADJUSTED_EDGE = 0.0      # cost-adjusted edge >= 0
P4_MIN_FULL_FILL_RATIO = 0.3         # 完全可成交比例 >= 30%

# Phase 5 治理维度
P5_REQUIRE_HEALTHY_GOVERNANCE = True


# ── 候选评估 ─────────────────────────────────────────────────────────


def _evaluate_phase2_score(
    evidence: dict[str, Any],
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    """Phase 2 维度评分."""
    result = {
        "dimension": "phase2_research",
        "score": 0.0,
        "max_score": 2.0,
        "promotion_evidence_qualified": False,
        "details": [],
    }

    combo_key = make_combo_key(family, timeframe) or f"{family}_{timeframe}"
    agg = get_phase2_combo_stats(evidence, family, timeframe)
    if not agg.get("available"):
        fallback_reason = agg.get("fallback_reason")
        reason_suffix = f" ({fallback_reason})" if fallback_reason else ""
        result["details"].append(
            f"{combo_key} 缺少 Phase 2 有效证据{reason_suffix}"
        )
        return result
    experiments_with_openings = agg.get("experiments_with_openings", 0)
    mean_edge_ratio = agg.get("mean_positive_edge_ratio", 0)
    max_opening = agg.get("max_opening_count", 0)

    # 开仓是一个 hard-gate 维度；实验数量与单实验最大开仓数不重复计分。
    if (
        type(experiments_with_openings) is int
        and experiments_with_openings >= P2_MIN_OPENING_COUNT
        and type(max_opening) is int
        and max_opening >= P2_MIN_OPENING_COUNT
    ):
        result["score"] += 1.0
        result["details"].append(
            f"{combo_key} 有 {experiments_with_openings} 个实验产生开仓信号，"
            f"最大 opening_count={max_opening}"
        )
    else:
        result["details"].append(
            f"{combo_key} opening 证据不足 "
            f"(experiments={experiments_with_openings}, max={max_opening})"
        )

    # 正 edge 比例
    if mean_edge_ratio >= P2_MIN_POSITIVE_EDGE_RATIO:
        result["score"] += 1.0
        result["details"].append(
            f"平均 positive_edge_ratio={mean_edge_ratio:.3f} >= {P2_MIN_POSITIVE_EDGE_RATIO}"
        )
    else:
        result["details"].append(
            f"{combo_key} positive_edge_ratio 不足 "
            f"({mean_edge_ratio:.3f} < {P2_MIN_POSITIVE_EDGE_RATIO})"
        )

    result["promotion_evidence_qualified"] = (
        phase2_combo_meets_promotion_gate(agg)
    )

    return result


def _evaluate_phase3_score(
    evidence: dict[str, Any],
    family: str,
    timeframe: str,
    *,
    expected_source_round_id: str | None,
    expected_values_fingerprint: str,
) -> dict[str, Any]:
    """Phase 3 维度评分."""
    result = {
        "dimension": "phase3_attribution",
        "score": 0.0,
        "max_score": 2.0,
        "details": [],
        "parameter_identity_bound": False,
    }

    latest = evidence.get("latest_round")
    if not latest:
        result["details"].append("无 Phase 3 round 数据")
        return result

    combo_key = f"{family}_{timeframe.lower()}"
    combo = latest.get("combos", {}).get(combo_key, {})

    if not combo:
        result["details"].append(f"Phase 3 无 {combo_key} 数据")
        return result

    if (
        not expected_source_round_id
        or combo.get("source_step3_round_id") != expected_source_round_id
        or combo.get("parameter_values_fingerprint")
        != expected_values_fingerprint
    ):
        result["details"].append(
            f"Phase 3 {combo_key} 参数身份与候选不一致"
        )
        return result
    result["parameter_identity_bound"] = True

    status = combo.get("status", "unknown")
    if status in ("succeeded", "partial_success"):
        result["score"] += 1.0
        result["details"].append(f"Phase 3 {combo_key} status={status}")
    else:
        result["details"].append(f"Phase 3 {combo_key} 失败: status={status}")

    # 检查 failure modes
    tfm = combo.get("top_failure_modes", {})
    if tfm:
        total_failures = tfm.get("total_failures", 0)
        total_success = tfm.get("total_success", 0)
        total = total_failures + total_success
        if total > 0 and total_failures / total < P3_STRATEGY_BLOCKED_THRESHOLD:
            result["score"] += 1.0
            result["details"].append(
                f"failure 占比 {total_failures}/{total} < {P3_STRATEGY_BLOCKED_THRESHOLD:.0%}"
            )
        elif total > 0:
            result["details"].append(
                f"failure 占比过高: {total_failures}/{total}"
            )
    else:
        # 无 failure mode 数据，给一半分
        result["score"] += 0.5
        result["details"].append("无 failure mode 数据（视为中性）")

    return result


def _evaluate_phase4_score(
    evidence: dict[str, Any],
    family: str,
    timeframe: str,
    *,
    expected_source_round_id: str | None,
    expected_values_fingerprint: str,
) -> dict[str, Any]:
    """Phase 4 维度评分."""
    result = {
        "dimension": "phase4_execution",
        "score": 0.0,
        "max_score": 2.0,
        "details": [],
        "parameter_identity_bound": False,
    }

    latest = evidence.get("latest_round")
    if not latest:
        result["details"].append("无 Phase 4 round 数据")
        return result

    combo_key = f"{family}_{timeframe.lower()}"
    combo = latest.get("combos", {}).get(combo_key, {})
    if (
        not expected_source_round_id
        or combo.get("source_step3_round_id") != expected_source_round_id
        or combo.get("parameter_values_fingerprint")
        != expected_values_fingerprint
    ):
        result["details"].append(
            f"Phase 4 {combo_key} 参数身份与候选不一致"
        )
        return result
    result["parameter_identity_bound"] = True
    cost = combo.get("cost_summary", {})

    if not cost:
        result["details"].append(f"Phase 4 无 {combo_key} cost 数据")
        return result

    # cost-adjusted edge
    adj_edge = cost.get("cost_adjusted_edge_mean", 0)
    if adj_edge >= P4_MIN_COST_ADJUSTED_EDGE:
        result["score"] += 1.0
        result["details"].append(
            f"cost_adjusted_edge={adj_edge:.2f}bps >= {P4_MIN_COST_ADJUSTED_EDGE}"
        )
    else:
        result["details"].append(
            f"cost_adjusted_edge 不足: {adj_edge:.2f}bps"
        )

    # full fill ratio
    ffr = cost.get("full_fill_ratio", 0)
    if ffr >= P4_MIN_FULL_FILL_RATIO:
        result["score"] += 1.0
        result["details"].append(
            f"full_fill_ratio={ffr:.1%} >= {P4_MIN_FULL_FILL_RATIO:.0%}"
        )
    else:
        result["details"].append(
            f"full_fill_ratio 不足: {ffr:.1%}"
        )

    return result


def _evaluate_governance_score(
    evidence: dict[str, Any],
    parameter_set: dict[str, Any],
) -> dict[str, Any]:
    """Phase 5 治理维度评分."""
    result = {
        "dimension": "phase5_governance",
        "score": 0.0,
        "max_score": 2.0,
        "details": [],
    }

    # 治理层健康
    health = evidence.get("quality_health")
    if health == "healthy":
        result["score"] += 1.0
        result["details"].append("治理层 healthy")
    elif health == "degraded":
        result["score"] += 0.5
        result["details"].append("治理层 degraded")
    else:
        result["details"].append(f"治理层 {health or 'unknown'}")

    # 参数状态
    ps_status = parameter_set.get("status", "unknown")
    if ps_status in ("frozen", "candidate"):
        result["score"] += 1.0
        result["details"].append(f"参数状态: {ps_status}")
    elif ps_status == "draft":
        result["score"] += 0.5
        result["details"].append("参数仍为 draft 状态")
    else:
        result["details"].append(f"参数状态异常: {ps_status}")

    return result


# ── 综合评估 ─────────────────────────────────────────────────────────


def evaluate_parameter_set(
    parameter_set: dict[str, Any],
    evidence_bundle: dict[str, Any],
) -> dict[str, Any]:
    """对单个 parameter set 进行综合评估.

    Returns
    -------
    dict  包含 decision / confidence / scores / reason
    """
    family = parameter_set.get("family", "unknown")
    timeframe = parameter_set.get("timeframe", "unknown")
    ps_id = parameter_set.get("parameter_set_id", "unknown")
    values_fingerprint = parameter_values_fingerprint(parameter_set.get("values"))
    source_round_id = parameter_set.get("source_round_id")

    p2_score = _evaluate_phase2_score(
        evidence_bundle.get("phase2_evidence", {}), family, timeframe,
    )
    p3_score = _evaluate_phase3_score(
        evidence_bundle.get("phase3_evidence", {}),
        family,
        timeframe,
        expected_source_round_id=source_round_id,
        expected_values_fingerprint=values_fingerprint,
    )
    p4_score = _evaluate_phase4_score(
        evidence_bundle.get("phase4_evidence", {}),
        family,
        timeframe,
        expected_source_round_id=source_round_id,
        expected_values_fingerprint=values_fingerprint,
    )
    p5_score = _evaluate_governance_score(
        evidence_bundle.get("phase5_governance_evidence", {}),
        parameter_set,
    )

    scores = [p2_score, p3_score, p4_score, p5_score]
    total_score = sum(s["score"] for s in scores)
    max_score = sum(s["max_score"] for s in scores)
    ratio = total_score / max_score if max_score > 0 else 0

    # 决策逻辑
    phase2_promotion_qualified = bool(
        p2_score.get("promotion_evidence_qualified", False)
    )
    parameter_evidence_bound = bool(
        p3_score.get("parameter_identity_bound")
        and p4_score.get("parameter_identity_bound")
    )
    if not parameter_evidence_bound:
        decision = "hold"
        confidence = "insufficient"
    elif ratio >= 0.7 and phase2_promotion_qualified:
        decision = "promote_candidate"
        confidence = "high" if ratio >= 0.85 else "medium"
    elif ratio >= 0.4:
        decision = "hold"
        confidence = "medium" if ratio >= 0.55 else "low"
    else:
        decision = "reject"
        confidence = "low" if ratio >= 0.2 else "insufficient"

    # 构建 reason
    reasons = []
    for s in scores:
        for d in s["details"]:
            reasons.append(f"[{s['dimension']}] {d}")

    return {
        "parameter_set_id": ps_id,
        # Qualification and apply must prove the exact values evaluated in
        # this Phase 6 round, not merely reuse a mutable parameter-set ID.
        "parameter_values_fingerprint": values_fingerprint,
        # 保留参数集所属研究轮次，供 recommendation 写入
        # governance.recommendations.source_round_id。缺失该字段会让审批、
        # 发布与回滚链无法证明候选来自哪一轮研究。
        "source_round_id": parameter_set.get("source_round_id"),
        "family": family,
        "symbol": parameter_set.get("symbol", "BTC-USDT-SWAP"),
        "timeframe": timeframe,
        "decision": decision,
        "confidence": confidence,
        "total_score": round(total_score, 2),
        "max_score": round(max_score, 2),
        "score_ratio": round(ratio, 3),
        "dimension_scores": scores,
        "reason": "; ".join(reasons),
    }


def select_parameter_upgrade_candidates(
    parameter_sets: list[dict[str, Any]],
    evidence_bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    """批量评估所有 parameter sets.

    Returns
    -------
    list  按 score_ratio 降序排列的评估结果
    """
    results = []
    for ps in parameter_sets:
        evaluation = evaluate_parameter_set(ps, evidence_bundle)
        results.append(evaluation)

    decision_rank = {
        "promote_candidate": 3,
        "hold": 2,
        "reject": 1,
    }
    best_by_combo: dict[str, dict[str, Any]] = {}
    for result in results:
        combo_key = make_combo_key(result.get("family"), result.get("timeframe")) or result[
            "parameter_set_id"
        ]
        current = best_by_combo.get(combo_key)
        if current is None:
            best_by_combo[combo_key] = result
            continue
        current_key = (
            current["score_ratio"],
            current["total_score"],
            decision_rank.get(current["decision"], 0),
        )
        candidate_key = (
            result["score_ratio"],
            result["total_score"],
            decision_rank.get(result["decision"], 0),
        )
        if candidate_key > current_key:
            best_by_combo[combo_key] = result

    deduped = list(best_by_combo.values())
    deduped.sort(key=lambda r: (r["score_ratio"], r["total_score"]), reverse=True)
    return deduped
