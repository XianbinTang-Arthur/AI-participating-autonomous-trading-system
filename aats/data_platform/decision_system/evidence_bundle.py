"""Phase 6-A: Evidence Bundle 统一化。"""

from __future__ import annotations

import json
import logging
import pathlib
import re
from datetime import datetime, timezone
from typing import Any

from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_STEP2,
    SNAPSHOT_ACTIVE_ROUND_INDEX,
    SNAPSHOT_ARTIFACT_INDEX,
    SNAPSHOT_QUALITY_MONITOR,
    is_snapshot_incomplete,
    load_governance_snapshot,
    load_research_round_snapshot,
    load_latest_research_round_snapshot,
)
from aats.data_platform.governance.parameter_registry import load_registry
from aats.data_platform.replay.diagnostics.replay_diagnostics import (
    extract_comparison_rows,
)

log = logging.getLogger(__name__)

COMBOS: list[dict[str, str]] = [
    {"key": "independent_15m", "family": "independent", "timeframe": "15m"},
    {"key": "independent_1h", "family": "independent", "timeframe": "1H"},
    {"key": "directional_15m", "family": "directional", "timeframe": "15m"},
    {"key": "directional_1h", "family": "directional", "timeframe": "1H"},
]

_UNTRUSTED_STATUSES: set[str] = {"deprecated", "failed"}
_ROUND_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")


def _safe_load_json(path: pathlib.Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to load JSON from %s: %s", path, exc)
        return None


def normalize_timeframe_value(timeframe: str | None) -> str | None:
    if timeframe is None:
        return None
    normalized = str(timeframe).strip().lower()
    if normalized in {"1h", "1hr", "1hour"}:
        return "1h"
    return normalized


def make_combo_key(family: str | None, timeframe: str | None) -> str | None:
    normalized_timeframe = normalize_timeframe_value(timeframe)
    if not family or not normalized_timeframe:
        return None
    return f"{family}_{normalized_timeframe}"


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build_phase2_diag_entry(
    raw: dict[str, Any],
    *,
    diag_id: str,
    diag_type: str,
    family: str | None = None,
    timeframe: str | None = None,
    label: str | None = None,
    scan_key: str | None = None,
    scan_run_id: str | None = None,
) -> dict[str, Any]:
    resolved_family = family or raw.get("family")
    resolved_timeframe = timeframe or raw.get("timeframe")
    combo_key = make_combo_key(resolved_family, resolved_timeframe)

    entry: dict[str, Any] = {
        "id": diag_id,
        "type": diag_type,
        "family": resolved_family,
        "timeframe": resolved_timeframe,
        "combo_key": combo_key,
        "total_bars": _coerce_int(raw.get("total_bars")),
        "opening_count": _coerce_int(raw.get("opening_count")),
        "positive_edge_ratio": _coerce_float(raw.get("positive_edge_ratio")) or 0.0,
        "mean_expected_edge_bps": _coerce_float(raw.get("mean_expected_edge_bps")),
        "execution_compatible_ratio": _coerce_float(
            raw.get("execution_compatible_ratio"),
        ),
        "selectable_ratio": _coerce_float(raw.get("selectable_ratio")),
    }
    if label or raw.get("label"):
        entry["label"] = label or raw.get("label")
    if scan_key:
        entry["scan_key"] = scan_key
    if scan_run_id:
        entry["scan_run_id"] = scan_run_id
    return entry


def _aggregate_phase2_stats(diags: list[dict[str, Any]]) -> dict[str, Any]:
    if not diags:
        return {
            "available": False,
            "total_experiments": 0,
            "experiments_with_openings": 0,
            "max_opening_count": 0,
            "mean_positive_edge_ratio": 0.0,
            "mean_expected_edge_bps": None,
            "mean_execution_compatible_ratio": None,
        }

    openings = [max(_coerce_int(d.get("opening_count")), 0) for d in diags]
    edge_ratios = [
        value
        for value in (_coerce_float(d.get("positive_edge_ratio")) for d in diags)
        if value is not None
    ]
    expected_edges = [
        value
        for value in (_coerce_float(d.get("mean_expected_edge_bps")) for d in diags)
        if value is not None
    ]
    exec_ratios = [
        value
        for value in (_coerce_float(d.get("execution_compatible_ratio")) for d in diags)
        if value is not None
    ]

    experiments_with_openings = sum(1 for opening in openings if opening > 0)
    # available 必须依赖真实开仓实验数：Phase2 扫描跑完但所有组合 opening_count=0
    # 属于"跑了但没证据"，不能让下游 selector / gate 把它当作"有证据可用"。
    return {
        "available": experiments_with_openings > 0,
        "total_experiments": len(diags),
        "experiments_with_openings": experiments_with_openings,
        "max_opening_count": max(openings) if openings else 0,
        "mean_positive_edge_ratio": round(
            sum(edge_ratios) / len(edge_ratios), 6,
        ) if edge_ratios else 0.0,
        "mean_expected_edge_bps": round(
            sum(expected_edges) / len(expected_edges), 6,
        ) if expected_edges else None,
        "mean_execution_compatible_ratio": round(
            sum(exec_ratios) / len(exec_ratios), 6,
        ) if exec_ratios else None,
    }


def get_phase2_combo_stats(
    evidence: dict[str, Any],
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    combo_key = make_combo_key(family, timeframe)
    combo_stats = evidence.get("combo_stats", {})
    if combo_key and combo_key in combo_stats:
        return combo_stats[combo_key]

    # 没有该 family/timeframe 的独立统计 → 必须返回 unavailable，而不是把全局
    # 聚合伪装成这一 combo 的证据：这是曾经出现过"global_stats 把无关 combo 误判为
    # 有证据可用"的故障根因。selector/gate 应基于 combo-specific 证据决策。
    fallback = _aggregate_phase2_stats([])
    fallback["family"] = family
    fallback["timeframe"] = timeframe
    fallback["combo_key"] = combo_key
    fallback["fallback_reason"] = "combo_stats_missing"
    return fallback


def _find_latest_round_dir(root: pathlib.Path) -> pathlib.Path | None:
    if not root.exists():
        return None
    round_dirs = [subdir for subdir in root.iterdir() if subdir.is_dir()]
    if not round_dirs:
        return None

    canonical_round_dirs = [
        subdir for subdir in round_dirs if _ROUND_DIR_PATTERN.match(subdir.name)
    ]
    if canonical_round_dirs:
        return sorted(canonical_round_dirs, key=lambda path: path.name)[-1]

    return sorted(round_dirs, key=lambda path: path.stat().st_mtime)[-1]


def _collect_latest_step2_round_diags(project_root: pathlib.Path) -> list[dict[str, Any]]:
    snapshot = load_latest_research_round_snapshot(
        phase=ROUND_PHASE_STEP2,
        project_root=project_root,
    )
    # 缺 round_manifest.json 的 Step2 目录（残留/半成品）不能进入 Phase 2 证据链，
    # 否则会让 collect_phase2_evidence / _aggregate_phase2_stats 把不完整目录
    # 当成"experiments_with_openings>=1"的可交易证据，污染 promotion readiness。
    if is_snapshot_incomplete(snapshot):
        log.warning(
            "Phase2 证据收集: Step2 最新 round snapshot 缺 round_manifest.json "
            "(round_id=%s)，按无可信证据处理",
            snapshot.get("round_id") if isinstance(snapshot, dict) else None,
        )
        return []
    if snapshot:
        diags: list[dict[str, Any]] = []
        summary = snapshot.get("summary", {}) or {}
        family_summary = summary.get("family_timeframe_summary", {}) or {}
        for index, item in enumerate(family_summary.get("experiments", [])):
            diags.append(
                _build_phase2_diag_entry(
                    item,
                    diag_id=f"{snapshot.get('round_id')}/calibration/{index}",
                    diag_type="calibration_experiment",
                ),
            )

        scan_summary = summary.get("scan_comparison_summary", {}) or {}
        for index, item in enumerate(extract_comparison_rows(scan_summary)):
            diags.append(
                _build_phase2_diag_entry(
                    item,
                    diag_id=f"{snapshot.get('round_id')}/scan/{index}",
                    diag_type="parameter_scan_item",
                    scan_key=item.get("scan_key"),
                    scan_run_id=item.get("scan_run_id"),
                ),
            )
        if diags:
            return diags

    return []


def _finalize_phase2_stats(
    evidence: dict[str, Any],
    all_diags: list[dict[str, Any]],
) -> None:
    combo_diags: dict[str, list[dict[str, Any]]] = {}
    for diag in all_diags:
        combo_key = diag.get("combo_key")
        if combo_key:
            combo_diags.setdefault(combo_key, []).append(diag)

    combo_stats: dict[str, Any] = {}
    for combo in COMBOS:
        combo_key = combo["key"]
        stats = _aggregate_phase2_stats(combo_diags.get(combo_key, []))
        stats["family"] = combo["family"]
        stats["timeframe"] = combo["timeframe"]
        stats["combo_key"] = combo_key
        combo_stats[combo_key] = stats

    global_stats = _aggregate_phase2_stats(all_diags)
    evidence["combo_stats"] = combo_stats
    evidence["global_stats"] = global_stats
    evidence["aggregate_stats"] = global_stats
    evidence["best_experiments"] = sorted(
        all_diags,
        key=lambda item: item.get("opening_count", 0),
        reverse=True,
    )[:3]


def collect_phase2_evidence(
    project_root: pathlib.Path,
    *,
    artifact_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """收集 Phase 2 (Step 1/2) 研究证据。"""
    evidence: dict[str, Any] = {
        "source": "phase2",
        "evidence_source": "governance_index" if artifact_index else "directory_scan",
        "experiment_count": 0,
        "parameter_scan_count": 0,
        "experiments": [],
        "best_experiments": [],
        "combo_stats": {},
        "global_stats": {},
        "aggregate_stats": {},
    }

    all_diags: list[dict[str, Any]] = []
    canonical_step2_diags = _collect_latest_step2_round_diags(project_root)
    if canonical_step2_diags:
        all_diags.extend(canonical_step2_diags)

    if artifact_index:
        for artifact in artifact_index.get("artifacts", []):
            if artifact.get("phase") not in ("phase2_step1", "phase2_step2"):
                continue

            artifact_type = artifact.get("artifact_type", "experiment")
            artifact_path = pathlib.Path(artifact.get("path", ""))

            if artifact_type == "parameter_scan":
                evidence["parameter_scan_count"] += 1
                comp = _safe_load_json(artifact_path / "comparison_summary.json")
                if not isinstance(comp, dict):
                    continue
                comparison = extract_comparison_rows(comp)
                evidence["experiments"].append({
                    "id": artifact["artifact_id"],
                    "type": "parameter_scan",
                    "experiment_count": comp.get("experiment_count"),
                    "comparison": comparison,
                })
                if not canonical_step2_diags:
                    for index, item in enumerate(comparison):
                        all_diags.append(
                            _build_phase2_diag_entry(
                                item,
                                diag_id=f"{artifact['artifact_id']}/scan/{index}",
                                diag_type="parameter_scan_item",
                                family=artifact.get("family"),
                                timeframe=artifact.get("timeframe"),
                                scan_key=artifact.get("artifact_id"),
                                scan_run_id=artifact.get("artifact_id"),
                            ),
                        )
                continue

            summary = artifact.get("diagnostics_summary")
            if not summary:
                continue
            evidence["experiment_count"] += 1
            diag = _safe_load_json(artifact_path / "diagnostics.json")
            payload = dict(summary)
            if isinstance(diag, dict):
                payload.update(diag)
            entry = _build_phase2_diag_entry(
                payload,
                diag_id=artifact["artifact_id"],
                diag_type="experiment",
                family=artifact.get("family"),
                timeframe=artifact.get("timeframe"),
            )
            evidence["experiments"].append(entry)
            all_diags.append(entry)
    else:
        log.warning("artifact_index 不存在，fallback 到目录扫描")
        exp_root = project_root / "artifacts/research/experiments"
        if exp_root.exists():
            for subdir in sorted(exp_root.iterdir()):
                if not subdir.is_dir():
                    continue

                comp_file = subdir / "comparison_summary.json"
                if comp_file.exists():
                    evidence["parameter_scan_count"] += 1
                    comp = _safe_load_json(comp_file)
                    if not isinstance(comp, dict):
                        continue
                    comparison = extract_comparison_rows(comp)
                    evidence["experiments"].append({
                        "id": subdir.name,
                        "type": "parameter_scan",
                        "experiment_count": comp.get("experiment_count"),
                        "comparison": comparison,
                    })
                    if not canonical_step2_diags:
                        for index, item in enumerate(comparison):
                            all_diags.append(
                                _build_phase2_diag_entry(
                                    item,
                                    diag_id=f"{subdir.name}/scan/{index}",
                                    diag_type="parameter_scan_item",
                                    scan_key=subdir.name,
                                    scan_run_id=subdir.name,
                                ),
                            )
                    continue

                diag = _safe_load_json(subdir / "diagnostics.json")
                if not isinstance(diag, dict):
                    continue
                evidence["experiment_count"] += 1
                entry = _build_phase2_diag_entry(
                    diag,
                    diag_id=subdir.name,
                    diag_type="experiment",
                )
                evidence["experiments"].append(entry)
                all_diags.append(entry)

    _finalize_phase2_stats(evidence, all_diags)
    return evidence


def _collect_round_evidence_from_index(
    active_round_index: dict[str, Any],
    phase: str,
) -> list[dict[str, Any]]:
    trusted: list[dict[str, Any]] = []
    for round_info in active_round_index.get("all_rounds", []):
        if round_info.get("phase") != phase:
            continue
        if round_info.get("status") in _UNTRUSTED_STATUSES:
            continue
        trusted.append(round_info)
    return trusted


def _enrich_round_from_manifest(
    round_info: dict[str, Any],
    phase: str,
    project_root: pathlib.Path,
) -> dict[str, Any]:
    round_id = round_info.get("round_id")
    snapshot = (
        load_research_round_snapshot(round_id=round_id, project_root=project_root)
        if round_id else None
    )
    if snapshot:
        summary = snapshot.get("summary", {}) or {}
        manifest_payload = snapshot.get("manifest", {}) or {}
        combos = summary.get("combos", {}) or {}
        enriched: dict[str, Any] = {
            "round_id": snapshot.get("round_id"),
            "started_at": snapshot.get("started_at"),
            "status": snapshot.get("status", "unknown"),
            "replay_only": bool(snapshot.get("replay_only", False)),
            "live_query_succeeded": bool(
                manifest_payload.get("live_query_succeeded", False)
            ),
            "combos": {},
        }
        for key, combo in combos.items():
            combo_data: dict[str, Any] = {"status": combo.get("status", "unknown")}
            if phase == "phase3":
                combo_data["live_query_succeeded"] = bool(
                    combo.get("live_query_succeeded", False)
                )
                if combo.get("alignment_stats") is not None:
                    combo_data["alignment_stats"] = combo.get("alignment_stats")
                if combo.get("attribution_summary") is not None:
                    combo_data["attribution_summary"] = combo.get("attribution_summary")
                if combo.get("top_failure_modes") is not None:
                    combo_data["top_failure_modes"] = combo.get("top_failure_modes")
            elif phase == "phase4" and combo.get("cost_summary") is not None:
                combo_data["cost_summary"] = combo.get("cost_summary")
            enriched["combos"][key] = combo_data
        return enriched

    round_dir = pathlib.Path(round_info.get("path", ""))
    manifest = _safe_load_json(round_dir / "round_manifest.json")
    if not isinstance(manifest, dict):
        return round_info

    enriched: dict[str, Any] = {
        "round_id": round_info.get("round_id", round_dir.name),
        "started_at": round_info.get("started_at"),
        "status": round_info.get("status", manifest.get("overall_status", "unknown")),
        "replay_only": bool(manifest.get("replay_only", False)),
        "live_query_succeeded": bool(manifest.get("live_query_succeeded", False)),
        "combos": {},
    }

    for combo in manifest.get("combos", []):
        key = combo.get("key", "?")
        combo_data: dict[str, Any] = {"status": combo.get("status", "unknown")}
        if phase == "phase3":
            combo_data["live_query_succeeded"] = bool(
                combo.get("live_query_succeeded", False)
            )
            if combo.get("alignment_stats") is not None:
                combo_data["alignment_stats"] = combo.get("alignment_stats")
        run_dir = combo.get("run_dir")
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
        trusted = _collect_round_evidence_from_index(active_round_index, "phase3")
        evidence["trusted_round_count"] = len(trusted)
        all_rounds = [
            round_info for round_info in active_round_index.get("all_rounds", [])
            if round_info.get("phase") == "phase3"
        ]
        evidence["round_count"] = len(all_rounds)
        evidence["skipped_untrusted"] = len(all_rounds) - len(trusted)
        for round_info in trusted:
            rounds.append(_enrich_round_from_manifest(round_info, "phase3", project_root))
    else:
        log.warning("active_round_index 不存在，Phase 3 fallback 到目录扫描")
        attr_root = project_root / "artifacts/research/attribution_rounds"
        if attr_root.exists():
            for subdir in sorted(attr_root.iterdir()):
                if not subdir.is_dir():
                    continue
                manifest = _safe_load_json(subdir / "round_manifest.json")
                if not isinstance(manifest, dict):
                    continue
                evidence["round_count"] += 1
                round_status = manifest.get("overall_status", manifest.get("status"))
                if round_status in _UNTRUSTED_STATUSES:
                    evidence["skipped_untrusted"] += 1
                    continue
                evidence["trusted_round_count"] += 1
                rounds.append(
                    _enrich_round_from_manifest(
                        {
                            "round_id": manifest.get("round_id", subdir.name),
                            "started_at": manifest.get("started_at"),
                            "status": manifest.get("overall_status", manifest.get("status", "unknown")),
                            "path": str(subdir),
                        },
                        "phase3",
                        project_root,
                    ),
                )

    if rounds:
        rounds.sort(key=lambda item: item.get("started_at") or "", reverse=True)
        evidence["latest_round"] = rounds[0]
    return evidence


def collect_phase4_evidence(
    project_root: pathlib.Path,
    *,
    active_round_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        all_rounds = [
            round_info for round_info in active_round_index.get("all_rounds", [])
            if round_info.get("phase") == "phase4"
        ]
        evidence["round_count"] = len(all_rounds)
        evidence["skipped_untrusted"] = len(all_rounds) - len(trusted)
        for round_info in trusted:
            rounds.append(_enrich_round_from_manifest(round_info, "phase4", project_root))
    else:
        log.warning("active_round_index 不存在，Phase 4 fallback 到目录扫描")
        exec_root = project_root / "artifacts/research/execution_rounds"
        if exec_root.exists():
            for subdir in sorted(exec_root.iterdir()):
                if not subdir.is_dir():
                    continue
                manifest = _safe_load_json(subdir / "round_manifest.json")
                if not isinstance(manifest, dict):
                    continue
                evidence["round_count"] += 1
                round_status = manifest.get("overall_status", manifest.get("status"))
                if round_status in _UNTRUSTED_STATUSES:
                    evidence["skipped_untrusted"] += 1
                    continue
                evidence["trusted_round_count"] += 1
                rounds.append(
                    _enrich_round_from_manifest(
                        {
                            "round_id": manifest.get("round_id", subdir.name),
                            "started_at": manifest.get("started_at"),
                            "status": manifest.get("overall_status", manifest.get("status", "unknown")),
                            "path": str(subdir),
                        },
                        "phase4",
                        project_root,
                    ),
                )

    if rounds:
        rounds.sort(key=lambda item: item.get("started_at") or "", reverse=True)
        evidence["latest_round"] = rounds[0]
    return evidence


def collect_phase5_evidence(project_root: pathlib.Path) -> dict[str, Any]:
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

    artifact_index = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_ARTIFACT_INDEX,
    )
    if artifact_index:
        evidence["artifact_index_exists"] = True
        evidence["total_artifacts"] = artifact_index.get("summary", {}).get(
            "total_artifacts", 0,
        )

    gov_root = project_root / "artifacts/governance"
    registry_path = gov_root / "current_parameter_registry.json"
    try:
        registry = load_registry(registry_path)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to load parameter registry via DB-first path: %s", exc)
        registry = _safe_load_json(registry_path)
    if isinstance(registry, dict):
        evidence["parameter_registry_exists"] = True
        for parameter_set in registry.get("parameter_sets", []):
            if parameter_set.get("status") == "frozen":
                evidence["frozen_parameter_sets"].append({
                    "parameter_set_id": parameter_set["parameter_set_id"],
                    "family": parameter_set["family"],
                    "timeframe": parameter_set["timeframe"],
                    "values": parameter_set.get("values", {}),
                    "frozen_at": parameter_set.get("frozen_at"),
                })
            elif parameter_set.get("status") == "candidate":
                evidence["candidate_parameter_sets"].append({
                    "parameter_set_id": parameter_set["parameter_set_id"],
                    "family": parameter_set["family"],
                    "timeframe": parameter_set["timeframe"],
                    "values": parameter_set.get("values", {}),
                })

    quality_monitor = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_QUALITY_MONITOR,
    )
    if isinstance(quality_monitor, dict):
        evidence["quality_monitor_exists"] = True
        summary = quality_monitor.get("summary", {})
        evidence["quality_health"] = summary.get("health")
        evidence["critical_failures"] = summary.get("critical_failures", 0)

    return evidence


def build_evidence_bundle(project_root: pathlib.Path) -> dict[str, Any]:
    artifact_index = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_ARTIFACT_INDEX,
    )
    active_round_index = load_governance_snapshot(
        project_root,
        snapshot_type=SNAPSHOT_ACTIVE_ROUND_INDEX,
    )

    if artifact_index:
        log.info("使用治理层 artifact_index 作为 Phase 2 证据来源")
    else:
        log.warning("artifact_index.json 不存在，Phase 2 将 fallback 到目录扫描")

    if active_round_index:
        log.info("使用治理层 active_round_index 作为 Phase 3/4 证据来源")
    else:
        log.warning("active_round_index.json 不存在，Phase 3/4 将 fallback 到目录扫描")

    p2 = collect_phase2_evidence(project_root, artifact_index=artifact_index)
    p3 = collect_phase3_evidence(project_root, active_round_index=active_round_index)
    p4 = collect_phase4_evidence(project_root, active_round_index=active_round_index)
    p5 = collect_phase5_evidence(project_root)

    phases_with_data: list[str] = []
    if p2.get("experiment_count", 0) > 0 or p2.get("parameter_scan_count", 0) > 0:
        phases_with_data.append("phase2")
    if p3.get("trusted_round_count", 0) > 0:
        phases_with_data.append("phase3")
    if p4.get("trusted_round_count", 0) > 0:
        phases_with_data.append("phase4")
    if p5.get("artifact_index_exists"):
        phases_with_data.append("phase5")

    return {
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
