"""Build automated strategy tuning reviews and review-required proposals."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aats.data_platform.decision_system.evidence_bundle import (
    COMBOS,
    collect_phase4_evidence,
    make_combo_key,
)
from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_STEP2,
    load_latest_research_round_snapshot,
)
from aats.data_platform.operations.strategy_tuning_registry import (
    record_generated_proposals,
)
from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
from aats.data_platform.replay.diagnostics.replay_diagnostics import (
    extract_comparison_rows,
)

log = logging.getLogger(__name__)

_OUTPUT_ROOT = Path("artifacts/strategy_tuning_reviews")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_review_id() -> str:
    return f"tune_{_utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _proposal_confidence(
    *,
    dominant_ratio: float,
    phase4_edge: float | None,
    max_opening_count: int,
) -> str:
    positive_phase4 = phase4_edge is not None and phase4_edge > 0
    if dominant_ratio >= 0.75 and positive_phase4 and max_opening_count >= 50:
        return "high"
    if dominant_ratio >= 0.5 and (positive_phase4 or max_opening_count > 0):
        return "medium"
    return "low"


def _build_tuning_proposal(
    combo_review: dict[str, Any],
    defaults: ReplayParameterOverrides,
) -> dict[str, Any] | None:
    combo_key = str(combo_review.get("combo_key") or "")
    if "_" not in combo_key:
        return None
    family, timeframe = combo_key.rsplit("_", 1)

    dominant_reason = combo_review.get("dominant_blocker")
    dominant_ratio = float(combo_review.get("dominant_blocker_ratio") or 0.0)
    phase4_edge = _as_float(combo_review.get("phase4_cost_adjusted_edge_mean"))
    max_opening_count = int(combo_review.get("max_opening_count") or 0)
    mean_cost_bps = _as_float(combo_review.get("mean_cost_bps"))

    current_value: float | None = None
    proposed_value: float | None = None
    parameter: str | None = None
    reason = ""

    if (
        dominant_reason == "cost_exceeds_max_acceptable"
        and dominant_ratio >= 0.5
        and mean_cost_bps is not None
        and mean_cost_bps >= defaults.max_acceptable_cost_bps * 0.85
        and (phase4_edge is None or phase4_edge > 0)
    ):
        parameter = "max_acceptable_cost_bps"
        current_value = defaults.max_acceptable_cost_bps
        proposed_value = round(current_value + 0.5, 4)
        reason = "成本门槛成为主阻断，且平均成本已接近当前阈值。"
    elif (
        dominant_reason == "net_edge_below_safe_minimum"
        and dominant_ratio >= 0.5
        and phase4_edge is not None
        and phase4_edge > 0
    ):
        parameter = "min_safe_net_edge_bps"
        current_value = defaults.min_safe_net_edge_bps
        proposed_value = round(max(current_value - 0.5, 0.0), 4)
        if proposed_value == current_value:
            return None
        reason = "安全边际门槛成为主阻断，但 Phase 4 成本后边际仍为正。"
    elif (
        dominant_reason == "score_not_stable"
        and dominant_ratio >= 0.5
        and phase4_edge is not None
        and phase4_edge > 0
    ):
        parameter = "score_stability_threshold"
        current_value = defaults.score_stability_threshold
        proposed_value = round(max(current_value - 0.5, 1.0), 4)
        if proposed_value == current_value:
            return None
        reason = "稳定性门槛成为主阻断，但执行后边际仍为正。"

    if parameter is None or current_value is None or proposed_value is None:
        return None

    return {
        "combo_key": combo_key,
        "family": family,
        "timeframe": timeframe,
        "parameter": parameter,
        "current_value": current_value,
        "proposed_value": proposed_value,
        "delta": round(proposed_value - current_value, 4),
        "confidence": _proposal_confidence(
            dominant_ratio=dominant_ratio,
            phase4_edge=phase4_edge,
            max_opening_count=max_opening_count,
        ),
        "dominant_blocker": dominant_reason,
        "dominant_blocker_ratio": dominant_ratio,
        "rationale": (
            f"{reason} 当前组合主阻断占比为 {dominant_ratio:.1%}，"
            f"Phase 4 成本后边际={phase4_edge}。"
        ),
    }


def _build_combo_review(
    combo_key: str,
    rows: list[dict[str, Any]],
    phase4_round: dict[str, Any] | None,
    current_cost_gate_bps: float,
) -> dict[str, Any]:
    blocker_counter: Counter[str] = Counter()
    mean_costs: list[float] = []
    mean_edges: list[float] = []
    execution_ratios: list[float] = []
    max_opening_count = 0

    for row in rows:
        blocker = str(row.get("top_blocking_reason") or "none")
        blocker_counter[blocker] += 1
        cost_bps = _as_float(row.get("mean_cost_bps"))
        if cost_bps is not None:
            mean_costs.append(cost_bps)
        edge_bps = _as_float(row.get("mean_expected_edge_bps"))
        if edge_bps is not None:
            mean_edges.append(edge_bps)
        execution_ratio = _as_float(row.get("execution_compatible_ratio"))
        if execution_ratio is not None:
            execution_ratios.append(execution_ratio)
        max_opening_count = max(max_opening_count, _as_int(row.get("opening_count")))

    dominant_reason, dominant_count = blocker_counter.most_common(1)[0] if blocker_counter else ("none", 0)
    dominant_ratio = round(dominant_count / len(rows), 6) if rows else 0.0
    mean_cost_bps = _mean(mean_costs)
    mean_expected_edge_bps = _mean(mean_edges)
    mean_execution_ratio = _mean(execution_ratios)

    phase4_combo = ((phase4_round or {}).get("combos") or {}).get(combo_key, {})
    phase4_cost_summary = phase4_combo.get("cost_summary", {})
    phase4_edge = _as_float(phase4_cost_summary.get("cost_adjusted_edge_mean"))
    phase4_full_fill = _as_float(phase4_cost_summary.get("full_fill_ratio"))

    suggested_focus = "inspect_signal_generation"
    rationale = "需要先核对信号质量与主阻断的对应关系。"

    if dominant_reason == "cost_exceeds_max_acceptable":
        suggested_focus = "revisit_max_acceptable_cost_bps"
        if mean_cost_bps is not None and mean_cost_bps < current_cost_gate_bps:
            rationale = "成本阻断占主导，但平均成本尚未贴近阈值，优先核对成本口径。"
        else:
            rationale = "成本阻断占主导，且平均成本已逼近当前阈值。"
    elif dominant_reason == "net_edge_below_safe_minimum":
        suggested_focus = "revisit_min_safe_net_edge_bps"
        rationale = "安全边际门槛占主导，应先复核 min_safe_net_edge_bps。"
    elif dominant_reason == "score_not_stable":
        suggested_focus = "revisit_score_stability_threshold"
        rationale = "稳定性门槛占主导，应先复核 score_stability_threshold。"
    elif max_opening_count > 0 and (phase4_edge or 0) > 0:
        suggested_focus = "keep_current_gates"
        rationale = "已有开仓且执行后边际为正，当前不建议优先动门槛。"

    recommend_cost_gate_reassessment = (
        dominant_reason == "cost_exceeds_max_acceptable"
        and dominant_ratio >= 0.5
        and mean_cost_bps is not None
        and mean_cost_bps >= current_cost_gate_bps * 0.85
    )

    return {
        "combo_key": combo_key,
        "experiment_count": len(rows),
        "dominant_blocker": dominant_reason,
        "dominant_blocker_ratio": dominant_ratio,
        "blocker_breakdown": dict(blocker_counter),
        "max_opening_count": max_opening_count,
        "mean_cost_bps": mean_cost_bps,
        "mean_expected_edge_bps": mean_expected_edge_bps,
        "mean_execution_compatible_ratio": mean_execution_ratio,
        "phase4_cost_adjusted_edge_mean": phase4_edge,
        "phase4_full_fill_ratio": phase4_full_fill,
        "suggested_focus": suggested_focus,
        "recommend_cost_gate_reassessment": recommend_cost_gate_reassessment,
        "rationale": rationale,
    }


def _build_markdown_review(result: dict[str, Any]) -> str:
    lines = [
        "# Strategy Tuning Review",
        "",
        f"- Review ID: `{result.get('review_id')}`",
        f"- Generated At: `{result.get('generated_at')}`",
        f"- Step2 Round: `{result.get('step2_round_id') or 'N/A'}`",
        f"- Phase4 Round: `{result.get('phase4_round_id') or 'N/A'}`",
        f"- Current max_acceptable_cost_bps: `{result.get('current_cost_gate_bps')}`",
        f"- Global Recommendation: `{result.get('global_recommendation')}`",
        f"- Reassess max_acceptable_cost_bps: `{result.get('recommend_cost_gate_reassessment')}`",
        f"- Generated Proposals: `{result.get('proposal_count', 0)}`",
        "",
        "## Combo Review",
        "",
        "| Combo | Dominant Blocker | Focus | Cost Gate? | Rationale |",
        "|---|---|---|---|---|",
    ]
    for combo in result.get("combos", []):
        lines.append(
            "| {combo_key} | `{dominant_blocker}` | `{suggested_focus}` | `{reassess}` | {rationale} |".format(
                combo_key=combo.get("combo_key"),
                dominant_blocker=combo.get("dominant_blocker"),
                suggested_focus=combo.get("suggested_focus"),
                reassess=combo.get("recommend_cost_gate_reassessment"),
                rationale=combo.get("rationale"),
            ),
        )

    lines.extend(
        [
            "",
            "## Generated Proposals",
            "",
            "| Proposal | Combo | Parameter | Current | Proposed | Status | Confidence |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for proposal in result.get("proposals", []):
        lines.append(
            "| {proposal_id} | {combo_key} | `{parameter}` | `{current}` | `{proposed}` | `{status}` | `{confidence}` |".format(
                proposal_id=proposal.get("proposal_id", "pending"),
                combo_key=proposal.get("combo_key"),
                parameter=proposal.get("parameter"),
                current=proposal.get("current_value"),
                proposed=proposal.get("proposed_value"),
                status=proposal.get("status", "pending_review"),
                confidence=proposal.get("confidence", "low"),
            ),
        )
    if not result.get("proposals"):
        lines.append("| - | - | - | - | - | `no_change` | - |")

    return "\n".join(lines) + "\n"


def _save_review(project_root: Path, result: dict[str, Any]) -> dict[str, str]:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    review_dir = project_root / _OUTPUT_ROOT / result["review_id"]
    review_dir.mkdir(parents=True, exist_ok=True)

    summary_path = review_dir / "strategy_tuning_review.json"
    atomic_json_write(result, summary_path)

    report_path = review_dir / "strategy_tuning_review.md"
    report_path.write_text(_build_markdown_review(result), encoding="utf-8")

    return {
        "summary_path": str(summary_path),
        "report_path": str(report_path),
    }


def build_strategy_tuning_review(
    project_root: Path,
    *,
    save_results: bool = True,
) -> dict[str, Any]:
    review_id = _make_review_id()
    generated_at = _utcnow().isoformat()
    defaults = ReplayParameterOverrides()

    step2_snapshot = load_latest_research_round_snapshot(
        phase=ROUND_PHASE_STEP2,
        project_root=project_root,
    )
    scan_summary = None
    if isinstance(step2_snapshot, dict):
        scan_summary = (step2_snapshot.get("summary") or {}).get("scan_comparison_summary")
    rows = extract_comparison_rows(scan_summary if isinstance(scan_summary, dict) else {})

    phase4_evidence = collect_phase4_evidence(project_root)
    phase4_round = phase4_evidence.get("latest_round")

    combos_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        combo_key = make_combo_key(row.get("family"), row.get("timeframe"))
        if combo_key:
            combos_by_key[combo_key].append(row)

    combo_reviews: list[dict[str, Any]] = []
    proposals_to_record: list[dict[str, Any]] = []
    for combo in COMBOS:
        combo_key = combo["key"]
        combo_review = _build_combo_review(
            combo_key,
            combos_by_key.get(combo_key, []),
            phase4_round if isinstance(phase4_round, dict) else None,
            defaults.max_acceptable_cost_bps,
        )
        proposal = _build_tuning_proposal(combo_review, defaults)
        if proposal is not None:
            proposals_to_record.append(proposal)
        combo_reviews.append(combo_review)

    focus_counter = Counter(combo["suggested_focus"] for combo in combo_reviews)
    global_recommendation = focus_counter.most_common(1)[0][0] if focus_counter else "insufficient_data"
    recommend_cost_gate_reassessment = any(
        combo["recommend_cost_gate_reassessment"] for combo in combo_reviews
    )

    result: dict[str, Any] = {
        "ok": True,
        "review_id": review_id,
        "generated_at": generated_at,
        "step2_round_id": step2_snapshot.get("round_id") if isinstance(step2_snapshot, dict) else None,
        "phase4_round_id": phase4_round.get("round_id") if isinstance(phase4_round, dict) else None,
        "current_cost_gate_bps": defaults.max_acceptable_cost_bps,
        "current_min_safe_net_edge_bps": defaults.min_safe_net_edge_bps,
        "current_score_stability_threshold": defaults.score_stability_threshold,
        "global_recommendation": global_recommendation,
        "recommend_cost_gate_reassessment": recommend_cost_gate_reassessment,
        "proposal_count": 0,
        "proposals": [],
        "combos": combo_reviews,
    }

    if save_results:
        proposal_result = record_generated_proposals(
            project_root,
            review_id=review_id,
            proposals=proposals_to_record,
        )
        result["proposal_count"] = len(proposal_result["recorded_proposals"])
        result["pending_review_count"] = proposal_result["pending_review_count"]
        result["proposal_registry_path"] = proposal_result["registry_path"]
        result["proposal_overrides_path"] = proposal_result["overrides_path"]
        result["proposals"] = proposal_result["recorded_proposals"]
        result["artifacts"] = _save_review(project_root, result)
    else:
        result["proposal_count"] = len(proposals_to_record)
        result["proposals"] = [
            {
                "status": "pending_review",
                "review_required": True,
                **proposal,
            }
            for proposal in proposals_to_record
        ]

    return result
