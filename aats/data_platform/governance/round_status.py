"""Round 生命周期管理 — Active Round Index.

统一 round / run 的生命周期：
  pending -> running -> succeeded / partial_success / failed -> deprecated

构建 active_round_index.json，记录当前 active / latest 的 round。
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

from .manifest_validation import normalize_legacy_manifest

log = logging.getLogger(__name__)

# ── 状态定义 ─────────────────────────────────────────────────────────

TERMINAL_STATUSES: set[str] = {"succeeded", "partial_success", "failed", "deprecated"}
ACTIVE_STATUSES: set[str] = {"pending", "running", "succeeded", "partial_success"}
RETRYABLE_STATUSES: set[str] = {"failed", "partial_success"}

# ── phase -> artifact root 映射 ──────────────────────────────────────

PHASE_ARTIFACT_ROOTS: dict[str, str] = {
    "phase3": "artifacts/research/attribution_rounds",
    "phase4": "artifacts/research/execution_rounds",
    "phase2_step1": "artifacts/research/calibration_batches",
    "phase2_step2": "artifacts/research/step2_rounds",
    "phase2_step3": "artifacts/research/step3_rounds",
}


# ── Round 信息提取 ───────────────────────────────────────────────────


def _extract_round_info(
    round_dir: pathlib.Path,
    *,
    phase: str,
) -> dict[str, Any] | None:
    """从 round 目录提取基本信息."""
    manifest_file = round_dir / "round_manifest.json"
    if not manifest_file.exists():
        return None

    try:
        with manifest_file.open(encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    manifest = normalize_legacy_manifest(raw, phase=phase)

    # combo 状态统计
    combos = manifest.get("combos", [])
    combo_statuses = {}
    for c in combos:
        key = c.get("key", "unknown")
        combo_statuses[key] = c.get("status", "unknown")

    return {
        "round_id": manifest.get("round_id", round_dir.name),
        "phase": phase,
        "path": str(round_dir),
        "status": manifest.get("status", "unknown"),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "scope": manifest.get("scope", {}),
        "combo_count": len(combos),
        "combo_statuses": combo_statuses,
    }


# ── Active Round Index 构建 ──────────────────────────────────────────


def build_active_round_index(
    project_root: pathlib.Path,
    *,
    phases: list[str] | None = None,
    include_deprecated: bool = False,
) -> dict[str, Any]:
    """扫描所有 round 目录，构建 active round 索引.

    只包含 active 状态的 round（默认排除 deprecated）。
    """
    all_rounds: list[dict[str, Any]] = []

    for phase, rel_path in PHASE_ARTIFACT_ROOTS.items():
        if phases and phase not in phases:
            continue

        root = project_root / rel_path
        if not root.exists():
            continue

        for subdir in sorted(root.iterdir()):
            if not subdir.is_dir():
                continue
            info = _extract_round_info(subdir, phase=phase)
            if info is None:
                continue
            # 过滤
            if not include_deprecated and info["status"] == "deprecated":
                continue
            all_rounds.append(info)

    # 按 started_at 降序排列
    all_rounds.sort(
        key=lambda r: r.get("started_at") or "",
        reverse=True,
    )

    # 按 phase 分组，每个 phase 取最近的 round 作为 latest
    latest_by_phase: dict[str, dict[str, Any]] = {}
    for r in all_rounds:
        phase = r["phase"]
        if phase not in latest_by_phase:
            latest_by_phase[phase] = r

    # 统计
    status_counts: dict[str, int] = {}
    for r in all_rounds:
        s = r["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_rounds": len(all_rounds),
            "status_distribution": status_counts,
            "phases_with_rounds": sorted(latest_by_phase.keys()),
        },
        "latest_by_phase": latest_by_phase,
        "all_rounds": all_rounds,
    }


def list_rounds_by_status(
    index: dict[str, Any],
    status: str,
) -> list[dict[str, Any]]:
    """从 index 中筛选特定状态的 round."""
    return [r for r in index.get("all_rounds", []) if r.get("status") == status]


def get_latest_round(index: dict[str, Any], phase: str) -> dict[str, Any] | None:
    """获取某 phase 最近一个 round."""
    return index.get("latest_by_phase", {}).get(phase)


# ── 也支持扫描 experiments（Step 1 单实验）────────────────────────────


def scan_experiments(
    project_root: pathlib.Path,
) -> list[dict[str, Any]]:
    """扫描 experiments/ 下的单实验目录.

    这些不是 round（没有 round_manifest.json），
    但仍需记录在 active index 中供治理参考。
    """
    exp_root = project_root / "artifacts/research/experiments"
    if not exp_root.exists():
        return []

    experiments: list[dict[str, Any]] = []
    for subdir in sorted(exp_root.iterdir()):
        if not subdir.is_dir():
            continue

        entry: dict[str, Any] = {
            "experiment_id": subdir.name,
            "phase": "phase2_step1",
            "path": str(subdir),
            "type": "experiment",
        }

        # 判断是否是参数扫描
        if (subdir / "comparison_summary.json").exists():
            entry["type"] = "parameter_scan"

        # 读 diagnostics
        diag_file = subdir / "diagnostics.json"
        if diag_file.exists():
            try:
                with diag_file.open(encoding="utf-8") as f:
                    diag = json.load(f)
                entry["opening_count"] = diag.get("opening_count")
                entry["positive_edge_ratio"] = diag.get("positive_edge_ratio")
            except Exception:
                pass

        experiments.append(entry)

    return experiments
