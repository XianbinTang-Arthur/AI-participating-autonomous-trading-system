"""Phase 6-A: Evidence Bundle 统一化.

从 Phase 2/3/4/5 的 artifact 中收集证据，
整理为统一的 evidence bundle，供 decision engine 使用。
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── 四个 family × timeframe combo ────────────────────────────────────

COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1H"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1H"},
]


def _safe_load_json(path: pathlib.Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── Phase 2 证据 ─────────────────────────────────────────────────────


def collect_phase2_evidence(project_root: pathlib.Path) -> dict[str, Any]:
    """收集 Phase 2 (Step 1/2) 的研究证据."""
    exp_root = project_root / "artifacts/research/experiments"
    evidence: dict[str, Any] = {
        "source": "phase2",
        "experiment_count": 0,
        "parameter_scan_count": 0,
        "experiments": [],
        "best_experiments": [],
        "aggregate_stats": {},
    }

    if not exp_root.exists():
        return evidence

    all_diags: list[dict[str, Any]] = []

    for subdir in sorted(exp_root.iterdir()):
        if not subdir.is_dir():
            continue

        # 参数扫描
        comp_file = subdir / "comparison_summary.json"
        if comp_file.exists():
            evidence["parameter_scan_count"] += 1
            comp = _safe_load_json(comp_file)
            if comp:
                evidence["experiments"].append({
                    "id": subdir.name,
                    "type": "parameter_scan",
                    "experiment_count": comp.get("experiment_count"),
                    "comparison": comp.get("comparison", []),
                })
            continue

        # 单实验
        diag_file = subdir / "diagnostics.json"
        if diag_file.exists():
            diag = _safe_load_json(diag_file)
            if diag:
                evidence["experiment_count"] += 1
                entry = {
                    "id": subdir.name,
                    "type": "experiment",
                    "total_bars": diag.get("total_bars"),
                    "opening_count": diag.get("opening_count", 0),
                    "positive_edge_ratio": diag.get("positive_edge_ratio", 0),
                    "mean_expected_edge_bps": diag.get("mean_expected_edge_bps"),
                    "execution_compatible_ratio": diag.get("execution_compatible_ratio"),
                    "selectable_ratio": diag.get("selectable_ratio"),
                }
                evidence["experiments"].append(entry)
                all_diags.append(entry)

    # 聚合统计
    if all_diags:
        openings = [d["opening_count"] for d in all_diags]
        edge_ratios = [d["positive_edge_ratio"] for d in all_diags if d["positive_edge_ratio"] is not None]
        evidence["aggregate_stats"] = {
            "total_experiments": len(all_diags),
            "experiments_with_openings": sum(1 for o in openings if o > 0),
            "max_opening_count": max(openings),
            "mean_positive_edge_ratio": (
                sum(edge_ratios) / len(edge_ratios) if edge_ratios else 0
            ),
        }
        # 选出最好的实验（按 opening_count 降序）
        best = sorted(all_diags, key=lambda d: d["opening_count"], reverse=True)[:3]
        evidence["best_experiments"] = best

    return evidence


# ── Phase 3 证据 ─────────────────────────────────────────────────────


def collect_phase3_evidence(project_root: pathlib.Path) -> dict[str, Any]:
    """收集 Phase 3 归因证据."""
    attr_root = project_root / "artifacts/research/attribution_rounds"
    evidence: dict[str, Any] = {
        "source": "phase3",
        "round_count": 0,
        "latest_round": None,
        "combo_results": {},
    }

    if not attr_root.exists():
        return evidence

    rounds: list[dict[str, Any]] = []
    for subdir in sorted(attr_root.iterdir()):
        if not subdir.is_dir():
            continue
        manifest = _safe_load_json(subdir / "round_manifest.json")
        if not manifest:
            continue

        evidence["round_count"] += 1
        round_info: dict[str, Any] = {
            "round_id": manifest.get("round_id", subdir.name),
            "started_at": manifest.get("started_at"),
            "status": "unknown",
            "combos": {},
        }

        # combo 状态
        combos = manifest.get("combos", [])
        statuses = set()
        for c in combos:
            key = c.get("key", "?")
            status = c.get("status", "unknown")
            statuses.add(status)
            round_info["combos"][key] = {"status": status}

            # 读 attribution_summary
            run_dir = c.get("run_dir")
            if run_dir:
                summary_file = pathlib.Path(run_dir) / "attribution_summary.json"
                summary = _safe_load_json(summary_file)
                if summary:
                    round_info["combos"][key]["attribution_summary"] = summary

                # 读 top_failure_modes
                tfm_file = pathlib.Path(run_dir) / "top_failure_modes.json"
                tfm = _safe_load_json(tfm_file)
                if tfm:
                    round_info["combos"][key]["top_failure_modes"] = tfm

        # 聚合状态
        if statuses == {"succeeded"}:
            round_info["status"] = "succeeded"
        elif "failed" in statuses and statuses - {"failed"} == set():
            round_info["status"] = "failed"
        else:
            round_info["status"] = "partial_success"

        rounds.append(round_info)

    # 最近 round
    if rounds:
        rounds.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        evidence["latest_round"] = rounds[0]

    return evidence


# ── Phase 4 证据 ─────────────────────────────────────────────────────


def collect_phase4_evidence(project_root: pathlib.Path) -> dict[str, Any]:
    """收集 Phase 4 执行代理评估证据."""
    exec_root = project_root / "artifacts/research/execution_rounds"
    evidence: dict[str, Any] = {
        "source": "phase4",
        "round_count": 0,
        "latest_round": None,
        "combo_results": {},
    }

    if not exec_root.exists():
        return evidence

    rounds: list[dict[str, Any]] = []
    for subdir in sorted(exec_root.iterdir()):
        if not subdir.is_dir():
            continue
        manifest = _safe_load_json(subdir / "round_manifest.json")
        if not manifest:
            continue

        evidence["round_count"] += 1
        round_info: dict[str, Any] = {
            "round_id": manifest.get("round_id", subdir.name),
            "started_at": manifest.get("started_at"),
            "status": "unknown",
            "combos": {},
        }

        combos = manifest.get("combos", [])
        statuses = set()
        for c in combos:
            key = c.get("key", "?")
            status = c.get("status", "unknown")
            statuses.add(status)
            combo_info: dict[str, Any] = {
                "status": status,
                "candidates": c.get("candidates", 0),
            }

            # 读 execution_cost_summary
            run_dir = c.get("run_dir")
            if run_dir:
                cost_file = pathlib.Path(run_dir) / "execution_cost_summary.json"
                cost = _safe_load_json(cost_file)
                if cost:
                    combo_info["cost_summary"] = {
                        "total_candidates": cost.get("total_candidates", 0),
                        "full_fill_ratio": cost.get("full_fill_ratio", 0),
                        "slippage_mean": cost.get("slippage", {}).get("mean", 0),
                        "total_cost_mean": cost.get("total_execution_cost", {}).get("mean", 0),
                        "cost_adjusted_edge_mean": cost.get("cost_adjusted_edge", {}).get("mean", 0),
                        "positive_adjusted_edge_ratio": cost.get("positive_adjusted_edge_ratio", 0),
                    }

            round_info["combos"][key] = combo_info

        if statuses == {"succeeded"}:
            round_info["status"] = "succeeded"
        elif "failed" in statuses and statuses - {"failed"} == set():
            round_info["status"] = "failed"
        else:
            round_info["status"] = "partial_success"

        rounds.append(round_info)

    if rounds:
        rounds.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        evidence["latest_round"] = rounds[0]

    return evidence


# ── Phase 5 治理证据 ─────────────────────────────────────────────────


def collect_phase5_evidence(project_root: pathlib.Path) -> dict[str, Any]:
    """收集 Phase 5 治理层证据."""
    gov_root = project_root / "artifacts/governance"
    evidence: dict[str, Any] = {
        "source": "phase5_governance",
        "artifact_index_exists": False,
        "parameter_registry_exists": False,
        "quality_monitor_exists": False,
        "quality_health": None,
        "frozen_parameter_sets": [],
        "candidate_parameter_sets": [],
        "total_artifacts": 0,
        "critical_failures": 0,
    }

    # artifact_index
    ai = _safe_load_json(gov_root / "artifact_index.json")
    if ai:
        evidence["artifact_index_exists"] = True
        evidence["total_artifacts"] = ai.get("summary", {}).get("total_artifacts", 0)

    # parameter registry
    reg = _safe_load_json(gov_root / "current_parameter_registry.json")
    if reg:
        evidence["parameter_registry_exists"] = True
        for ps in reg.get("parameter_sets", []):
            if ps.get("status") == "frozen":
                evidence["frozen_parameter_sets"].append({
                    "parameter_set_id": ps["parameter_set_id"],
                    "family": ps["family"],
                    "timeframe": ps["timeframe"],
                    "values": ps.get("values", {}),
                    "frozen_at": ps.get("frozen_at"),
                })
            elif ps.get("status") == "candidate":
                evidence["candidate_parameter_sets"].append({
                    "parameter_set_id": ps["parameter_set_id"],
                    "family": ps["family"],
                    "timeframe": ps["timeframe"],
                    "values": ps.get("values", {}),
                })

    # quality monitor
    qm = _safe_load_json(gov_root / "quality_monitor_summary.json")
    if qm:
        evidence["quality_monitor_exists"] = True
        summary = qm.get("summary", {})
        evidence["quality_health"] = summary.get("health")
        evidence["critical_failures"] = summary.get("critical_failures", 0)

    return evidence


# ── 完整 Evidence Bundle 构建 ────────────────────────────────────────


def build_evidence_bundle(project_root: pathlib.Path) -> dict[str, Any]:
    """构建完整的 evidence bundle."""
    log.info("收集 Phase 2 证据...")
    p2 = collect_phase2_evidence(project_root)

    log.info("收集 Phase 3 证据...")
    p3 = collect_phase3_evidence(project_root)

    log.info("收集 Phase 4 证据...")
    p4 = collect_phase4_evidence(project_root)

    log.info("收集 Phase 5 治理证据...")
    p5 = collect_phase5_evidence(project_root)

    # 证据完整度
    phases_with_data = []
    if p2.get("experiment_count", 0) > 0 or p2.get("parameter_scan_count", 0) > 0:
        phases_with_data.append("phase2")
    if p3.get("round_count", 0) > 0:
        phases_with_data.append("phase3")
    if p4.get("round_count", 0) > 0:
        phases_with_data.append("phase4")
    if p5.get("artifact_index_exists"):
        phases_with_data.append("phase5")

    bundle: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "evidence_completeness": {
            "phases_with_data": phases_with_data,
            "total_phases": 4,
            "completeness_ratio": len(phases_with_data) / 4,
        },
        "phase2_evidence": p2,
        "phase3_evidence": p3,
        "phase4_evidence": p4,
        "phase5_governance_evidence": p5,
    }

    return bundle
