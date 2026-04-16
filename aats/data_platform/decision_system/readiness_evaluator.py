"""Phase 6-D: Promotion Readiness 评估。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .evidence_bundle import COMBOS, get_phase2_combo_stats

log = logging.getLogger(__name__)

READINESS_STATUSES = (
    "ready_for_next_live_test",
    "not_ready_more_research_needed",
    "not_ready_attribution_issue",
    "not_ready_execution_issue",
    "not_ready_governance_issue",
)


def evaluate_promotion_readiness(
    evidence_bundle: dict[str, Any],
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """评估当前证据是否足以进入下一轮 live test。"""
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    p2 = evidence_bundle.get("phase2_evidence", {})
    viable_combos: list[dict[str, Any]] = []
    for combo in COMBOS:
        stats = get_phase2_combo_stats(p2, combo["family"], combo["timeframe"])
        if not stats.get("available"):
            continue
        if (
            stats.get("experiments_with_openings", 0) >= 1
            and stats.get("mean_positive_edge_ratio", 0) >= 0.15
        ):
            viable_combos.append({
                "combo_key": combo["key"],
                "experiments_with_openings": stats.get("experiments_with_openings", 0),
                "mean_positive_edge_ratio": stats.get("mean_positive_edge_ratio", 0),
            })

    research_ok = len(viable_combos) >= 1
    if viable_combos:
        strongest_combo = sorted(
            viable_combos,
            key=lambda item: (
                item["experiments_with_openings"],
                item["mean_positive_edge_ratio"],
            ),
            reverse=True,
        )[0]
        research_detail = (
            f"可用 combo={len(viable_combos)}, best={strongest_combo['combo_key']}, "
            f"opens={strongest_combo['experiments_with_openings']}, "
            f"edge_ratio={strongest_combo['mean_positive_edge_ratio']:.3f}"
        )
    else:
        research_detail = "没有任何 combo 同时满足开仓与 edge 稳定性阈值"
    checks.append({
        "check": "research_stability",
        "passed": research_ok,
        "detail": research_detail,
    })
    if not research_ok:
        blockers.append("研究结果不稳定，缺少可交易 combo")

    p3 = evidence_bundle.get("phase3_evidence", {})
    p3_round_count = p3.get("round_count", 0)
    if p3_round_count > 0:
        p3_latest = p3.get("latest_round", {})
        p3_combos = p3_latest.get("combos", {})
        if p3_combos:
            combo_statuses = [combo.get("status", "unknown") for combo in p3_combos.values()]
            attribution_ok = any(status in ("succeeded", "partial_success") for status in combo_statuses)
            p3_status = (
                "succeeded" if all(status == "succeeded" for status in combo_statuses)
                else "partial_success" if attribution_ok
                else "failed"
            )
        else:
            p3_status = p3_latest.get("overall_status", p3_latest.get("status", "unknown"))
            attribution_ok = p3_status in ("succeeded", "partial_success")
        checks.append({
            "check": "attribution_no_severe_issue",
            "passed": attribution_ok,
            "detail": f"latest_phase3_status={p3_status}",
        })
        if not attribution_ok:
            blockers.append(f"归因显示严重问题: latest_phase3_status={p3_status}")
    else:
        checks.append({
            "check": "attribution_no_severe_issue",
            "passed": True,
            "detail": "无 Phase 3 数据，跳过",
        })

    p4 = evidence_bundle.get("phase4_evidence", {})
    p4_round_count = p4.get("round_count", 0)
    if p4_round_count > 0:
        p4_latest = p4.get("latest_round", {})
        severe_exec = False
        for combo_data in p4_latest.get("combos", {}).values():
            adj_edge = combo_data.get("cost_summary", {}).get("cost_adjusted_edge_mean", 0)
            if adj_edge < -5.0:
                severe_exec = True
                break
        execution_ok = not severe_exec
        checks.append({
            "check": "execution_not_severe",
            "passed": execution_ok,
            "detail": f"phase4_rounds={p4_round_count}, severe_negative={'yes' if severe_exec else 'no'}",
        })
        if not execution_ok:
            blockers.append("执行 realism 显示严重成本问题")
    else:
        checks.append({
            "check": "execution_not_severe",
            "passed": True,
            "detail": "无 Phase 4 数据，跳过",
        })

    p5 = evidence_bundle.get("phase5_governance_evidence", {})
    health = p5.get("quality_health")
    governance_ok = health in ("healthy", "degraded")
    checks.append({
        "check": "governance_healthy",
        "passed": governance_ok,
        "detail": f"health={health or 'unknown'}",
    })
    if not governance_ok:
        blockers.append(f"治理层不健康: health={health}")

    has_frozen = len(p5.get("frozen_parameter_sets", [])) > 0
    has_candidate = len(p5.get("candidate_parameter_sets", [])) > 0
    parameter_ok = has_frozen or has_candidate
    checks.append({
        "check": "parameter_traceable",
        "passed": parameter_ok,
        "detail": (
            f"frozen={len(p5.get('frozen_parameter_sets', []))}, "
            f"candidate={len(p5.get('candidate_parameter_sets', []))}"
        ),
    })
    if not parameter_ok:
        blockers.append("无可追溯参数集")

    promoted = [candidate for candidate in upgrade_candidates if candidate.get("decision") == "promote_candidate"]
    has_promoted = len(promoted) > 0
    checks.append({
        "check": "has_promote_candidate",
        "passed": has_promoted,
        "detail": f"promote_candidate_count={len(promoted)}",
    })
    if not has_promoted:
        blockers.append("无参数集达到 promote_candidate 标准")

    active_fts = [decision for decision in ft_decisions if decision.get("decision") == "keep_active"]
    has_active = len(active_fts) > 0
    checks.append({
        "check": "has_keep_active_ft",
        "passed": has_active,
        "detail": f"keep_active_count={len(active_fts)}",
    })
    if not has_active:
        blockers.append("无 family/timeframe 被建议 keep_active")

    all_passed = all(check["passed"] for check in checks)
    critical_passed = all(
        check["passed"] for check in checks
        if check["check"] in {"research_stability", "governance_healthy", "has_promote_candidate"}
    )

    if all_passed:
        readiness = "ready_for_next_live_test"
        overall_confidence = "high"
    elif critical_passed:
        readiness = "ready_for_next_live_test"
        overall_confidence = "medium"
    else:
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
        "checks_passed": sum(1 for check in checks if check["passed"]),
        "checks_failed": sum(1 for check in checks if not check["passed"]),
        "blockers": blockers,
        "checks": checks,
        "promoted_candidates": [
            {"parameter_set_id": item["parameter_set_id"], "score_ratio": item["score_ratio"]}
            for item in promoted
        ],
        "active_family_timeframes": [
            {"combo_key": item["combo_key"], "confidence": item["confidence"]}
            for item in active_fts
        ],
    }
