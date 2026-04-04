"""指标计算器.

工作包 A: 从 artifacts 中读取数据，计算各层指标值。
支持按 family / timeframe 维度筛选。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_json(fp: Path) -> dict | None:
    if not fp.exists():
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


# ── 研究层 ────────────────────────────────────────────────────

def calc_research_metrics(
    root: Path,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """计算研究层指标."""
    rec_data = _load_json(
        root / "artifacts" / "decision_system" / "recommendation_registry.json"
    )
    ps_data = _load_json(
        root / "artifacts" / "governance" / "current_parameter_registry.json"
    )

    recs = rec_data.get("recommendations", []) if rec_data else []
    psets = ps_data.get("parameter_sets", []) if ps_data else []

    # 筛选
    if family:
        recs = [r for r in recs if r.get("family") == family]
        psets = [p for p in psets if p.get("family") == family]
    if timeframe:
        recs = [r for r in recs if r.get("timeframe") == timeframe]
        psets = [p for p in psets if p.get("timeframe") == timeframe]

    total_recs = len(recs)
    approved = sum(1 for r in recs if r.get("status") == "approved")
    draft = sum(1 for r in recs if r.get("status") == "draft")
    promoted = sum(
        1 for p in psets if p.get("status") in ("frozen", "candidate")
    )

    # 证据完整性: 从最新 decision round 取
    evidence_ratio = _calc_evidence_completeness(root, family, timeframe)

    return {
        "recommendation_count": total_recs,
        "approved_recommendation_count": approved,
        "promoted_parameter_set_count": promoted,
        "evidence_completeness_ratio": evidence_ratio,
        "stale_recommendation_ratio": _safe_ratio(draft, total_recs),
    }


def _calc_evidence_completeness(
    root: Path,
    family: str | None,
    timeframe: str | None,
) -> float:
    """从最新 decision round 计算证据完整性."""
    rounds_dir = root / "artifacts" / "decision_rounds"
    if not rounds_dir.exists():
        return 0.0
    dirs = sorted(
        [d for d in rounds_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for d in dirs[:3]:  # 检查最近 3 个
        es = _load_json(d / "evidence_summary.json")
        if es and "evidence_completeness" in es:
            ec = es["evidence_completeness"]
            return round(ec.get("completeness_ratio", 0.0), 4)
    return 0.0


# ── 归因层 ────────────────────────────────────────────────────

def calc_attribution_metrics(
    root: Path,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """计算归因层指标.

    从 decision_rounds 的 evidence_summary 中提取 phase3 信息。
    第一版: 若无归因数据则返回默认值。
    """
    # 尝试从最新 round 获取
    rounds_dir = root / "artifacts" / "decision_rounds"
    phase3 = _find_latest_phase_evidence(rounds_dir, "phase3_evidence")

    if not phase3 or not phase3.get("source"):
        return {
            "replay_live_alignment_coverage": 0.0,
            "top_failure_mode_concentration": 0.0,
            "strategy_blocked_ratio": 0.0,
            "risk_rejected_ratio": 0.0,
            "execution_blocked_ratio": 0.0,
        }

    # 从 phase3 数据提取（如果有）
    return {
        "replay_live_alignment_coverage": phase3.get(
            "replay_live_alignment_coverage", 0.0
        ),
        "top_failure_mode_concentration": phase3.get(
            "top_failure_mode_concentration", 0.0
        ),
        "strategy_blocked_ratio": phase3.get("strategy_blocked_ratio", 0.0),
        "risk_rejected_ratio": phase3.get("risk_rejected_ratio", 0.0),
        "execution_blocked_ratio": phase3.get("execution_blocked_ratio", 0.0),
    }


# ── 执行可行性层 ──────────────────────────────────────────────

def calc_execution_metrics(
    root: Path,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """计算执行可行性层指标.

    从 decision_rounds 的 evidence_summary 中提取 phase4 信息。
    """
    rounds_dir = root / "artifacts" / "decision_rounds"
    phase4 = _find_latest_phase_evidence(rounds_dir, "phase4_evidence")

    if not phase4 or not phase4.get("source"):
        return {
            "full_fill_ratio": 0.0,
            "partial_fill_ratio": 0.0,
            "mean_total_execution_cost_bps": 0.0,
            "positive_adjusted_edge_ratio": 0.0,
        }

    return {
        "full_fill_ratio": phase4.get("full_fill_ratio", 0.0),
        "partial_fill_ratio": phase4.get("partial_fill_ratio", 0.0),
        "mean_total_execution_cost_bps": phase4.get(
            "mean_total_execution_cost_bps", 0.0
        ),
        "positive_adjusted_edge_ratio": phase4.get(
            "positive_adjusted_edge_ratio", 0.0
        ),
    }


def _find_latest_phase_evidence(
    rounds_dir: Path, phase_key: str
) -> dict | None:
    """从最新 round 找指定 phase evidence."""
    if not rounds_dir or not rounds_dir.exists():
        return None
    dirs = sorted(
        [d for d in rounds_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for d in dirs[:5]:
        es = _load_json(d / "evidence_summary.json")
        if es and phase_key in es:
            phase = es[phase_key]
            if phase and isinstance(phase, dict):
                return phase
    return None


# ── 运营层 ────────────────────────────────────────────────────

def calc_operations_metrics(
    root: Path,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """计算运营层指标."""
    # apply history
    apply_data = _load_json(
        root / "artifacts" / "decision_system" / "parameter_apply_history.json"
    )
    ops = apply_data.get("operations", []) if apply_data else []
    if family:
        ops = [o for o in ops if o.get("family") == family]
    if timeframe:
        ops = [o for o in ops if o.get("timeframe") == timeframe]

    apply_count = sum(1 for o in ops if o.get("operation_type") == "apply")
    rollback_count = sum(1 for o in ops if o.get("operation_type") == "rollback")

    # release history
    rel_data = _load_json(
        root / "artifacts" / "production_workflow" / "parameter_release_history.json"
    )
    releases = rel_data.get("releases", []) if rel_data else []
    if family:
        releases = [r for r in releases if r.get("family") == family]
    if timeframe:
        releases = [r for r in releases if r.get("timeframe") == timeframe]

    total_releases = len(releases)
    completed_obs = sum(
        1 for r in releases if r.get("observation_status") == "completed"
    )
    no_gate = sum(
        1 for r in releases
        if not r.get("gate_result_ref")
    )

    # rollback recommendations
    rb_dir = root / "artifacts" / "production_workflow" / "rollback_recommendations"
    rb_count = 0
    if rb_dir.exists():
        for d in rb_dir.iterdir():
            if d.is_dir():
                rb_rec = _load_json(d / "rollback_recommendation.json")
                if rb_rec and rb_rec.get("rollback_recommended"):
                    if family and rb_rec.get("family") != family:
                        continue
                    if timeframe and rb_rec.get("timeframe") != timeframe:
                        continue
                    rb_count += 1

    return {
        "apply_success_count": apply_count,
        "rollback_count": rollback_count,
        "rollback_recommendation_count": rb_count,
        "release_observation_completion_ratio": _safe_ratio(
            completed_obs, total_releases
        ),
        "release_without_gate_ratio": _safe_ratio(no_gate, total_releases),
    }


# ── 可靠性层 ──────────────────────────────────────────────────

def calc_reliability_metrics(root: Path, **_: Any) -> dict[str, Any]:
    """计算可靠性层指标."""
    # workflow runs
    runs_dir = root / "artifacts" / "operations" / "workflow_runs"
    total_runs = 0
    success_runs = 0
    if runs_dir.exists():
        for f in runs_dir.iterdir():
            if f.suffix == ".json":
                data = _load_json(f)
                if data:
                    total_runs += 1
                    if data.get("overall_status") == "success":
                        success_runs += 1

    # failures
    fail_data = _load_json(
        root / "artifacts" / "operations" / "workflow_failures.json"
    )
    failures = fail_data.get("failures", []) if fail_data else []
    open_failures = [f for f in failures if f.get("status") == "open"]
    retried = [f for f in failures if f.get("retry_count", 0) > 0]
    retry_success = [
        f for f in retried if f.get("last_retry_result") == "success"
    ]

    # alerts
    alerts_data = _load_json(
        root / "artifacts" / "operations" / "alerts" / "current_alerts.json"
    )
    alert_count = 0
    if alerts_data:
        alert_count = sum(
            1 for a in alerts_data.get("alerts", [])
            if not a.get("acknowledged")
        )

    # stale rounds (decision_rounds older than 7 days with no subsequent round)
    stale = 0
    rounds_dir = root / "artifacts" / "decision_rounds"
    if rounds_dir.exists():
        dirs = sorted(
            [d for d in rounds_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
        )
        if len(dirs) > 1:
            # 只算除最后一个外的 rounds
            for d in dirs[:-1]:
                manifest = _load_json(d / "round_manifest.json")
                if manifest and manifest.get("status") not in (
                    "completed", "closed"
                ):
                    stale += 1

    return {
        "workflow_success_ratio": _safe_ratio(success_runs, total_runs),
        "retry_success_ratio": _safe_ratio(
            len(retry_success), len(retried)
        ),
        "alert_open_count": alert_count,
        "alert_resolution_time_hours": 0.0,  # 第一版不追踪精确时间
        "stale_round_count": stale,
    }


# ── 汇总计算 ──────────────────────────────────────────────────

def calculate_all_metrics(
    root: Path,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict[str, dict[str, Any]]:
    """计算所有层的指标.

    Returns:
        {"research": {...}, "attribution": {...}, ...}
    """
    return {
        "research": calc_research_metrics(root, family, timeframe),
        "attribution": calc_attribution_metrics(root, family, timeframe),
        "execution": calc_execution_metrics(root, family, timeframe),
        "operations": calc_operations_metrics(root, family, timeframe),
        "reliability": calc_reliability_metrics(root),
    }


def flatten_metrics(by_layer: dict[str, dict]) -> dict[str, Any]:
    """将按层组织的指标展平为单层 dict."""
    flat = {}
    for layer, metrics in by_layer.items():
        for k, v in metrics.items():
            flat[k] = v
    return flat
