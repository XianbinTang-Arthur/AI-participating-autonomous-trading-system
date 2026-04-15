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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

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
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _collect_latest_workflow_runs(project_root: Path) -> dict[str, dict[str, Any]]:
    runs_dir = project_root / "artifacts/operations/workflow_runs"
    latest_by_workflow: dict[str, dict[str, Any]] = {}
    if not runs_dir.exists():
        return latest_by_workflow

    for path in sorted(runs_dir.glob("*.json"), reverse=True):
        payload = _safe_load_json(path)
        if not isinstance(payload, dict):
            continue
        workflow = str(payload.get("workflow") or "").strip()
        if not workflow:
            continue
        candidate = {
            "run_id": payload.get("run_id"),
            "workflow": workflow,
            "overall_status": payload.get("overall_status"),
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "path": str(path),
        }
        current = latest_by_workflow.get(workflow)
        candidate_dt = _parse_iso_datetime(
            str(candidate.get("finished_at") or candidate.get("started_at") or ""),
        )
        current_dt = _parse_iso_datetime(
            str(current.get("finished_at") or current.get("started_at") or "")
            if current else None,
        )
        if current is None or (
            candidate_dt is not None and current_dt is not None and candidate_dt > current_dt
        ) or (current_dt is None and candidate_dt is not None):
            latest_by_workflow[workflow] = candidate
    return latest_by_workflow


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

    try:
        from aats.data_platform.governance.parameter_registry import load_registry

        registry = load_registry(registry_path)
    except Exception:
        registry = _safe_load_json(registry_path)

    result: dict[str, Any] = {
        "available": False,
        "registry_path": str(registry_path),
        "generated_at": None,
        "version": None,
        "parameter_sets": [],
        "status_distribution": {},
    }

    if registry is None:
        return result

    parameter_sets = registry.get("parameter_sets", [])
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
    """查询 RDP 子系统整体健康状态."""
    checks: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []
    warnings: list[str] = []
    from aats.data_platform.operations.environment_guard import get_current_environment

    environment = get_current_environment()
    strict_environment = environment in {"staging", "prod"}

    # 1. 核心 artifacts 初始化情况
    governance_files = {
        "artifact_index": project_root / _GOVERNANCE_DIR / "artifact_index.json",
        "active_round_index": project_root / _GOVERNANCE_DIR / "active_round_index.json",
        "parameter_registry": project_root / _GOVERNANCE_DIR / "current_parameter_registry.json",
        "quality_monitor": project_root / _GOVERNANCE_DIR / "quality_monitor_summary.json",
    }
    governance_initialized = True
    for name, path in governance_files.items():
        data = _safe_load_json(path)
        exists = path.exists()
        governance_initialized = governance_initialized and exists
        checks.append({
            "category": "artifacts",
            "name": f"governance:{name}",
            "status": "ok" if exists else "missing",
            "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
            "detail": str(path),
        })

    decision_files = {
        "recommendation_registry": project_root / _DECISION_SYSTEM_DIR / "recommendation_registry.json",
        "active_decision_registry": project_root / _DECISION_SYSTEM_DIR / "active_decision_registry.json",
        "evidence_bundle_index": project_root / _DECISION_SYSTEM_DIR / "evidence_bundle_index.json",
    }
    decision_initialized = True
    for name, path in decision_files.items():
        data = _safe_load_json(path)
        exists = path.exists()
        decision_initialized = decision_initialized and exists
        checks.append({
            "category": "artifacts",
            "name": f"decision:{name}",
            "status": "ok" if exists else "missing",
            "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
            "detail": str(path),
        })

    # 2. governance DB + task queue + runtime status
    governance_runtime = _query_governance_runtime_state()
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
        failed = int(task_queue.get("failed_count", 0) or 0)
        queue_status = "ok"
        if backlog > 0 or failed > 0:
            queue_status = "warn"
            warnings.append("rdp_task_queue_backlog_or_failures")
        checks.append({
            "category": "task_queue",
            "name": "queue_state",
            "status": queue_status,
            "detail": f"pending={backlog}, running={running}, failed={failed}",
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
        finished_at = _parse_iso_datetime(
            str(latest.get("finished_at") or latest.get("started_at") or ""),
        )
        status = str(latest.get("overall_status") or "unknown")
        if status not in {"success", "partial"}:
            workflow_status = "blocked"
            workflow_details.append(f"{workflow}=status:{status}")
            continue
        if finished_at is None:
            workflow_status = "warn" if workflow_status == "ok" else workflow_status
            workflow_details.append(f"{workflow}=missing_finished_at")
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
