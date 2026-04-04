"""Artifact 索引构建.

扫描 artifacts/ 目录，收集所有 round / run / experiment 的元信息，
生成统一的 artifact_index.json。
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

from .manifest_validation import (
    normalize_legacy_manifest,
    validate_manifest,
)

log = logging.getLogger(__name__)

# ── 已知 artifact 根目录 ─────────────────────────────────────────────

KNOWN_ARTIFACT_ROOTS: dict[str, dict[str, str]] = {
    "experiments": {
        "path": "artifacts/research/experiments",
        "phase": "phase2_step1",
        "description": "Step 1 / Step 2 单实验或参数扫描",
    },
    "calibration_batches": {
        "path": "artifacts/research/calibration_batches",
        "phase": "phase2_step1",
        "description": "Step 1 批量校准",
    },
    "calibration_rounds": {
        "path": "artifacts/research/calibration_rounds",
        "phase": "phase2_step2",
        "description": "Step 2 研究 round",
    },
    "attribution_rounds": {
        "path": "artifacts/research/attribution_rounds",
        "phase": "phase3",
        "description": "Phase 3 归因 round",
    },
    "execution_rounds": {
        "path": "artifacts/research/execution_rounds",
        "phase": "phase4",
        "description": "Phase 4 执行代理评估 round",
    },
}

# 实验级别的关键文件
EXPERIMENT_KEY_FILES: list[str] = [
    "diagnostics.json",
    "replay_decisions.csv",
    "report.md",
]

# Round 级别的关键文件
ROUND_KEY_FILES: list[str] = [
    "round_manifest.json",
]


# ── 索引项结构 ───────────────────────────────────────────────────────


def _scan_experiment_dir(exp_dir: pathlib.Path, *, phase: str) -> dict[str, Any]:
    """扫描一个实验目录，收集元信息."""
    entry: dict[str, Any] = {
        "artifact_id": exp_dir.name,
        "artifact_type": "experiment",
        "phase": phase,
        "path": str(exp_dir),
        "files": [],
        "missing_files": [],
        "sub_experiments": [],
        "has_manifest": False,
        "has_diagnostics": False,
        "has_report": False,
    }

    # 列出文件
    if exp_dir.exists():
        for item in sorted(exp_dir.iterdir()):
            if item.is_file():
                entry["files"].append(item.name)
            elif item.is_dir():
                # 子实验（参数扫描的 combo）
                entry["sub_experiments"].append(item.name)

    # 关键文件检查
    for kf in EXPERIMENT_KEY_FILES:
        if kf in entry["files"]:
            if kf == "diagnostics.json":
                entry["has_diagnostics"] = True
            elif kf == "report.md":
                entry["has_report"] = True
        else:
            # 子实验可能有
            found_in_sub = any(
                (exp_dir / sub / kf).exists()
                for sub in entry["sub_experiments"]
            )
            if not found_in_sub:
                entry["missing_files"].append(kf)

    # 读 diagnostics 摘要
    diag_file = exp_dir / "diagnostics.json"
    if diag_file.exists():
        try:
            with diag_file.open(encoding="utf-8") as f:
                diag = json.load(f)
            entry["diagnostics_summary"] = {
                "total_bars": diag.get("total_bars"),
                "opening_count": diag.get("opening_count"),
                "positive_edge_ratio": diag.get("positive_edge_ratio"),
            }
        except Exception as exc:
            log.warning("Failed to read diagnostics.json in %s: %s", exp_dir, exc)
            entry["diagnostics_summary"] = None

    # 读 comparison_summary
    comp_file = exp_dir / "comparison_summary.json"
    if comp_file.exists():
        entry["artifact_type"] = "parameter_scan"
        entry["has_comparison"] = True
        try:
            with comp_file.open(encoding="utf-8") as f:
                comp = json.load(f)
            entry["experiment_count"] = comp.get("experiment_count")
        except Exception as exc:
            log.warning("Failed to read comparison_summary.json in %s: %s", exp_dir, exc)

    return entry


def _scan_round_dir(round_dir: pathlib.Path, *, phase: str) -> dict[str, Any]:
    """扫描一个 round 目录，收集元信息."""
    entry: dict[str, Any] = {
        "artifact_id": round_dir.name,
        "artifact_type": "round",
        "phase": phase,
        "path": str(round_dir),
        "files": [],
        "missing_files": [],
        "has_manifest": False,
        "manifest": None,
        "manifest_validation": None,
        "status": None,
        "started_at": None,
        "finished_at": None,
    }

    if not round_dir.exists():
        return entry

    for item in sorted(round_dir.iterdir()):
        if item.is_file():
            entry["files"].append(item.name)

    # round_manifest.json
    manifest_file = round_dir / "round_manifest.json"
    if manifest_file.exists():
        entry["has_manifest"] = True
        try:
            with manifest_file.open(encoding="utf-8") as f:
                raw_manifest = json.load(f)
            # 规范化
            manifest = normalize_legacy_manifest(raw_manifest, phase=phase)
            entry["manifest"] = manifest
            entry["status"] = manifest.get("status")
            entry["started_at"] = manifest.get("started_at")
            entry["finished_at"] = manifest.get("finished_at")
            # 校验
            vr = validate_manifest(manifest, path=str(manifest_file))
            entry["manifest_validation"] = {
                "is_valid": vr.is_valid,
                "error_count": vr.error_count,
                "warning_count": vr.warning_count,
            }
        except Exception as exc:
            entry["manifest_validation"] = {
                "is_valid": False,
                "error_count": 1,
                "warning_count": 0,
                "parse_error": str(exc),
            }
    else:
        entry["missing_files"].append("round_manifest.json")

    return entry


# ── 主扫描入口 ───────────────────────────────────────────────────────


def build_artifact_index(
    project_root: pathlib.Path,
    *,
    phases: list[str] | None = None,
) -> dict[str, Any]:
    """扫描所有已知 artifact 目录，构建索引.

    Parameters
    ----------
    project_root : pathlib.Path
        项目根目录
    phases : list[str] | None
        限定扫描的 phase（如 ["phase3", "phase4"]），None = 全部

    Returns
    -------
    dict  artifact_index.json 内容
    """
    entries: list[dict[str, Any]] = []
    scan_summary: dict[str, int] = {}

    for root_key, root_info in KNOWN_ARTIFACT_ROOTS.items():
        phase = root_info["phase"]
        if phases and phase not in phases:
            continue

        root_path = project_root / root_info["path"]
        if not root_path.exists():
            log.info("跳过不存在的目录: %s", root_path)
            scan_summary[root_key] = 0
            continue

        subdirs = sorted(
            [d for d in root_path.iterdir() if d.is_dir()],
            key=lambda p: p.name,
        )
        count = 0

        for subdir in subdirs:
            # 判断是 round 还是 experiment
            has_manifest = (subdir / "round_manifest.json").exists()

            if has_manifest or root_key in ("attribution_rounds", "execution_rounds", "calibration_rounds"):
                entry = _scan_round_dir(subdir, phase=phase)
            else:
                entry = _scan_experiment_dir(subdir, phase=phase)

            entry["root_category"] = root_key
            entries.append(entry)
            count += 1

        scan_summary[root_key] = count
        log.info("扫描 %s: %d 个 artifact", root_key, count)

    # 统计
    total = len(entries)
    rounds = sum(1 for e in entries if e["artifact_type"] == "round")
    experiments = sum(1 for e in entries if e["artifact_type"] in ("experiment", "parameter_scan"))
    with_manifest = sum(1 for e in entries if e.get("has_manifest"))
    valid_manifests = sum(
        1 for e in entries
        if e.get("manifest_validation", {}).get("is_valid")
    )

    index: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "summary": {
            "total_artifacts": total,
            "rounds": rounds,
            "experiments": experiments,
            "with_manifest": with_manifest,
            "valid_manifests": valid_manifests,
            "by_category": scan_summary,
        },
        "artifacts": entries,
    }

    return index
