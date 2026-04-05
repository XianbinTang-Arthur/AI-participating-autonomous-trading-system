"""Phase 6-D: Promotion Readiness 评估.

回答核心问题：当前是否建议把某个 parameter set 进入下一轮 live test？
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── 就绪状态 ─────────────────────────────────────────────────────────

READINESS_STATUSES = (
    "ready_for_next_live_test",
    "not_ready_more_research_needed",
    "not_ready_attribution_issue",
    "not_ready_execution_issue",
    "not_ready_governance_issue",
)


# ── 核心判断逻辑 ─────────────────────────────────────────────────────


def evaluate_promotion_readiness(
    evidence_bundle: dict[str, Any],
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """评估当前证据是否足以支持进入下一轮 live test.

    Parameters
    ----------
    evidence_bundle : dict
        完整 evidence bundle
    upgrade_candidates : list
        来自 candidate_selector 的评估结果
    ft_decisions : list
        来自 decision_engine 的 family/timeframe 决策

    Returns
    -------
    dict  promotion readiness report
    """
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    # ── Check 1: 研究结果是否有稳定提升 ──
    p2 = evidence_bundle.get("phase2_evidence", {})
    p2_agg = p2.get("aggregate_stats", {})
    exp_with_openings = p2_agg.get("experiments_with_openings", 0)
    mean_edge = p2_agg.get("mean_positive_edge_ratio", 0)

    research_ok = exp_with_openings >= 1 and mean_edge >= 0.15
    checks.append({
        "check": "research_stability",
        "passed": research_ok,
        "detail": f"实验有开仓={exp_with_openings >= 1}, edge_ratio={mean_edge:.3f}",
    })
    if not research_ok:
        blockers.append("研究结果不稳定: 需更多实验或改善 edge")

    # ── Check 2: Attribution 是否无严重结构问题 ──
    p3 = evidence_bundle.get("phase3_evidence", {})
    p3_round_count = p3.get("round_count", 0)

    if p3_round_count > 0:
        p3_latest = p3.get("latest_round", {})
        # 优先从 combos 推导状态（round manifest 可能无顶层 status 字段）
        p3_combos = p3_latest.get("combos", {})
        if p3_combos:
            combo_statuses = [c.get("status", "unknown") for c in p3_combos.values()]
            attribution_ok = any(
                s in ("succeeded", "partial_success") for s in combo_statuses
            )
            p3_status = (
                "succeeded" if all(s == "succeeded" for s in combo_statuses)
                else "partial_success" if attribution_ok
                else "failed"
            )
        else:
            p3_status = p3_latest.get(
                "overall_status", p3_latest.get("status", "unknown"),
            )
            attribution_ok = p3_status in ("succeeded", "partial_success")
        checks.append({
            "check": "attribution_no_severe_issue",
            "passed": attribution_ok,
            "detail": f"最近 Phase 3 round status={p3_status}",
        })
        if not attribution_ok:
            blockers.append(f"归因显示严重问题: 最近 round status={p3_status}")
    else:
        checks.append({
            "check": "attribution_no_severe_issue",
            "passed": True,  # 无数据视为中性（不阻塞）
            "detail": "无 Phase 3 数据（跳过）",
        })

    # ── Check 3: Execution realism 是否未显著吞噬 edge ──
    p4 = evidence_bundle.get("phase4_evidence", {})
    p4_round_count = p4.get("round_count", 0)

    if p4_round_count > 0:
        p4_latest = p4.get("latest_round", {})
        # 检查是否有任何 combo 的 cost_adjusted_edge 严重为负
        severe_exec = False
        for combo_key, combo_data in p4_latest.get("combos", {}).items():
            cost = combo_data.get("cost_summary", {})
            adj_edge = cost.get("cost_adjusted_edge_mean", 0)
            if adj_edge < -5.0:
                severe_exec = True
                break

        execution_ok = not severe_exec
        checks.append({
            "check": "execution_not_severe",
            "passed": execution_ok,
            "detail": f"Phase 4 有 {p4_round_count} round, 严重负面={'是' if severe_exec else '否'}",
        })
        if not execution_ok:
            blockers.append("执行 realism 显示严重成本问题")
    else:
        checks.append({
            "check": "execution_not_severe",
            "passed": True,
            "detail": "无 Phase 4 数据（跳过）",
        })

    # ── Check 4: Governance 是否健康 ──
    p5 = evidence_bundle.get("phase5_governance_evidence", {})
    health = p5.get("quality_health")
    governance_ok = health in ("healthy", "degraded")
    checks.append({
        "check": "governance_healthy",
        "passed": governance_ok,
        "detail": f"治理层 health={health or 'unknown'}",
    })
    if not governance_ok:
        blockers.append(f"治理层不健康: health={health}")

    # ── Check 5: 推荐参数是否足够新鲜且可追溯 ──
    has_frozen = len(p5.get("frozen_parameter_sets", [])) > 0
    has_candidate = len(p5.get("candidate_parameter_sets", [])) > 0
    parameter_ok = has_frozen or has_candidate
    checks.append({
        "check": "parameter_traceable",
        "passed": parameter_ok,
        "detail": f"frozen={len(p5.get('frozen_parameter_sets', []))}, candidate={len(p5.get('candidate_parameter_sets', []))}",
    })
    if not parameter_ok:
        blockers.append("无可追溯的参数集 (无 frozen 或 candidate)")

    # ── Check 6: 至少有一个 promote_candidate 的参数 ──
    promoted = [c for c in upgrade_candidates if c.get("decision") == "promote_candidate"]
    has_promoted = len(promoted) > 0
    checks.append({
        "check": "has_promote_candidate",
        "passed": has_promoted,
        "detail": f"promote_candidate 数量: {len(promoted)}",
    })
    if not has_promoted:
        blockers.append("无参数集达到 promote_candidate 标准")

    # ── Check 7: 至少有一个 keep_active 的 family/timeframe ──
    active_fts = [d for d in ft_decisions if d.get("decision") == "keep_active"]
    has_active = len(active_fts) > 0
    checks.append({
        "check": "has_keep_active_ft",
        "passed": has_active,
        "detail": f"keep_active 的 family/timeframe 数量: {len(active_fts)}",
    })
    if not has_active:
        blockers.append("无 family/timeframe 被建议 keep_active")

    # ── 综合判断 ──
    all_passed = all(c["passed"] for c in checks)
    critical_passed = all(
        c["passed"] for c in checks
        if c["check"] in ("research_stability", "governance_healthy", "has_promote_candidate")
    )

    if all_passed:
        readiness = "ready_for_next_live_test"
        overall_confidence = "high"
    elif critical_passed:
        readiness = "ready_for_next_live_test"
        overall_confidence = "medium"
    else:
        # 确定具体 not_ready 原因
        if not checks[0]["passed"]:
            readiness = "not_ready_more_research_needed"
        elif not checks[1]["passed"]:
            readiness = "not_ready_attribution_issue"
        elif not checks[2]["passed"]:
            readiness = "not_ready_execution_issue"
        elif not checks[3]["passed"]:
            readiness = "not_ready_governance_issue"
        else:
            readiness = "not_ready_more_research_needed"
        overall_confidence = "medium" if len(blockers) <= 2 else "high"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "readiness": readiness,
        "overall_confidence": overall_confidence,
        "checks_total": len(checks),
        "checks_passed": sum(1 for c in checks if c["passed"]),
        "checks_failed": sum(1 for c in checks if not c["passed"]),
        "blockers": blockers,
        "checks": checks,
        "promoted_candidates": [
            {"parameter_set_id": p["parameter_set_id"], "score_ratio": p["score_ratio"]}
            for p in promoted
        ],
        "active_family_timeframes": [
            {"combo_key": d["combo_key"], "confidence": d["confidence"]}
            for d in active_fts
        ],
    }
