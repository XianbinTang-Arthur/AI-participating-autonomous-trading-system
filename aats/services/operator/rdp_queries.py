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
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from aats.data_platform.governance.snapshot_db import (
    ROUND_PHASE_PHASE3,
    ROUND_PHASE_PHASE4,
    SNAPSHOT_ACTIVE_ROUND_INDEX,
    SNAPSHOT_ARTIFACT_INDEX,
    SNAPSHOT_QUALITY_MONITOR,
    is_snapshot_incomplete,
    load_governance_snapshot,
    load_latest_research_round_snapshot,
)

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


def _parse_iso_datetime(value: str | None) -> datetime | None:
    """Run-aggregation timestamp parse; illegal → None to avoid skipping siblings.

    Governance-critical reads must use :func:`parse_iso_datetime_utc` directly
    so illegal inputs raise rather than silently degrade.
    """
    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    try:
        return parse_iso_datetime_utc(value, context="rdp_queries.run_timestamp")
    except ValueError:
        return None


def _collect_latest_workflow_runs(project_root: Path) -> dict[str, dict[str, Any]]:
    """Use the same managed workflow truth contract as the capital Gate."""
    from aats.data_platform.governance._exceptions import DBUnavailableError
    from aats.data_platform.production_workflow.gate_runtime_contract import (
        _collect_latest_workflow_runs as collect_gate_workflow_runs,
    )

    try:
        return collect_gate_workflow_runs(project_root)
    except DBUnavailableError as exc:
        log.warning(
            "managed workflow truth unavailable; stale file substitution denied (%s)",
            type(exc).__name__,
        )
        return {}


def _collect_latest_workflow_runs_from_db(
    governance_runtime: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """从 governance DB 的 task_queue 表获取最近完成的 workflow 信息.

    容器内无 artifacts/operations/workflow_runs/ 目录时的回退路径。
    """
    latest_by_workflow: dict[str, dict[str, Any]] = {}
    try:
        from aats.api._governance_db import governance_session

        with governance_session() as session:
            from sqlalchemy import text as sa_text

            rows = session.execute(
                sa_text(
                    "SELECT DISTINCT ON (workflow) "
                    "  workflow, status, requested_at, started_at, finished_at "
                    "FROM governance.rdp_task_queue "
                    "WHERE status = 'done' "
                    "ORDER BY workflow, finished_at DESC NULLS LAST"
                ),
            ).fetchall()
            for row in rows:
                workflow = str(row[0])
                latest_by_workflow[workflow] = {
                    "workflow": workflow,
                    "overall_status": "success" if row[1] == "done" else row[1],
                    "started_at": row[3].isoformat() if row[3] else None,
                    "finished_at": row[4].isoformat() if row[4] else None,
                    "path": "governance.rdp_task_queue",
                }
    except Exception:
        pass
    return latest_by_workflow


def _load_latest_decision_round_from_db(
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    """Resolve DB truth without conflating empty/error with file-only mode.

    ``None`` is reserved for an explicitly file-only development context.
    Once a DB probe succeeds, or managed DB configuration exists, an empty or
    failed query is an authoritative unavailable result and must not trigger a
    stale latest-file substitution.
    """

    from aats.data_platform.governance._db_util import (
        has_explicit_governance_db_configuration,
    )

    db_is_managed_truth = has_explicit_governance_db_configuration(project_root)
    engine = None
    try:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance._db_util import try_governance_db
        from aats.data_platform.governance.decision_rounds_db import (
            db_load_latest_decision_round_snapshot,
        )

        engine, ok = try_governance_db()
        if not ok:
            if db_is_managed_truth:
                return {
                    "available": False,
                    "data_source": "db",
                    "authoritative": True,
                    "audit_only": True,
                    "reason_code": "decision_round_db_unavailable",
                }
            return None
        try:
            with Session(engine) as session:
                snapshot = db_load_latest_decision_round_snapshot(session)
            if not snapshot:
                return {
                    "available": False,
                    "data_source": "db",
                    "authoritative": True,
                    "audit_only": True,
                    "reason_code": "decision_round_db_empty",
                }
            return {
                "available": True,
                "data_source": "db",
                "authoritative": True,
                "audit_only": False,
                "round_id": snapshot.get("round_id"),
                "round_dir": None,
                "started_at": snapshot.get("started_at"),
                "finished_at": snapshot.get("finished_at"),
                "evidence_bundle_summary": snapshot.get("evidence_bundle_summary"),
                "parameter_upgrade_candidates": snapshot.get("parameter_upgrade_candidates"),
                "family_timeframe_decisions": snapshot.get("family_timeframe_decisions"),
                "promotion_readiness_assessment": snapshot.get("promotion_readiness_assessment"),
                "manifest": snapshot.get("manifest"),
                "has_conclusion_report": bool(snapshot.get("conclusion_markdown")),
            }
        finally:
            if engine is not None:
                engine.dispose()
    except Exception as exc:
        log.warning("decision round DB 读取失败，已禁止文件替代 (%s)", type(exc).__name__)
        return {
            "available": False,
            "data_source": "db",
            "authoritative": True,
            "audit_only": True,
            "reason_code": "decision_round_db_error",
        }


def _query_latest_decision_round_from_files(project_root: Path) -> dict[str, Any]:
    rounds_root = project_root / _DECISION_ROUNDS_DIR
    latest_dir = _find_latest_round_dir(rounds_root)

    result: dict[str, Any] = {
        "available": False,
        "data_source": "file",
        "round_id": None,
        "round_dir": None,
        "started_at": None,
        "finished_at": None,
    }

    if latest_dir is None:
        return result

    result["round_id"] = latest_dir.name
    result["round_dir"] = str(latest_dir)
    result["available"] = True

    manifest = _safe_load_json(latest_dir / "round_manifest.json")
    if isinstance(manifest, dict):
        result["manifest"] = manifest
        result["started_at"] = manifest.get("started_at")
        result["finished_at"] = manifest.get("finished_at")

    file_map = {
        "evidence_bundle_summary": ["evidence_bundle_summary.json", "evidence_summary.json"],
        "parameter_upgrade_candidates": ["parameter_upgrade_candidates.json"],
        "family_timeframe_decisions": ["family_timeframe_decisions.json"],
        "promotion_readiness_assessment": [
            "promotion_readiness_assessment.json",
            "promotion_readiness_report.json",
        ],
    }
    for key, filenames in file_map.items():
        data = None
        for filename in filenames:
            data = _safe_load_json(latest_dir / filename)
            if data is not None:
                break
        result[key] = data

    conclusion_md = latest_dir / "phase6_closed_loop_decision_conclusion.md"
    result["has_conclusion_report"] = conclusion_md.exists()
    return result


def _augment_workflow_runs_with_decision_round(
    latest_runs: dict[str, dict[str, Any]],
    project_root: Path,
) -> dict[str, dict[str, Any]]:
    snapshot = _load_latest_decision_round_from_db(project_root)
    if snapshot is None:
        snapshot = _query_latest_decision_round_from_files(project_root)
    if not snapshot or not snapshot.get("available"):
        return latest_runs

    finished_at = snapshot.get("finished_at") or snapshot.get("started_at")
    if not finished_at:
        return latest_runs
    snapshot_dt = _parse_iso_datetime(str(finished_at))
    if snapshot_dt is None:
        return latest_runs

    augmented = dict(latest_runs)
    data_source = str(snapshot.get("data_source") or "snapshot")
    location = snapshot.get("round_dir") or f"decision_round:{snapshot.get('round_id')}"
    for workflow in ("governance_cycle", "decision_cycle"):
        current = augmented.get(workflow)
        current_dt = _parse_iso_datetime(
            str(current.get("finished_at") or current.get("started_at") or "")
            if current else None,
        )
        if current is None or current_dt is None or snapshot_dt > current_dt:
            augmented[workflow] = {
                "workflow": workflow,
                "overall_status": "success",
                "started_at": snapshot.get("started_at"),
                "finished_at": snapshot.get("finished_at"),
                "path": location,
                "synthetic_from": data_source,
            }
    return augmented


def _check_db_initialization(
    governance_runtime: dict[str, Any],
) -> tuple[bool, bool]:
    """通过 governance DB 查询判断初始化状态.

    容器内无 daemon 宿主侧的 artifact 文件，但 DB 中有对应数据。
    检查 task_queue 中是否有完成的 workflow 记录来判断数据是否存在。

    Returns
    -------
    (has_governance_data, has_recommendations)
    """
    has_governance_data = False
    has_recommendations = False
    try:
        task_queue = governance_runtime.get("task_queue") or {}
        # 有完成的任务 → governance/decision 数据已初始化
        done_count = int(task_queue.get("done_count", 0) or 0)
        if done_count > 0:
            has_governance_data = True
        # 尝试查 recommendation 表
        try:
            from aats.api._governance_db import governance_session

            with governance_session() as session:
                from sqlalchemy import text as sa_text

                row = session.execute(
                    sa_text(
                        "SELECT COUNT(*) FROM governance.recommendations "
                        "WHERE created_at > NOW() - INTERVAL '7 days'"
                    ),
                ).scalar()
                if row and int(row) > 0:
                    has_recommendations = True
                    has_governance_data = True
        except Exception:
            # 表可能不存在（旧 schema），不影响其他判定
            pass
    except Exception:
        pass
    return has_governance_data, has_recommendations


def _query_governance_runtime_state() -> dict[str, Any]:
    result: dict[str, Any] = {
        "connection_ok": False,
        "task_queue": None,
        "runtime_components": [],
        "errors": [],
    }
    try:
        from aats.data_platform.db import get_session
        from aats.data_platform.governance.rdp_runtime_status_db import (
            db_list_runtime_status,
        )
        from aats.data_platform.governance.rdp_task_db import (
            db_get_task_queue_summary,
        )

        with get_session() as session:
            session.execute(text("SELECT 1"))
            result["connection_ok"] = True
            try:
                result["task_queue"] = db_get_task_queue_summary(session)
            except Exception as exc:
                result["errors"].append(f"task_queue_summary_failed: {exc}")
            try:
                result["runtime_components"] = db_list_runtime_status(session)
            except Exception as exc:
                result["errors"].append(f"runtime_status_failed: {exc}")
    except Exception as exc:
        result["errors"].append(f"governance_db_connection_failed: {exc}")
    return result


# ── 1. Active Parameter Sets ──────────────────────────────────────


def query_active_parameter_sets(project_root: Path) -> dict[str, Any]:
    """查询当前 active parameter sets.

    读取 configs/active_parameter_sets/ 目录下的所有 JSON 文件。
    """
    from aats.bootstrap.active_parameters import get_active_parameter_summary
    return get_active_parameter_summary(project_root=project_root)


def query_parameter_registry(project_root: Path) -> dict[str, Any]:
    """查询 parameter registry，用于解释 candidate / active 参数状态。"""
    registry_path = project_root / _GOVERNANCE_DIR / "current_parameter_registry.json"
    from aats.data_platform.governance._db_util import (
        has_explicit_governance_db_configuration,
    )

    managed_truth = has_explicit_governance_db_configuration(project_root)
    result: dict[str, Any] = {
        "available": False,
        "registry_path": str(registry_path),
        "generated_at": None,
        "version": None,
        "parameter_sets": [],
        "status_distribution": {},
    }

    try:
        from aats.data_platform.governance.parameter_registry import load_registry

        registry = load_registry(registry_path)
    except Exception as exc:
        if managed_truth:
            result.update({
                "audit_only": True,
                "reason_code": "parameter_registry_db_unavailable",
                "error_type": type(exc).__name__,
            })
            return result
        registry = _safe_load_json(registry_path)

    if registry is None:
        return result

    parameter_sets = registry.get("parameter_sets", [])
    if managed_truth and not parameter_sets:
        result.update({
            "audit_only": True,
            "reason_code": "parameter_registry_db_empty",
        })
        return result
    result["available"] = True
    result["generated_at"] = registry.get("generated_at")
    result["version"] = registry.get("version")
    result["parameter_sets"] = parameter_sets

    status_counts: dict[str, int] = {}
    for item in parameter_sets:
        status = item.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    result["status_distribution"] = status_counts
    return result


# ── 2. Latest Attribution ─────────────────────────────────────────


def query_latest_attribution(project_root: Path) -> dict[str, Any]:
    """查询最近一次 attribution round 结论.

    读取:
      - artifacts/research/attribution_rounds/<latest>/round_manifest.json
      - artifacts/research/attribution_rounds/<latest>/attribution_report.md
      - artifacts/research/attribution_rounds/<latest>/attribution_summary.json
    """
    result: dict[str, Any] = {
        "available": False,
        "round_id": None,
        "round_dir": None,
        "manifest": None,
        "summary": None,
    }

    snapshot = load_latest_research_round_snapshot(
        phase=ROUND_PHASE_PHASE3,
        project_root=project_root,
    )
    # 磁盘目录缺 round_manifest.json 的不完整 phase3 快照：暴露 round_id 让运营者知道
    # "磁盘上有目录但不完整"，但 available 必须保持 False —— 否则 UI 会把占位
    # summary 当成"最新 attribution 已可用"。直接返回，避免回落到磁盘扫描时
    # 再把同一个不完整目录翻出来当 available。
    if is_snapshot_incomplete(snapshot):
        result["round_id"] = snapshot.get("round_id")
        result["round_dir"] = snapshot.get("round_path")
        result["manifest"] = {
            "round_id": snapshot.get("round_id"),
            "status": "unknown",
            "started_at": None,
            "finished_at": None,
            "scope": None,
        }
        result["incomplete_reason"] = "manifest_missing_on_disk"
        return result
    if snapshot:
        manifest = snapshot.get("manifest") or {}
        result["manifest"] = {
            "round_id": manifest.get("round_id"),
            "status": manifest.get("overall_status", manifest.get("status")),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "scope": manifest.get("scope", manifest.get("window")),
        }
        result["round_id"] = snapshot.get("round_id")
        result["round_dir"] = snapshot.get("round_path")
        summary = snapshot.get("summary", {}) or {}
        if summary.get("summary_rows") is not None:
            result["summary"] = {"experiments": summary.get("summary_rows", [])}
            result["available"] = True
        combos = []
        for combo_key, combo in (summary.get("combos", {}) or {}).items():
            combo_summary = combo.get("attribution_summary")
            if combo_summary is not None:
                combos.append({"combo_key": combo_key, "summary": combo_summary})
        if combos:
            result["combos"] = combos
            result["available"] = True
        if result["available"]:
            return result

    rounds_root = project_root / _ATTRIBUTION_ROUNDS_DIR
    latest_dir = _find_latest_round_dir(rounds_root)
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
    else:
        # 磁盘 fallback 路径同样要检查 manifest：缺就不能 available。
        result["manifest"] = {
            "round_id": latest_dir.name,
            "status": "unknown",
            "started_at": None,
            "finished_at": None,
            "scope": None,
        }
        result["incomplete_reason"] = "manifest_missing_on_disk"
        return result

    summary = _safe_load_json(latest_dir / "attribution_summary.json")
    if summary:
        result["summary"] = summary
        result["available"] = True

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
    result: dict[str, Any] = {
        "available": False,
        "round_id": None,
        "round_dir": None,
        "manifest": None,
        "summary": None,
    }

    snapshot = load_latest_research_round_snapshot(
        phase=ROUND_PHASE_PHASE4,
        project_root=project_root,
    )
    # 同 attribution：缺 round_manifest.json 的 phase4 snapshot 不能标 available，
    # 否则 operator/UI 会误信"最新 execution realism 已就绪"。
    if is_snapshot_incomplete(snapshot):
        result["round_id"] = snapshot.get("round_id")
        result["round_dir"] = snapshot.get("round_path")
        result["manifest"] = {
            "round_id": snapshot.get("round_id"),
            "status": "unknown",
            "started_at": None,
            "finished_at": None,
            "scope": None,
        }
        result["incomplete_reason"] = "manifest_missing_on_disk"
        return result
    if snapshot:
        manifest = snapshot.get("manifest") or {}
        result["manifest"] = {
            "round_id": manifest.get("round_id"),
            "status": manifest.get("overall_status", manifest.get("status")),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "scope": manifest.get("scope", manifest.get("window")),
        }
        result["round_id"] = snapshot.get("round_id")
        result["round_dir"] = snapshot.get("round_path")
        summary = snapshot.get("summary", {}) or {}
        if summary.get("comparison_rows") is not None:
            result["summary"] = {
                "comparison_rows": summary.get("comparison_rows", []),
                "cross_findings": summary.get("cross_findings", []),
            }
            result["available"] = True
        combos = []
        for combo_key, combo in (summary.get("combos", {}) or {}).items():
            combo_summary = combo.get("cost_summary")
            if combo_summary is not None:
                combos.append({"combo_key": combo_key, "summary": combo_summary})
        if combos:
            result["combos"] = combos
            result["available"] = True
        if result["available"]:
            return result

    rounds_root = project_root / _EXECUTION_ROUNDS_DIR
    latest_dir = _find_latest_round_dir(rounds_root)
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
    else:
        result["manifest"] = {
            "round_id": latest_dir.name,
            "status": "unknown",
            "started_at": None,
            "finished_at": None,
            "scope": None,
        }
        result["incomplete_reason"] = "manifest_missing_on_disk"
        return result

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
    from aats.data_platform.governance._db_util import (
        has_explicit_governance_db_configuration,
    )

    managed_truth = has_explicit_governance_db_configuration(project_root)
    result: dict[str, Any] = {
        "available": False,
        "registry_path": str(dec_path),
        "generated_at": None,
        "version": None,
        "decisions": [],
        "status_distribution": {},
    }

    # 复用 DB-first loader
    try:
        from aats.data_platform.decision_system.recommendation_registry import (
            load_active_decision_registry,
        )
        registry = load_active_decision_registry(dec_path)
    except Exception as exc:
        if managed_truth:
            result.update({
                "audit_only": True,
                "reason_code": "active_decision_db_unavailable",
                "error_type": type(exc).__name__,
            })
            return result
        registry = _safe_load_json(dec_path)

    if registry is None:
        return result

    decisions = registry.get("decisions", [])
    if managed_truth and not decisions:
        result.update({
            "audit_only": True,
            "reason_code": "active_decision_db_empty",
        })
        return result

    result["available"] = True
    result["generated_at"] = registry.get("generated_at")
    result["version"] = registry.get("version")
    result["decisions"] = decisions

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
    truth_error: str | None = None

    # 复用 DB-first loader。loader 已在明确 file-only 模式内自行 fallback；
    # 它抛错表示 managed DB 真值不可用，禁止在这里再次复活 JSON 审计副本。
    try:
        from aats.data_platform.decision_system.recommendation_registry import (
            load_recommendation_registry,
        )
        from aats.data_platform.governance._exceptions import DBUnavailableError

        registry = load_recommendation_registry(rec_path)
    except DBUnavailableError as exc:
        log.warning(
            "recommendation truth unavailable; stale file substitution denied (%s)",
            type(exc).__name__,
        )
        registry = None
        truth_error = "recommendation_db_unavailable"
    except Exception as exc:
        log.warning(
            "recommendation registry invalid; no recommendations exposed (%s)",
            type(exc).__name__,
        )
        registry = None
        truth_error = "recommendation_registry_invalid"

    result: dict[str, Any] = {
        "available": False,
        "registry_path": str(rec_path),
        "generated_at": None,
        "version": None,
        "total_count": 0,
        "recommendations": [],
        "data_source": (
            "db" if truth_error == "recommendation_db_unavailable" else None
        ),
        "audit_only": truth_error is not None,
        "reason_code": truth_error,
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
    """查询 RDP 子系统整体健康状态."""
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []
    warnings: list[str] = []
    from aats.data_platform.operations.environment_guard import get_current_environment

    environment = get_current_environment()
    strict_environment = environment in {"staging", "prod"}

    # 0. 先检查 governance DB，后续根据 DB 可用性调整 artifact 检查策略
    governance_runtime = _query_governance_runtime_state()
    db_connected = governance_runtime.get("connection_ok", False)

    # 当 DB 可用时，用 DB 数据判断初始化状态（容器内无本地 artifact 文件）
    db_has_governance_data = False
    db_has_recommendations = False
    if db_connected:
        db_has_governance_data, db_has_recommendations = _check_db_initialization(
            governance_runtime,
        )

    # 1. 核心 artifacts 初始化情况
    #    当 DB 可达且有数据时，本地文件缺失仅作 info，不影响初始化判定。
    governance_files = {
        "artifact_index": {
            "path": project_root / _GOVERNANCE_DIR / "artifact_index.json",
            "snapshot_type": SNAPSHOT_ARTIFACT_INDEX,
        },
        "active_round_index": {
            "path": project_root / _GOVERNANCE_DIR / "active_round_index.json",
            "snapshot_type": SNAPSHOT_ACTIVE_ROUND_INDEX,
        },
        "parameter_registry": {
            "path": project_root / _GOVERNANCE_DIR / "current_parameter_registry.json",
            "snapshot_type": None,
        },
        "quality_monitor": {
            "path": project_root / _GOVERNANCE_DIR / "quality_monitor_summary.json",
            "snapshot_type": SNAPSHOT_QUALITY_MONITOR,
        },
    }
    governance_files_ok = True
    for name, file_info in governance_files.items():
        path = file_info["path"]
        snapshot_type = file_info["snapshot_type"]
        data = (
            load_governance_snapshot(project_root, snapshot_type=snapshot_type)
            if snapshot_type
            else _safe_load_json(path)
        )
        exists = path.exists()
        snapshot_available = data is not None
        governance_files_ok = governance_files_ok and (exists or snapshot_available)
        if snapshot_available:
            status = "ok"
            detail = str(path) if exists else "DB-first snapshot available"
        elif db_has_governance_data:
            # DB 有数据，文件仅在 daemon 宿主侧存在 → 不阻断
            status = "ok"
            detail = "本地文件不可用（容器环境），治理数据库已连接且有数据"
        else:
            status = "missing"
            detail = str(path)
        checks.append({
            "category": "artifacts",
            "name": f"governance:{name}",
            "status": status,
            "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
            "detail": detail,
        })
    governance_initialized = governance_files_ok or db_has_governance_data

    decision_files = {
        "recommendation_registry": project_root / _DECISION_SYSTEM_DIR / "recommendation_registry.json",
        "active_decision_registry": project_root / _DECISION_SYSTEM_DIR / "active_decision_registry.json",
        "evidence_bundle_index": project_root / _DECISION_SYSTEM_DIR / "evidence_bundle_index.json",
    }
    decision_files_ok = True
    for name, path in decision_files.items():
        data = _safe_load_json(path)
        exists = path.exists()
        decision_files_ok = decision_files_ok and exists
        if exists:
            status = "ok"
            detail = str(path)
        elif db_has_recommendations:
            status = "ok"
            detail = "本地文件不可用（容器环境），治理数据库已连接且有建议数据"
        else:
            status = "missing"
            detail = str(path)
        checks.append({
            "category": "artifacts",
            "name": f"decision:{name}",
            "status": status,
            "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
            "detail": detail,
        })
    decision_initialized = decision_files_ok or db_has_recommendations

    # 2. governance DB + task queue + runtime status (已提前查询)
    if not governance_runtime.get("connection_ok"):
        blocking_reasons.append("governance_db_unreachable")
        checks.append({
            "category": "governance_db",
            "name": "connection",
            "status": "blocked",
            "detail": "; ".join(governance_runtime.get("errors") or ["connection failed"]),
        })
    else:
        checks.append({
            "category": "governance_db",
            "name": "connection",
            "status": "ok",
            "detail": "governance DB reachable",
        })

    task_queue = governance_runtime.get("task_queue") or {}
    if governance_runtime.get("connection_ok") and task_queue:
        backlog = int(task_queue.get("pending_count", 0) or 0)
        running = int(task_queue.get("running_count", 0) or 0)
        failed_history = int(task_queue.get("failed_count", 0) or 0)
        failed_latest = int(task_queue.get("latest_failed_count", 0) or 0)
        queue_status = "ok"
        if backlog > 0 or failed_latest > 0:
            queue_status = "warn"
            warnings.append("rdp_task_queue_backlog_or_failures")
        checks.append({
            "category": "task_queue",
            "name": "queue_state",
            "status": queue_status,
            "detail": (
                f"pending={backlog}, running={running}, "
                f"failed_latest={failed_latest}, failed_history={failed_history}"
            ),
        })

    runtime_components = {
        item.get("component"): item
        for item in governance_runtime.get("runtime_components", [])
        if isinstance(item, dict)
    }
    daemon_status = runtime_components.get("rdp-daemon")
    daemon_fresh = False
    if daemon_status is None:
        if strict_environment:
            blocking_reasons.append("rdp_daemon_status_missing")
        else:
            warnings.append("rdp_daemon_status_missing")
        checks.append({
            "category": "runtime",
            "name": "rdp-daemon",
            "status": "blocked" if strict_environment else "warn",
            "detail": "rdp-daemon heartbeat not found in governance.rdp_runtime_status",
        })
    else:
        heartbeat_at = _parse_iso_datetime(str(daemon_status.get("heartbeat_at") or ""))
        age_seconds = None
        if heartbeat_at is not None:
            age_seconds = (datetime.now(timezone.utc) - heartbeat_at).total_seconds()
            daemon_fresh = age_seconds < 45
        daemon_state = str(daemon_status.get("status") or "unknown")
        if not daemon_fresh or daemon_state in {"error", "stopped"}:
            if strict_environment:
                blocking_reasons.append("rdp_daemon_unhealthy")
            else:
                warnings.append("rdp_daemon_unhealthy")
            checks.append({
                "category": "runtime",
                "name": "rdp-daemon",
                "status": "blocked" if strict_environment else "warn",
                "detail": f"state={daemon_state}, age_seconds={age_seconds}",
            })
        elif daemon_state in {"degraded", "starting"}:
            warnings.append("rdp_daemon_degraded")
            checks.append({
                "category": "runtime",
                "name": "rdp-daemon",
                "status": "warn",
                "detail": f"state={daemon_state}, age_seconds={age_seconds}",
            })
        else:
            checks.append({
                "category": "runtime",
                "name": "rdp-daemon",
                "status": "ok",
                "detail": f"state={daemon_state}, age_seconds={age_seconds}",
            })

    # 3. reliability alerts
    from aats.data_platform.operations.alerting import load_current_alerts

    current_alerts = load_current_alerts(project_root)
    if current_alerts is None:
        warnings.append("current_alerts_missing")
        checks.append({
            "category": "alerts",
            "name": "current_alerts",
            "status": "warn",
            "detail": "current_alerts.json not found",
        })
    else:
        overall_status = str(current_alerts.get("overall_status") or "unknown")
        critical_count = int(current_alerts.get("critical_alerts", 0) or 0)
        warning_count = int(current_alerts.get("warning_alerts", 0) or 0)
        if overall_status == "critical" or critical_count > 0:
            blocking_reasons.append("critical_reliability_alerts")
            status = "blocked"
        elif overall_status == "warning" or warning_count > 0:
            warnings.append("warning_reliability_alerts")
            status = "warn"
        else:
            status = "ok"
        checks.append({
            "category": "alerts",
            "name": "current_alerts",
            "status": status,
            "detail": (
                f"overall={overall_status}, critical={critical_count}, "
                f"warning={warning_count}"
            ),
        })

    # 4. workflow 新鲜度
    workflow_thresholds = {
        "data_maintenance": 36,
        "governance_cycle": 36,
        "decision_cycle": 168,
    }
    latest_runs = _collect_latest_workflow_runs(project_root)
    workflow_status = "ok"
    workflow_details: list[str] = []
    now = datetime.now(timezone.utc)
    for workflow, max_age_hours in workflow_thresholds.items():
        latest = latest_runs.get(workflow)
        if latest is None:
            workflow_status = "blocked"
            workflow_details.append(f"{workflow}=missing")
            continue
        status = str(latest.get("overall_status") or "unknown")
        if status != "success":
            workflow_status = "blocked"
            workflow_details.append(f"{workflow}=status:{status}")
            continue
        raw_finished_at = latest.get("finished_at")
        if not isinstance(raw_finished_at, str) or not raw_finished_at.strip():
            workflow_status = "blocked"
            workflow_details.append(f"{workflow}=missing_finished_at")
            continue
        finished_at = _parse_iso_datetime(raw_finished_at)
        if finished_at is None:
            workflow_status = "blocked"
            workflow_details.append(f"{workflow}=invalid_finished_at")
            continue
        if finished_at > now + timedelta(minutes=5):
            workflow_status = "blocked"
            workflow_details.append(f"{workflow}=future_finished_at")
            continue
        age_hours = (now - finished_at).total_seconds() / 3600
        if age_hours > max_age_hours:
            workflow_status = "blocked"
            workflow_details.append(f"{workflow}=stale:{age_hours:.1f}h")
        else:
            workflow_details.append(f"{workflow}=ok:{age_hours:.1f}h")
    if workflow_status == "blocked":
        if strict_environment:
            blocking_reasons.append("workflow_runs_stale_or_missing")
        else:
            warnings.append("workflow_runs_stale_or_missing")
            workflow_status = "warn"
    elif workflow_status == "warn":
        warnings.append("workflow_runs_incomplete")
    checks.append({
        "category": "workflow_runs",
        "name": "freshness",
        "status": workflow_status,
        "detail": "; ".join(workflow_details) if workflow_details else "no workflow reports",
    })

    # 5. live DB 只读链路
    from aats.data_platform.live_query_adapter import check_live_db_health

    live_db_health = check_live_db_health()
    if not live_db_health.get("healthy"):
        if strict_environment:
            blocking_reasons.append("live_db_unhealthy")
        else:
            warnings.append("live_db_unhealthy")
        checks.append({
            "category": "live_db",
            "name": "readonly_access",
            "status": "blocked" if strict_environment else "warn",
            "detail": "; ".join(live_db_health.get("errors") or ["live DB unhealthy"]),
        })
    else:
        checks.append({
            "category": "live_db",
            "name": "readonly_access",
            "status": "ok",
            "detail": (
                f"connection_ok={live_db_health.get('connection_ok')}, "
                f"tables={len(live_db_health.get('tables_checked', {}))}"
            ),
        })

    from aats.bootstrap.active_parameters import load_all_active_parameter_sets

    active_sets = load_all_active_parameter_sets(project_root=project_root)
    if not active_sets:
        warnings.append("no_active_parameter_sets")
    checks.append({
        "category": "parameters",
        "name": "active_parameter_sets",
        "status": "ok" if active_sets else "warn",
        "detail": f"count={len(active_sets)}",
    })

    initialized = governance_initialized or decision_initialized or bool(active_sets)
    if not initialized and not governance_runtime.get("connection_ok"):
        overall = "not_initialized"
    elif blocking_reasons:
        overall = "blocked"
    elif warnings:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "overall_health": overall,
        "environment": environment,
        "governance_initialized": governance_initialized,
        "decision_system_initialized": decision_initialized,
        "active_parameter_count": len(active_sets),
        "governance_db": governance_runtime,
        "task_queue": task_queue,
        "runtime_components": governance_runtime.get("runtime_components", []),
        "live_db": live_db_health,
        "workflow_runs": latest_runs,
        "current_alerts": current_alerts,
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "checks": checks,
    }


# ── 7. Latest Decision Round ─────────────────────────────────────


def query_latest_decision_round(project_root: Path) -> dict[str, Any]:
    """查询最近一次 decision round 结论.

    优先走 governance DB snapshot，文件系统仅作为 fallback。
    """
    snapshot = _load_latest_decision_round_from_db(project_root)
    if snapshot is not None:
        return snapshot
    return _query_latest_decision_round_from_files(project_root)


# ── 8. Promotion Readiness ────────────────────────────────────────


_PROMOTION_READINESS_STATUSES = frozenset({
    "ready_for_next_live_test",
    "not_ready_more_research_needed",
    "not_ready_attribution_issue",
    "not_ready_execution_issue",
    "not_ready_governance_issue",
})
_PROMOTION_READINESS_CHECKS = (
    "research_stability",
    "attribution_no_severe_issue",
    "execution_not_severe",
    "governance_healthy",
    "parameter_traceable",
    "has_promote_candidate",
    "has_keep_active_ft",
)
_PROMOTION_READINESS_ASSESSMENT_FIELDS = frozenset({
    "generated_at",
    "readiness",
    "overall_confidence",
    "checks_total",
    "checks_passed",
    "checks_failed",
    "blockers",
    "checks",
    "promoted_candidates",
    "active_family_timeframes",
})


def _parse_canonical_promotion_timestamp(
    value: Any,
    *,
    context: str,
) -> datetime | None:
    """Parse one exact UTC timestamp used by the readiness projection."""

    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not (value.endswith("Z") or value.endswith("+00:00"))
    ):
        return None
    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    try:
        return parse_iso_datetime_utc(value, context=context)
    except (TypeError, ValueError):
        return None


def _derived_promotion_readiness(checks: list[dict[str, Any]]) -> str:
    """Reproduce the current readiness evaluator's ordered decision contract."""

    if all(check["passed"] for check in checks):
        return "ready_for_next_live_test"
    if not checks[0]["passed"]:
        return "not_ready_more_research_needed"
    if not checks[1]["passed"]:
        return "not_ready_attribution_issue"
    if not checks[2]["passed"]:
        return "not_ready_execution_issue"
    if not checks[3]["passed"]:
        return "not_ready_governance_issue"
    return "not_ready_more_research_needed"


def _validate_promotion_readiness_assessment(
    assessment: Any,
    *,
    manifest: dict[str, Any],
    upgrade_candidates: Any,
    ft_decisions: Any,
    round_started_at: datetime,
    round_finished_at: datetime,
) -> str | None:
    """Return a fail-closed reason code, or ``None`` for the current schema.

    The persisted assessment has no standalone round-id field.  Its identity is
    therefore bound by the enclosing exact snapshot, manifest readiness, and a
    generated-at value inside the same canonical start/finish interval.
    Internal counts and the ordered seven-check state machine are recomputed
    instead of trusted.
    """

    if type(assessment) is not dict or not assessment:
        return "promotion_readiness_assessment_invalid"
    if frozenset(assessment) != _PROMOTION_READINESS_ASSESSMENT_FIELDS:
        return "promotion_readiness_assessment_schema_invalid"

    readiness = assessment.get("readiness")
    confidence = assessment.get("overall_confidence")
    if (
        type(readiness) is not str
        or readiness not in _PROMOTION_READINESS_STATUSES
        or type(confidence) is not str
        or confidence not in {"medium", "high"}
    ):
        return "promotion_readiness_assessment_schema_invalid"
    if manifest.get("readiness") != readiness:
        return "promotion_readiness_assessment_manifest_mismatch"

    generated_at = _parse_canonical_promotion_timestamp(
        assessment.get("generated_at"),
        context="rdp_queries.promotion_readiness.assessment.generated_at",
    )
    if (
        generated_at is None
        or generated_at < round_started_at
        or generated_at > round_finished_at
    ):
        return "promotion_readiness_assessment_identity_mismatch"

    counts = tuple(
        assessment.get(field)
        for field in ("checks_total", "checks_passed", "checks_failed")
    )
    if any(type(value) is not int or value < 0 for value in counts):
        return "promotion_readiness_assessment_schema_invalid"
    checks_total, checks_passed, checks_failed = counts

    checks = assessment.get("checks")
    blockers = assessment.get("blockers")
    promoted = assessment.get("promoted_candidates")
    active = assessment.get("active_family_timeframes")
    if not all(type(value) is list for value in (checks, blockers, promoted, active)):
        return "promotion_readiness_assessment_schema_invalid"
    if (
        len(checks) != len(_PROMOTION_READINESS_CHECKS)
        or checks_total != len(checks)
    ):
        return "promotion_readiness_assessment_count_mismatch"

    for expected_name, check in zip(_PROMOTION_READINESS_CHECKS, checks, strict=True):
        if (
            type(check) is not dict
            or frozenset(check) != {"check", "passed", "detail"}
            or check.get("check") != expected_name
            or type(check.get("passed")) is not bool
            or type(check.get("detail")) is not str
            or not check["detail"].strip()
        ):
            return "promotion_readiness_assessment_schema_invalid"

    actual_passed = sum(1 for check in checks if check["passed"])
    actual_failed = len(checks) - actual_passed
    if (
        checks_passed != actual_passed
        or checks_failed != actual_failed
        or checks_passed + checks_failed != checks_total
    ):
        return "promotion_readiness_assessment_count_mismatch"
    if (
        not all(type(blocker) is str and blocker.strip() for blocker in blockers)
        or len(blockers) != actual_failed
    ):
        return "promotion_readiness_assessment_count_mismatch"

    if readiness != _derived_promotion_readiness(checks):
        return "promotion_readiness_assessment_schema_invalid"
    expected_confidence = (
        "high" if actual_failed == 0 or len(blockers) > 2 else "medium"
    )
    if confidence != expected_confidence:
        return "promotion_readiness_assessment_schema_invalid"

    promoted_ids: set[str] = set()
    for item in promoted:
        if type(item) is not dict or frozenset(item) != {
            "parameter_set_id",
            "score_ratio",
        }:
            return "promotion_readiness_assessment_schema_invalid"
        parameter_set_id = item.get("parameter_set_id")
        score_ratio = item.get("score_ratio")
        if (
            type(parameter_set_id) is not str
            or not parameter_set_id.strip()
            or parameter_set_id in promoted_ids
            or type(score_ratio) not in {int, float}
            or not math.isfinite(float(score_ratio))
        ):
            return "promotion_readiness_assessment_schema_invalid"
        promoted_ids.add(parameter_set_id)

    active_combos: set[str] = set()
    for item in active:
        if type(item) is not dict or frozenset(item) != {"combo_key", "confidence"}:
            return "promotion_readiness_assessment_schema_invalid"
        combo_key = item.get("combo_key")
        item_confidence = item.get("confidence")
        if (
            type(combo_key) is not str
            or not combo_key.strip()
            or combo_key in active_combos
            or item_confidence not in {"low", "medium", "high"}
        ):
            return "promotion_readiness_assessment_schema_invalid"
        active_combos.add(combo_key)

    upgrade_count = manifest.get("upgrade_candidates_count")
    ft_decision_count = manifest.get("ft_decisions_count")
    if (
        type(upgrade_count) is not int
        or upgrade_count < 0
        or type(ft_decision_count) is not int
        or ft_decision_count < 0
    ):
        return "promotion_readiness_manifest_count_invalid"
    if (
        type(upgrade_candidates) is not list
        or type(ft_decisions) is not list
        or not all(type(item) is dict for item in upgrade_candidates)
        or not all(type(item) is dict for item in ft_decisions)
    ):
        return "promotion_readiness_round_payload_invalid"
    if (
        len(upgrade_candidates) != upgrade_count
        or len(ft_decisions) != ft_decision_count
    ):
        return "promotion_readiness_manifest_count_mismatch"

    expected_promoted = [
        {
            "parameter_set_id": item.get("parameter_set_id"),
            "score_ratio": item.get("score_ratio"),
        }
        for item in upgrade_candidates
        if item.get("decision") == "promote_candidate"
    ]
    expected_active = [
        {
            "combo_key": item.get("combo_key"),
            "confidence": item.get("confidence"),
        }
        for item in ft_decisions
        if item.get("decision") == "keep_active"
    ]
    if promoted != expected_promoted or active != expected_active:
        return "promotion_readiness_assessment_count_mismatch"

    checks_by_name = {check["check"]: check for check in checks}
    if checks_by_name["has_promote_candidate"]["passed"] is not bool(promoted):
        return "promotion_readiness_assessment_count_mismatch"
    if checks_by_name["has_keep_active_ft"]["passed"] is not bool(active):
        return "promotion_readiness_assessment_count_mismatch"
    return None


def query_promotion_readiness(project_root: Path) -> dict[str, Any]:
    """查询最近一次 promotion readiness 评估.

    从最近完整且仍新鲜的 Phase 6 decision round 中提取。

    这是资本推进读模型；最新 round 的目录或快照存在并不证明它可用于
    promotion。因此只接受 manifest 与 snapshot 身份一致、明确成功完成，且
    canonical UTC ``finished_at`` 不在未来并且不超过 168 小时的 round。
    """
    dr = query_latest_decision_round(project_root)
    if not dr.get("available"):
        return {
            "available": False,
            "audit_only": bool(dr.get("audit_only")),
            "data_source": dr.get("data_source"),
            "reason_code": dr.get("reason_code") or "decision_round_unavailable",
        }

    round_id = dr.get("round_id")
    manifest = dr.get("manifest")
    if not isinstance(manifest, dict):
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_manifest_invalid",
        }
    if (
        type(round_id) is not str
        or not round_id
        or round_id != round_id.strip()
        or manifest.get("round_id") != round_id
    ):
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_round_id_mismatch",
        }
    if manifest.get("phase") != "phase6":
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_round_phase_invalid",
        }
    if manifest.get("status") != "succeeded":
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_round_status_invalid",
        }

    raw_finished_at = dr.get("finished_at")
    if not isinstance(raw_finished_at, str) or not raw_finished_at.strip():
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_finished_at_missing",
        }
    finished_at = _parse_canonical_promotion_timestamp(
        raw_finished_at,
        context="rdp_queries.promotion_readiness.finished_at",
    )
    if finished_at is None:
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_finished_at_invalid",
        }

    started_at = _parse_canonical_promotion_timestamp(
        dr.get("started_at"),
        context="rdp_queries.promotion_readiness.started_at",
    )
    manifest_started_at = _parse_canonical_promotion_timestamp(
        manifest.get("started_at"),
        context="rdp_queries.promotion_readiness.manifest.started_at",
    )
    manifest_finished_at = _parse_canonical_promotion_timestamp(
        manifest.get("finished_at"),
        context="rdp_queries.promotion_readiness.manifest.finished_at",
    )
    if (
        started_at is None
        or manifest_started_at is None
        or manifest_finished_at is None
    ):
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_round_timestamps_invalid",
        }
    if (
        started_at != manifest_started_at
        or finished_at != manifest_finished_at
        or started_at > finished_at
    ):
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_round_timestamp_mismatch",
        }
    now = datetime.now(timezone.utc)
    if finished_at > now:
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_finished_at_future",
        }
    if now - finished_at > timedelta(hours=168):
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": "promotion_readiness_round_stale",
        }

    # 历史 Phase 6 round 可能把旧的自声明 Phase 2 聚合写成
    # ``ready_for_next_live_test``。readiness 是资本推进读模型，不能只因
    # report 文件存在就恢复资格；必须与现行、hash-bound promotion policy
    # 同时出现。旧 round 仍可通过 query_latest_decision_round() 审计查看。
    from aats.data_platform.decision_system.evidence_bundle import (
        PHASE2_PROMOTION_QUALIFICATION_POLICY,
    )

    evidence = dr.get("evidence_bundle_summary")
    phase2 = evidence.get("phase2_evidence") if isinstance(evidence, dict) else None
    policy = phase2.get("promotion_qualification_policy") if isinstance(phase2, dict) else None
    if policy != PHASE2_PROMOTION_QUALIFICATION_POLICY:
        return {
            "available": False,
            "round_id": dr.get("round_id"),
            "audit_only": True,
            "reason_code": "promotion_qualification_policy_unsupported",
            "promotion_qualification_policy": policy,
        }

    readiness = dr.get("promotion_readiness_assessment")
    assessment_error = _validate_promotion_readiness_assessment(
        readiness,
        manifest=manifest,
        upgrade_candidates=dr.get("parameter_upgrade_candidates"),
        ft_decisions=dr.get("family_timeframe_decisions"),
        round_started_at=started_at,
        round_finished_at=finished_at,
    )
    if assessment_error is not None:
        return {
            "available": False,
            "round_id": round_id,
            "audit_only": True,
            "reason_code": assessment_error,
        }

    return {
        "available": True,
        "round_id": round_id,
        "audit_only": False,
        "promotion_qualification_policy": policy,
        "assessment": readiness,
    }
