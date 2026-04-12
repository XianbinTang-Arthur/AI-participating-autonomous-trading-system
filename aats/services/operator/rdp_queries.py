"""RDP 治理与决策数据查询服务.

为 Operator API 和 UI 提供 RDP 子系统的只读查询：
  - 当前 active parameter sets
  - 最近 attribution 结论
  - 最近 execution realism 结论
  - 当前 family/timeframe 决策状态
  - 最近 recommendations
  - RDP 子系统健康状态
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── 路径常量 ───────────────────────────────────────────────────────

_GOVERNANCE_DIR = "artifacts/governance"
_DECISION_SYSTEM_DIR = "artifacts/decision_system"
_DECISION_ROUNDS_DIR = "artifacts/decision_rounds"
_ATTRIBUTION_ROUNDS_DIR = "artifacts/research/attribution_rounds"
_EXECUTION_ROUNDS_DIR = "artifacts/research/execution_rounds"
_EXPERIMENTS_DIR = "artifacts/research/experiments"


# ── 工具函数 ───────────────────────────────────────────────────────


def _safe_load_json(path: Path) -> dict | list | None:
    """安全加载 JSON 文件，失败返回 None."""
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("无法加载 %s: %s", path, exc)
        return None


def _find_latest_round_dir(rounds_root: Path) -> Path | None:
    """查找最近的 round 目录（按名称排序，round_id 含时间戳）."""
    if not rounds_root.exists():
        return None
    dirs = sorted(
        (d for d in rounds_root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )
    return dirs[0] if dirs else None


# ── 1. Active Parameter Sets ──────────────────────────────────────


def query_active_parameter_sets(project_root: Path) -> dict[str, Any]:
    """查询当前 active parameter sets.

    读取 configs/active_parameter_sets/ 目录下的所有 JSON 文件。
    """
    from aats.bootstrap.active_parameters import get_active_parameter_summary
    return get_active_parameter_summary(project_root=project_root)


# ── 2. Latest Attribution ─────────────────────────────────────────


def query_latest_attribution(project_root: Path) -> dict[str, Any]:
    """查询最近一次 attribution round 结论.

    读取:
      - artifacts/research/attribution_rounds/<latest>/round_manifest.json
      - artifacts/research/attribution_rounds/<latest>/attribution_report.md
      - artifacts/research/attribution_rounds/<latest>/attribution_summary.json
    """
    rounds_root = project_root / _ATTRIBUTION_ROUNDS_DIR
    latest_dir = _find_latest_round_dir(rounds_root)

    result: dict[str, Any] = {
        "available": False,
        "round_id": None,
        "round_dir": None,
        "manifest": None,
        "summary": None,
    }

    if latest_dir is None:
        return result

    result["round_dir"] = str(latest_dir)
    result["round_id"] = latest_dir.name

    # manifest
    manifest = _safe_load_json(latest_dir / "round_manifest.json")
    if manifest:
        result["manifest"] = {
            "round_id": manifest.get("round_id"),
            "status": manifest.get("status"),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "scope": manifest.get("scope"),
        }

    # summary
    summary = _safe_load_json(latest_dir / "attribution_summary.json")
    if summary:
        result["summary"] = summary
        result["available"] = True

    # 尝试读 combo 级结果
    combos: list[dict[str, Any]] = []
    for combo_dir in sorted(latest_dir.iterdir()):
        if not combo_dir.is_dir():
            continue
        combo_summary = _safe_load_json(combo_dir / "attribution_summary.json")
        if combo_summary:
            combos.append({
                "combo_key": combo_dir.name,
                "summary": combo_summary,
            })
    if combos:
        result["combos"] = combos
        result["available"] = True

    return result


# ── 3. Latest Execution Realism ───────────────────────────────────


def query_latest_execution_realism(project_root: Path) -> dict[str, Any]:
    """查询最近一次 execution realism round 结论.

    读取:
      - artifacts/research/execution_rounds/<latest>/round_manifest.json
      - artifacts/research/execution_rounds/<latest>/execution_summary.json
    """
    rounds_root = project_root / _EXECUTION_ROUNDS_DIR
    latest_dir = _find_latest_round_dir(rounds_root)

    result: dict[str, Any] = {
        "available": False,
        "round_id": None,
        "round_dir": None,
        "manifest": None,
        "summary": None,
    }

    if latest_dir is None:
        return result

    result["round_dir"] = str(latest_dir)
    result["round_id"] = latest_dir.name

    manifest = _safe_load_json(latest_dir / "round_manifest.json")
    if manifest:
        result["manifest"] = {
            "round_id": manifest.get("round_id"),
            "status": manifest.get("status"),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "scope": manifest.get("scope"),
        }

    summary = _safe_load_json(latest_dir / "execution_summary.json")
    if summary:
        result["summary"] = summary
        result["available"] = True

    combos: list[dict[str, Any]] = []
    for combo_dir in sorted(latest_dir.iterdir()):
        if not combo_dir.is_dir():
            continue
        combo_summary = _safe_load_json(combo_dir / "execution_summary.json")
        if combo_summary:
            combos.append({
                "combo_key": combo_dir.name,
                "summary": combo_summary,
            })
    if combos:
        result["combos"] = combos
        result["available"] = True

    return result


# ── 4. Family/Timeframe Decisions ─────────────────────────────────


def query_latest_decisions(project_root: Path) -> dict[str, Any]:
    """查询当前 family/timeframe 决策状态.

    优先级: DB → 文件 fallback（复用 recommendation_registry 的 DB-first loader）。
    """
    dec_path = project_root / _DECISION_SYSTEM_DIR / "active_decision_registry.json"

    # 复用 DB-first loader
    try:
        from aats.data_platform.decision_system.recommendation_registry import (
            load_active_decision_registry,
        )
        registry = load_active_decision_registry(dec_path)
    except Exception:
        registry = _safe_load_json(dec_path)

    result: dict[str, Any] = {
        "available": False,
        "registry_path": str(dec_path),
        "generated_at": None,
        "version": None,
        "decisions": [],
    }

    if registry is None:
        return result

    result["available"] = True
    result["generated_at"] = registry.get("generated_at")
    result["version"] = registry.get("version")
    result["decisions"] = registry.get("decisions", [])

    # 按状态统计
    status_counts: dict[str, int] = {}
    for d in result["decisions"]:
        s = d.get("current_status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
    result["status_distribution"] = status_counts

    return result


# ── 5. Latest Recommendations ─────────────────────────────────────


def query_latest_recommendations(
    project_root: Path,
    *,
    limit: int = 20,
    status_filter: str | None = None,
) -> dict[str, Any]:
    """查询最近的 recommendations.

    优先级: DB → 文件 fallback（复用 recommendation_registry 的 DB-first loader）。
    """
    rec_path = project_root / _DECISION_SYSTEM_DIR / "recommendation_registry.json"

    # 复用 DB-first loader
    try:
        from aats.data_platform.decision_system.recommendation_registry import (
            load_recommendation_registry,
        )
        registry = load_recommendation_registry(rec_path)
    except Exception:
        registry = _safe_load_json(rec_path)

    result: dict[str, Any] = {
        "available": False,
        "registry_path": str(rec_path),
        "generated_at": None,
        "version": None,
        "total_count": 0,
        "recommendations": [],
    }

    if registry is None:
        return result

    result["available"] = True
    result["generated_at"] = registry.get("generated_at")
    result["version"] = registry.get("version")

    recs = registry.get("recommendations", [])
    result["total_count"] = len(recs)

    # 过滤
    if status_filter:
        recs = [r for r in recs if r.get("status") == status_filter]

    # 按时间倒序取最近 N 条
    recs_sorted = sorted(
        recs, key=lambda r: r.get("created_at", ""), reverse=True,
    )
    result["recommendations"] = recs_sorted[:limit]

    # 状态分布
    status_counts: dict[str, int] = {}
    for r in registry.get("recommendations", []):
        s = r.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
    result["status_distribution"] = status_counts

    return result


# ── 6. RDP Health ─────────────────────────────────────────────────


def query_rdp_health(project_root: Path) -> dict[str, Any]:
    """查询 RDP 子系统整体健康状态.

    综合检查:
      - 治理层文件（artifact_index, parameter_registry, quality_monitor_summary）
      - 决策层文件（recommendation_registry, active_decision_registry）
      - 最近 round 时间
    """
    checks: list[dict[str, Any]] = []

    # 治理层文件
    governance_files = {
        "artifact_index": project_root / _GOVERNANCE_DIR / "artifact_index.json",
        "active_round_index": project_root / _GOVERNANCE_DIR / "active_round_index.json",
        "parameter_registry": project_root / _GOVERNANCE_DIR / "current_parameter_registry.json",
        "quality_monitor": project_root / _GOVERNANCE_DIR / "quality_monitor_summary.json",
    }

    for name, path in governance_files.items():
        data = _safe_load_json(path)
        checks.append({
            "category": "governance",
            "name": name,
            "exists": path.exists(),
            "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
        })

    # 决策层文件
    decision_files = {
        "recommendation_registry": project_root / _DECISION_SYSTEM_DIR / "recommendation_registry.json",
        "active_decision_registry": project_root / _DECISION_SYSTEM_DIR / "active_decision_registry.json",
        "evidence_bundle_index": project_root / _DECISION_SYSTEM_DIR / "evidence_bundle_index.json",
    }

    for name, path in decision_files.items():
        data = _safe_load_json(path)
        checks.append({
            "category": "decision_system",
            "name": name,
            "exists": path.exists(),
            "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
        })

    # active parameter sets
    from aats.bootstrap.active_parameters import load_all_active_parameter_sets
    active_sets = load_all_active_parameter_sets(project_root=project_root)
    checks.append({
        "category": "parameters",
        "name": "active_parameter_sets",
        "exists": bool(active_sets),
        "count": len(active_sets),
    })

    # quality monitor 摘要
    qm = _safe_load_json(
        project_root / _GOVERNANCE_DIR / "quality_monitor_summary.json",
    )
    qm_health = "unknown"
    if isinstance(qm, dict):
        qm_health = qm.get("health", "unknown")

    # 综合判定
    governance_ok = all(
        c["exists"] for c in checks if c["category"] == "governance"
    )
    decision_ok = all(
        c["exists"] for c in checks if c["category"] == "decision_system"
    )

    if governance_ok and decision_ok and qm_health != "unhealthy":
        overall = "healthy"
    elif governance_ok or decision_ok:
        overall = "degraded"
    else:
        overall = "not_initialized"

    return {
        "overall_health": overall,
        "quality_monitor_health": qm_health,
        "governance_initialized": governance_ok,
        "decision_system_initialized": decision_ok,
        "active_parameter_count": len(active_sets),
        "checks": checks,
    }


# ── 7. Latest Decision Round ─────────────────────────────────────


def query_latest_decision_round(project_root: Path) -> dict[str, Any]:
    """查询最近一次 decision round 结论.

    读取 artifacts/decision_rounds/<latest>/ 下的结论文件。
    """
    rounds_root = project_root / _DECISION_ROUNDS_DIR
    latest_dir = _find_latest_round_dir(rounds_root)

    result: dict[str, Any] = {
        "available": False,
        "round_id": None,
        "round_dir": None,
    }

    if latest_dir is None:
        return result

    result["round_id"] = latest_dir.name
    result["round_dir"] = str(latest_dir)
    result["available"] = True

    # 加载各类结论文件
    for filename in [
        "evidence_bundle_summary.json",
        "parameter_upgrade_candidates.json",
        "family_timeframe_decisions.json",
        "promotion_readiness_assessment.json",
    ]:
        data = _safe_load_json(latest_dir / filename)
        key = filename.replace(".json", "")
        result[key] = data

    # 检查结论报告
    conclusion_md = latest_dir / "phase6_closed_loop_decision_conclusion.md"
    result["has_conclusion_report"] = conclusion_md.exists()

    return result


# ── 8. Promotion Readiness ────────────────────────────────────────


def query_promotion_readiness(project_root: Path) -> dict[str, Any]:
    """查询最近一次 promotion readiness 评估.

    从最近 decision round 中提取。
    """
    dr = query_latest_decision_round(project_root)
    if not dr.get("available"):
        return {"available": False}

    readiness = dr.get("promotion_readiness_assessment")
    if readiness is None:
        return {"available": False, "round_id": dr.get("round_id")}

    return {
        "available": True,
        "round_id": dr.get("round_id"),
        "assessment": readiness,
    }
