"""Phase 6-A: Evidence Bundle 统一化.

从 Phase 5 治理层索引出发收集证据，而非直接扫目录。
优先读取 artifact_index.json / active_round_index.json，
按治理层认可的 active/trusted rounds 提取证据，
过滤掉 deprecated / stale 结果。

仅在治理索引不存在时 fallback 到直接目录扫描。
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

# 不受信任的 round 状态，不应作为证据来源
_UNTRUSTED_STATUSES: set[str] = {"deprecated", "failed"}


def _safe_load_json(path: pathlib.Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Failed to load JSON from %s: %s", path, exc)
        return None


# ── Phase 2 证据 ─────────────────────────────────────────────────────


def collect_phase2_evidence(
    project_root: pathlib.Path,
    *,
    artifact_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """收集 Phase 2 (Step 1/2) 的研究证据.

    优先从 artifact_index 获取实验列表（治理认可的 artifact），
    仅在 index 不存在时 fallback 到直接扫描。
    """
    evidence: dict[str, Any] = {
        "source": "phase2",
        "evidence_source": "governance_index" if artifact_index else "directory_scan",
        "experiment_count": 0,
        "parameter_scan_count": 0,
        "experiments": [],
        "best_experiments": [],
        "aggregate_stats": {},
    }

    all_diags: list[dict[str, Any]] = []

    if artifact_index:
        # ── 优先路径：从治理索引读 ──
        for artifact in artifact_index.get("artifacts", []):
            if artifact.get("phase") not in ("phase2_step1", "phase2_step2"):
                continue

            atype = artifact.get("artifact_type", "experiment")
            art_path = pathlib.Path(artifact.get("path", ""))

            if atype == "parameter_scan":
                evidence["parameter_scan_count"] += 1
                comp = _safe_load_json(art_path / "comparison_summary.json")
                if comp:
                    evidence["experiments"].append({
                        "id": artifact["artifact_id"],
                        "type": "parameter_scan",
                        "experiment_count": comp.get("experiment_count"),
                        "comparison": comp.get("comparison", []),
                    })
                continue

            # 单实验
            ds = artifact.get("diagnostics_summary")
            if ds:
                evidence["experiment_count"] += 1
                entry = {
                    "id": artifact["artifact_id"],
                    "type": "experiment",
                    "total_bars": ds.get("total_bars"),
                    "opening_count": ds.get("opening_count", 0),
                    "positive_edge_ratio": ds.get("positive_edge_ratio", 0),
                }
                # 补充完整 diagnostics
                diag = _safe_load_json(art_path / "diagnostics.json")
                if diag:
                    entry["mean_expected_edge_bps"] = diag.get("mean_expected_edge_bps")
                    entry["execution_compatible_ratio"] = diag.get("execution_compatible_ratio")
                    entry["selectable_ratio"] = diag.get("selectable_ratio")

                evidence["experiments"].append(entry)
                all_diags.append(entry)
    else:
        # ── Fallback：直接扫描 ──
        log.warning("artifact_index 不存在，fallback 到目录扫描")
        exp_root = project_root / "artifacts/research/experiments"
        if not exp_root.exists():
            return evidence

        for subdir in sorted(exp_root.iterdir()):
            if not subdir.is_dir():
                continue

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
        best = sorted(all_diags, key=lambda d: d["opening_count"], reverse=True)[:3]
        evidence["best_experiments"] = best

    return evidence


# ── Phase 3 证据 ─────────────────────────────────────────────────────


def _collect_round_evidence_from_index(
    active_round_index: dict[str, Any],
    phase: str,
) -> list[dict[str, Any]]:
    """从 active_round_index 获取某 phase 的可信 round 列表."""
    trusted: list[dict[str, Any]] = []
    for r in active_round_index.get("all_rounds", []):
        if r.get("phase") != phase:
            continue
        if r.get("status") in _UNTRUSTED_STATUSES:
            continue
        trusted.append(r)
    return trusted


def _enrich_round_from_manifest(
    round_info: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    """从 round 目录的 manifest 和子文件中补充详细证据."""
    round_dir = pathlib.Path(round_info.get("path", ""))
    manifest = _safe_load_json(round_dir / "round_manifest.json")
    if not manifest:
        return round_info

    enriched: dict[str, Any] = {
        "round_id": round_info.get("round_id", round_dir.name),
        "started_at": round_info.get("started_at"),
        "status": round_info.get(
            "status",
            manifest.get("overall_status", "unknown"),
        ),
        "combos": {},
    }

    combos = manifest.get("combos", [])
    for c in combos:
        key = c.get("key", "?")
        status = c.get("status", "unknown")
        combo_data: dict[str, Any] = {"status": status}

        run_dir = c.get("run_dir")
        if run_dir:
            run_path = pathlib.Path(run_dir)
            if not run_path.is_absolute():
                run_path = run_path.resolve()

            if phase == "phase3":
                summary = _safe_load_json(run_path / "attribution_summary.json")
                if summary:
                    combo_data["attribution_summary"] = summary
                tfm = _safe_load_json(run_path / "top_failure_modes.json")
                if tfm:
                    combo_data["top_failure_modes"] = tfm

            elif phase == "phase4":
                cost = _safe_load_json(run_path / "execution_cost_summary.json")
                if cost:
                    combo_data["cost_summary"] = {
                        "total_candidates": cost.get("total_candidates", 0),
                        "full_fill_ratio": cost.get("full_fill_ratio", 0),
                        "slippage_mean": cost.get("slippage", {}).get("mean", 0),
                        "total_cost_mean": cost.get("total_execution_cost", {}).get("mean", 0),
                        "cost_adjusted_edge_mean": cost.get("cost_adjusted_edge", {}).get("mean", 0),
                        "positive_edge_ratio": cost.get("positive_edge_ratio", 0),
                    }

        enriched["combos"][key] = combo_data

    return enriched


def collect_phase3_evidence(
    project_root: pathlib.Path,
    *,
    active_round_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """收集 Phase 3 归因证据.

    优先从 active_round_index 获取可信 round（排除 deprecated/failed），
    仅在 index 不存在时 fallback 到直接扫描。
    """
    evidence: dict[str, Any] = {
        "source": "phase3",
        "evidence_source": "governance_index" if active_round_index else "directory_scan",
        "round_count": 0,
        "trusted_round_count": 0,
        "skipped_untrusted": 0,
        "latest_round": None,
        "combo_results": {},
    }

    rounds: list[dict[str, Any]] = []

    if active_round_index:
        # ── 优先路径：从治理索引读 ──
        trusted = _collect_round_evidence_from_index(active_round_index, "phase3")
        evidence["trusted_round_count"] = len(trusted)

        # 统计被跳过的
        all_p3 = [r for r in active_round_index.get("all_rounds", []) if r.get("phase") == "phase3"]
        evidence["round_count"] = len(all_p3)
        evidence["skipped_untrusted"] = len(all_p3) - len(trusted)

        for r in trusted:
            enriched = _enrich_round_from_manifest(r, "phase3")
            rounds.append(enriched)
    else:
        # ── Fallback ──
        log.warning("active_round_index 不存在，Phase 3 fallback 到目录扫描")
        attr_root = project_root / "artifacts/research/attribution_rounds"
        if not attr_root.exists():
            return evidence

        for subdir in sorted(attr_root.iterdir()):
            if not subdir.is_dir():
                continue
            manifest = _safe_load_json(subdir / "round_manifest.json")
            if not manifest:
                continue

            evidence["round_count"] += 1
            # 跳过 deprecated
            round_status = manifest.get("overall_status", manifest.get("status"))
            if round_status in _UNTRUSTED_STATUSES:
                evidence["skipped_untrusted"] += 1
                continue

            evidence["trusted_round_count"] += 1
            enriched = _enrich_round_from_manifest(
                {"round_id": manifest.get("round_id", subdir.name),
                 "started_at": manifest.get("started_at"),
                 "status": manifest.get("overall_status", manifest.get("status", "unknown")),
                 "path": str(subdir)},
                "phase3",
            )
            rounds.append(enriched)

    if rounds:
        rounds.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        evidence["latest_round"] = rounds[0]

    return evidence


# ── Phase 4 证据 ─────────────────────────────────────────────────────


def collect_phase4_evidence(
    project_root: pathlib.Path,
    *,
    active_round_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """收集 Phase 4 执行代理评估证据.

    优先从 active_round_index 获取可信 round（排除 deprecated/failed），
    仅在 index 不存在时 fallback 到直接扫描。
    """
    evidence: dict[str, Any] = {
        "source": "phase4",
        "evidence_source": "governance_index" if active_round_index else "directory_scan",
        "round_count": 0,
        "trusted_round_count": 0,
        "skipped_untrusted": 0,
        "latest_round": None,
        "combo_results": {},
    }

    rounds: list[dict[str, Any]] = []

    if active_round_index:
        trusted = _collect_round_evidence_from_index(active_round_index, "phase4")
        evidence["trusted_round_count"] = len(trusted)

        all_p4 = [r for r in active_round_index.get("all_rounds", []) if r.get("phase") == "phase4"]
        evidence["round_count"] = len(all_p4)
        evidence["skipped_untrusted"] = len(all_p4) - len(trusted)

        for r in trusted:
            enriched = _enrich_round_from_manifest(r, "phase4")
            rounds.append(enriched)
    else:
        log.warning("active_round_index 不存在，Phase 4 fallback 到目录扫描")
        exec_root = project_root / "artifacts/research/execution_rounds"
        if not exec_root.exists():
            return evidence

        for subdir in sorted(exec_root.iterdir()):
            if not subdir.is_dir():
                continue
            manifest = _safe_load_json(subdir / "round_manifest.json")
            if not manifest:
                continue

            evidence["round_count"] += 1
            round_status = manifest.get("overall_status", manifest.get("status"))
            if round_status in _UNTRUSTED_STATUSES:
                evidence["skipped_untrusted"] += 1
                continue

            evidence["trusted_round_count"] += 1
            enriched = _enrich_round_from_manifest(
                {"round_id": manifest.get("round_id", subdir.name),
                 "started_at": manifest.get("started_at"),
                 "status": manifest.get("overall_status", manifest.get("status", "unknown")),
                 "path": str(subdir)},
                "phase4",
            )
            rounds.append(enriched)

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
    """构建完整的 evidence bundle.

    核心原则：先读治理层索引，以治理认可的 artifact/round 为证据来源。
    只有在治理索引缺失时才 fallback 到目录扫描（并在 evidence_source 中标记）。
    """
    gov_root = project_root / "artifacts/governance"

    # 加载治理索引
    artifact_index = _safe_load_json(gov_root / "artifact_index.json")
    active_round_index = _safe_load_json(gov_root / "active_round_index.json")

    if artifact_index:
        log.info("使用治理层 artifact_index 作为 Phase 2 证据来源")
    else:
        log.warning("artifact_index.json 不存在，Phase 2 将 fallback 到目录扫描")

    if active_round_index:
        log.info("使用治理层 active_round_index 作为 Phase 3/4 证据来源")
    else:
        log.warning("active_round_index.json 不存在，Phase 3/4 将 fallback 到目录扫描")

    log.info("收集 Phase 2 证据...")
    p2 = collect_phase2_evidence(project_root, artifact_index=artifact_index)

    log.info("收集 Phase 3 证据...")
    p3 = collect_phase3_evidence(project_root, active_round_index=active_round_index)

    log.info("收集 Phase 4 证据...")
    p4 = collect_phase4_evidence(project_root, active_round_index=active_round_index)

    log.info("收集 Phase 5 治理证据...")
    p5 = collect_phase5_evidence(project_root)

    # 证据完整度
    phases_with_data = []
    if p2.get("experiment_count", 0) > 0 or p2.get("parameter_scan_count", 0) > 0:
        phases_with_data.append("phase2")
    if p3.get("trusted_round_count", 0) > 0:
        phases_with_data.append("phase3")
    if p4.get("trusted_round_count", 0) > 0:
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
        "governance_index_used": {
            "artifact_index": artifact_index is not None,
            "active_round_index": active_round_index is not None,
        },
        "phase2_evidence": p2,
        "phase3_evidence": p3,
        "phase4_evidence": p4,
        "phase5_governance_evidence": p5,
    }

    return bundle
