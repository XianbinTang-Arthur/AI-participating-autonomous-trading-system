"""Phase 6-D: Promotion Readiness 评估。"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from .evidence_bundle import COMBOS, get_phase2_combo_stats, make_combo_key
from .promotion_policy import phase2_combo_meets_promotion_gate

log = logging.getLogger(__name__)

READINESS_STATUSES = (
    "ready_for_next_live_test",
    "not_ready_more_research_needed",
    "not_ready_attribution_issue",
    "not_ready_execution_issue",
    "not_ready_governance_issue",
)

_EXPECTED_COMBO_KEYS = frozenset(combo["key"] for combo in COMBOS)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _complete_succeeded_round(
    latest_round: dict[str, Any],
    *,
    phase: str,
) -> tuple[bool, str]:
    """Require the canonical four-combo topology and a fully succeeded round.

    ``status`` is the required canonical evidence projection.
    ``overall_status`` is checked only as the documented compatibility alias.
    If present it must agree, so a stale succeeded alias cannot hide a partial
    canonical status.
    """
    canonical_status = latest_round.get("status")
    compatibility_status = latest_round.get("overall_status")
    round_status = canonical_status or compatibility_status or "unknown"

    combos = latest_round.get("combos")
    if not isinstance(combos, dict):
        combos = {}
    actual_keys = set(combos)
    missing = sorted(_EXPECTED_COMBO_KEYS - actual_keys)
    unexpected = sorted(actual_keys - _EXPECTED_COMBO_KEYS)
    non_succeeded = sorted(
        f"{key}:{(combo if isinstance(combo, dict) else {}).get('status', 'unknown')}"
        for key, combo in combos.items()
        if key in _EXPECTED_COMBO_KEYS
        and (combo if isinstance(combo, dict) else {}).get("status") != "succeeded"
    )

    complete = (
        canonical_status == "succeeded"
        and compatibility_status in (None, "succeeded")
        and not missing
        and not unexpected
        and not non_succeeded
    )
    detail = (
        f"{phase}_round_status={round_status};"
        f"combo_count={len(actual_keys)};"
        f"missing={','.join(missing) or 'none'};"
        f"unexpected={','.join(unexpected) or 'none'};"
        f"non_succeeded={','.join(non_succeeded) or 'none'}"
    )
    return complete, detail


def _phase4_has_usable_cost_summary(latest_round: dict[str, Any]) -> bool:
    """Require every canonical Phase 4 combo to contain executed candidates.

    A present-but-empty summary or ``total_candidates=0`` cannot prove
    execution realism.  ``_complete_succeeded_round`` already validates the
    exact four-combo topology before this helper is called.
    """
    combos = latest_round.get("combos") or {}
    if not isinstance(combos, dict) or not combos:
        return False
    for combo_data in combos.values():
        cost_summary = _dict_or_empty(
            _dict_or_empty(combo_data).get("cost_summary")
        )
        total_candidates = cost_summary.get("total_candidates")
        adjusted_edge = cost_summary.get("cost_adjusted_edge_mean")
        if (
            type(total_candidates) is not int
            or total_candidates <= 0
            or type(adjusted_edge) not in {int, float}
            or not math.isfinite(adjusted_edge)
        ):
            return False
    return True


def evaluate_promotion_readiness(
    evidence_bundle: dict[str, Any],
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """评估当前证据是否足以进入下一轮 live test。

    会跑 7 个检查：research_stability / attribution_no_severe_issue /
    execution_not_severe / governance_healthy / parameter_traceable /
    has_promote_candidate / has_keep_active_ft。

    readiness 按如下规则分类：
    - **全部通过** → ``ready_for_next_live_test`` (confidence=high)
    - 否则按首个失败检查的位置归类到具体的 not_ready_* 状态；
      此时 confidence 表达"对 not_ready 判定的确信度"：
      blockers<=2 → medium（个别指标失败，可能是噪声），>2 → high（多指标
      同时失败，确信现在不宜上线）。

    Phase 3 / Phase 4 均不允许"无数据即跳过通过"：
    - Phase 3 无 round、``latest_round.replay_only=True``、live 查询未成功、零精确
      alignment、存在不可归因 live lineage，或 round 不是 ``succeeded``、不是精确
      四个 combo 且逐项 ``succeeded`` → ``attribution_no_severe_issue`` failed。
    - Phase 4 无 round、round 不是 ``succeeded``、不是精确四个 combo 且逐项
      ``succeeded``，或 latest round 所有 combo 都缺少可用 ``cost_summary``
      → ``execution_not_severe`` failed。

    不存在"关键子集通过即 medium ready"的中径：半成品 / replay-only 结果不能 promote。
    """
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    p2 = evidence_bundle.get("phase2_evidence", {})
    viable_combos: list[dict[str, Any]] = []
    for combo in COMBOS:
        stats = get_phase2_combo_stats(p2, combo["family"], combo["timeframe"])
        if not stats.get("available"):
            continue
        if phase2_combo_meets_promotion_gate(stats):
            viable_combos.append({
                "combo_key": combo["key"],
                "experiments_with_openings": stats.get("experiments_with_openings", 0),
                "mean_positive_edge_ratio": stats.get("mean_positive_edge_ratio", 0),
            })

    viable_combo_keys = {item["combo_key"] for item in viable_combos}
    promoted_combo_keys: set[str] = set()
    promoted_combo_invalid = False
    for candidate in upgrade_candidates:
        if candidate.get("decision") != "promote_candidate":
            continue
        combo_key = make_combo_key(
            candidate.get("family"),
            candidate.get("timeframe"),
        )
        if combo_key is None:
            promoted_combo_invalid = True
        else:
            promoted_combo_keys.add(combo_key)
    unqualified_promoted_combos = promoted_combo_keys - viable_combo_keys
    research_ok = bool(viable_combos) and not (
        promoted_combo_invalid or unqualified_promoted_combos
    )
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
        promotion_reason = p2.get("promotion_evidence_reason")
        research_detail = (
            str(promotion_reason)
            if isinstance(promotion_reason, str) and promotion_reason
            else "没有任何 combo 同时满足开仓与 edge 稳定性阈值"
        )
    if promoted_combo_invalid or unqualified_promoted_combos:
        invalid_targets = sorted(unqualified_promoted_combos)
        if promoted_combo_invalid:
            invalid_targets.append("invalid_combo_identity")
        research_detail += (
            "; promote_candidate 未通过目标 Phase 2 hard gate="
            + ",".join(invalid_targets)
        )
    checks.append({
        "check": "research_stability",
        "passed": research_ok,
        "detail": research_detail,
    })
    if not research_ok:
        blockers.append(f"研究结果不稳定，缺少可交易 combo: {research_detail}")

    p3 = evidence_bundle.get("phase3_evidence", {})
    p3_round_count = p3.get("round_count", 0)
    if p3_round_count <= 0:
        checks.append({
            "check": "attribution_no_severe_issue",
            "passed": False,
            "detail": "no_phase3_round_data",
        })
        blockers.append("缺少 Phase 3 attribution 证据，不能 promote")
    elif (
        isinstance(p3.get("latest_round"), dict)
        and p3["latest_round"].get("replay_only")
    ):
        checks.append({
            "check": "attribution_no_severe_issue",
            "passed": False,
            "detail": "phase3_latest_round_replay_only",
        })
        blockers.append("Phase 3 latest round 为 replay_only attribution，无法 promote")
    else:
        p3_latest_value = p3.get("latest_round")
        p3_latest = (
            p3_latest_value if isinstance(p3_latest_value, dict) else {}
        )
        p3_complete, p3_completeness_detail = _complete_succeeded_round(
            p3_latest,
            phase="phase3",
        )
        p3_combos = p3_latest.get("combos", {})
        if not isinstance(p3_combos, dict):
            p3_combos = {}
        aligned_total = sum(
            int(
                _dict_or_empty(
                    _dict_or_empty(combo).get("alignment_stats")
                ).get("aligned", 0)
                or 0
            )
            for combo in p3_combos.values()
        )
        unattributable_total = sum(
            int(
                _dict_or_empty(
                    _dict_or_empty(combo).get("alignment_stats")
                ).get("unattributable", 0)
                or 0
            )
            for combo in p3_combos.values()
        )
        live_query_succeeded = bool(p3_latest.get("live_query_succeeded", False))
        if not p3_complete:
            attribution_ok = False
            detail = p3_completeness_detail
            blocker = (
                "Phase 3 必须是完整 succeeded round，且精确包含四个 "
                f"succeeded combo: {p3_completeness_detail}"
            )
        elif not live_query_succeeded:
            attribution_ok = False
            p3_status = "live_query_failed_or_unproven"
            detail = p3_status
            blocker = "Phase 3 未证明 live DB 查询成功，无法进行实盘归因"
        elif aligned_total <= 0:
            attribution_ok = False
            p3_status = "zero_exact_alignment"
            detail = f"{p3_status};aligned={aligned_total}"
            blocker = "Phase 3 精确 replay/live 对齐样本为 0，无法 promote"
        elif unattributable_total > 0:
            attribution_ok = False
            p3_status = "unattributable_live_lineage"
            detail = f"{p3_status};count={unattributable_total};aligned={aligned_total}"
            blocker = (
                "Phase 3 存在缺失 lineage 的 live intent，禁止猜测性归因: "
                f"count={unattributable_total}"
            )
        else:
            attribution_ok = True
            detail = f"{p3_completeness_detail};aligned={aligned_total}"
            blocker = ""
        checks.append({
            "check": "attribution_no_severe_issue",
            "passed": attribution_ok,
            "detail": detail,
        })
        if not attribution_ok:
            blockers.append(blocker)

    p4 = evidence_bundle.get("phase4_evidence", {})
    p4_round_count = p4.get("round_count", 0)
    if p4_round_count <= 0:
        checks.append({
            "check": "execution_not_severe",
            "passed": False,
            "detail": "no_phase4_round_data",
        })
        blockers.append("缺少 Phase 4 execution realism 证据，不能 promote")
    else:
        p4_latest_value = p4.get("latest_round")
        p4_latest = (
            p4_latest_value if isinstance(p4_latest_value, dict) else {}
        )
        p4_complete, p4_completeness_detail = _complete_succeeded_round(
            p4_latest,
            phase="phase4",
        )
        if not p4_complete:
            checks.append({
                "check": "execution_not_severe",
                "passed": False,
                "detail": p4_completeness_detail,
            })
            blockers.append(
                "Phase 4 必须是完整 succeeded round，且精确包含四个 "
                f"succeeded combo: {p4_completeness_detail}"
            )
        elif not _phase4_has_usable_cost_summary(p4_latest):
            checks.append({
                "check": "execution_not_severe",
                "passed": False,
                "detail": (
                    f"phase4_rounds={p4_round_count}, "
                    "latest_round_has_no_usable_cost_summary"
                ),
            })
            blockers.append("Phase 4 latest round 无可用 combo cost_summary，无法验证执行成本")
        else:
            severe_exec = False
            for combo_data in p4_latest.get("combos", {}).values():
                cost_summary = _dict_or_empty(
                    _dict_or_empty(combo_data).get("cost_summary")
                )
                adj_edge = cost_summary.get("cost_adjusted_edge_mean")
                if adj_edge is not None and adj_edge < -5.0:
                    severe_exec = True
                    break
            execution_ok = not severe_exec
            checks.append({
                "check": "execution_not_severe",
                "passed": execution_ok,
                "detail": (
                    f"{p4_completeness_detail};phase4_rounds={p4_round_count};"
                    f"severe_negative={'yes' if severe_exec else 'no'}"
                ),
            })
            if not execution_ok:
                blockers.append("执行 realism 显示严重成本问题")

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

    if all_passed:
        readiness = "ready_for_next_live_test"
        overall_confidence = "high"
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
