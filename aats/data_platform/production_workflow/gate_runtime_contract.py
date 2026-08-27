"""Canonical runtime data contract for pre-apply gate checks."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aats.data_platform.governance._time_util import parse_iso_datetime_utc

log = logging.getLogger(__name__)

_GATE_CRITICAL_WORKFLOWS = (
    "reliability_cycle",
    "data_maintenance",
    "governance_cycle",
    "decision_cycle",
)


def _safe_load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _parse_for_sort(value: str | None, *, context: str) -> datetime | None:
    """Timestamp parse for run-aggregation sort keys.

    Wraps :func:`parse_iso_datetime_utc` with illegal-as-None policy so a single
    corrupt artifact cannot prevent sibling runs from being compared. Must not
    be used for gate checks — those should let :class:`ValueError` propagate.
    """
    try:
        return parse_iso_datetime_utc(value, context=context)
    except ValueError as exc:
        log.warning("gate_runtime_contract: ignoring illegal timestamp: %s", exc)
        return None


def _collect_latest_workflow_runs(project_root: Path) -> dict[str, dict[str, Any]]:
    from aats.data_platform.governance._db_util import (
        has_explicit_governance_db_configuration,
        try_governance_db,
    )
    from aats.data_platform.governance._exceptions import DBUnavailableError
    from aats.data_platform.governance.operational_state_db import (
        db_load_latest_workflow_runs,
    )
    from aats.data_platform.governance.rdp_task_db import (
        db_get_latest_task_for_workflow,
    )

    managed_truth = has_explicit_governance_db_configuration(project_root)
    engine, ok = try_governance_db()
    if ok:
        try:
            with Session(engine) as session:
                latest_by_workflow = db_load_latest_workflow_runs(session)
                latest_tasks = {
                    workflow: db_get_latest_task_for_workflow(session, workflow)
                    for workflow in _GATE_CRITICAL_WORKFLOWS
                }
        except Exception as exc:
            if managed_truth:
                raise DBUnavailableError(
                    "governance DB workflow history read failed; stale file fallback denied"
                ) from exc
        else:
            # Managed DB success is authoritative even when the table is empty.
            # Decision-round augmentation below also stays DB-only in this mode.
            if managed_truth or latest_by_workflow:
                augmented = _augment_workflow_runs_with_decision_round(
                    latest_by_workflow,
                    project_root,
                    require_managed_db_truth=managed_truth,
                )
                return _reconcile_workflow_runs_with_task_attempts(
                    augmented,
                    latest_tasks,
                )
        finally:
            if engine is not None:
                engine.dispose()
    elif managed_truth:
        raise DBUnavailableError(
            "governance DB unavailable for workflow history; stale file fallback denied"
        )

    runs_dir = project_root / "artifacts/operations/workflow_runs"
    latest_by_workflow: dict[str, dict[str, Any]] = {}
    if not runs_dir.exists():
        return _augment_workflow_runs_with_decision_round(
            latest_by_workflow,
            project_root,
            require_managed_db_truth=False,
        )

    for path in sorted(runs_dir.glob("*.json"), reverse=True):
        payload = _safe_load_json(path)
        if not isinstance(payload, dict):
            continue
        workflow = str(payload.get("workflow") or "").strip()
        if not workflow:
            continue
        finished_at = payload.get("finished_at") or payload.get("started_at")
        candidate = {
            "run_id": payload.get("run_id"),
            "workflow": workflow,
            "overall_status": payload.get("overall_status"),
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "path": str(path),
        }
        current = latest_by_workflow.get(workflow)
        candidate_dt = _parse_for_sort(
            str(finished_at) if finished_at else None,
            context=f"workflow_runs.{workflow}.candidate",
        )
        current_dt = _parse_for_sort(
            str(current.get("finished_at") or current.get("started_at")) if current else None,
            context=f"workflow_runs.{workflow}.current",
        )
        if current is None or (
            candidate_dt is not None and current_dt is not None and candidate_dt > current_dt
        ) or (current_dt is None and candidate_dt is not None):
            latest_by_workflow[workflow] = candidate
    return _augment_workflow_runs_with_decision_round(
        latest_by_workflow,
        project_root,
        require_managed_db_truth=False,
    )


def _reconcile_workflow_runs_with_task_attempts(
    latest_by_workflow: dict[str, dict[str, Any]],
    latest_tasks: dict[str, dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    """Require one exact successful report for the latest completed attempt.

    The report write and queue terminal update are separate transactions.  A
    timestamp comparison cannot prove they describe the same execution: the
    worker may save a success report and then fail to mark the task ``done``.
    The managed contract therefore requires all three identity predicates:
    latest task status is exactly ``done``, report ``run_id`` matches the
    logical queue run, and report ``attempt_no`` matches that task attempt.
    """
    reconciled = dict(latest_by_workflow)
    for workflow in _GATE_CRITICAL_WORKFLOWS:
        # Offline callers may intentionally reconcile only a subset.  The
        # managed collector supplies every critical workflow key; an explicit
        # ``None`` is therefore the authoritative "no queue task" result.
        if workflow not in latest_tasks:
            continue
        task = latest_tasks.get(workflow)
        report = reconciled.get(workflow)
        if not isinstance(task, dict):
            if isinstance(report, dict):
                reconciled[workflow] = {
                    "run_id": report.get("run_id"),
                    "workflow": workflow,
                    "overall_status": "task_missing",
                    "started_at": report.get("started_at"),
                    "finished_at": None,
                    "reconciliation_required": True,
                    "reason_code": "latest_queue_task_missing",
                    "data_source": "db_task_queue",
                }
            continue

        task_status = task.get("status")
        task_run_id = task.get("run_id")
        task_attempt_no = task.get("attempt_no")
        report_run_id = report.get("run_id") if isinstance(report, dict) else None
        report_attempt_no = (
            report.get("attempt_no") if isinstance(report, dict) else None
        )
        identity_matches = (
            task_status == "done"
            and task.get("workflow") == workflow
            and isinstance(task_run_id, str)
            and bool(task_run_id.strip())
            and type(task_attempt_no) is int
            and task_attempt_no >= 1
            and isinstance(report, dict)
            and report.get("workflow") == workflow
            and report_run_id == task_run_id
            and type(report_attempt_no) is int
            and report_attempt_no == task_attempt_no
        )
        if identity_matches:
            reconciled[workflow] = {
                **report,
                "task_id": task.get("task_id"),
                "task_attempt_no": task_attempt_no,
                "queue_task_status": "done",
                "queue_identity_matched": True,
            }
            continue

        task_time_raw = task.get("started_at") or task.get("requested_at")
        if task_status != "done":
            reason_code = "latest_queue_task_not_done"
        elif report is None:
            reason_code = "matching_workflow_report_missing"
        else:
            reason_code = "workflow_report_identity_mismatch"
        reconciled[workflow] = {
            "run_id": task.get("run_id") or task.get("task_id"),
            "workflow": workflow,
            "overall_status": f"task_{task_status or 'unknown'}",
            "started_at": task_time_raw,
            "finished_at": None,
            "task_id": task.get("task_id"),
            "task_attempt_no": task_attempt_no,
            "report_run_id": report_run_id,
            "report_attempt_no": report_attempt_no,
            "reconciliation_required": True,
            "reason_code": reason_code,
            "data_source": "db_task_queue",
        }
    return reconciled


def _augment_workflow_runs_with_decision_round(
    latest_by_workflow: dict[str, dict[str, Any]],
    project_root: Path,
    *,
    require_managed_db_truth: bool,
) -> dict[str, dict[str, Any]]:
    snapshot = _load_latest_decision_round_snapshot(
        project_root,
        require_managed_db_truth=require_managed_db_truth,
    )
    if snapshot is None:
        return latest_by_workflow
    if snapshot.get("status") != "succeeded" or snapshot.get("phase") != "phase6":
        return latest_by_workflow
    finished_at = snapshot.get("finished_at") or snapshot.get("started_at")
    candidate_dt = _parse_for_sort(
        str(finished_at) if finished_at else None,
        context="decision_round_snapshot.candidate",
    )
    if candidate_dt is None:
        return latest_by_workflow

    augmented = dict(latest_by_workflow)
    # Phase 6 可以证明 decision_cycle 完成，不能替代独立执行 quality monitor
    # 的 governance_cycle。显式 failed/degraded decision run 也绝不被合成成功覆盖。
    workflow = "decision_cycle"
    current = augmented.get(workflow)
    current_status = str((current or {}).get("overall_status") or "")
    current_dt = _parse_for_sort(
        str(current.get("finished_at") or current.get("started_at")) if current else None,
        context=f"decision_round_snapshot.{workflow}.current",
    )
    may_synthesize = current is None or current_status in {"success", "partial"}
    if may_synthesize and (current is None or current_dt is None or candidate_dt > current_dt):
        augmented[workflow] = {
            "run_id": snapshot.get("round_id"),
            "workflow": workflow,
            "overall_status": "success",
            "started_at": snapshot.get("started_at"),
            "finished_at": snapshot.get("finished_at"),
            "path": snapshot.get("path"),
            "synthetic_from": snapshot.get("data_source"),
        }
    return augmented


def _load_latest_decision_round_snapshot(
    project_root: Path,
    *,
    require_managed_db_truth: bool,
) -> dict[str, Any] | None:
    from aats.data_platform.governance._db_util import (
        has_explicit_governance_db_configuration,
        try_governance_db,
    )
    from aats.data_platform.governance._exceptions import DBUnavailableError
    from aats.data_platform.governance.decision_rounds_db import (
        db_load_latest_decision_round_snapshot,
    )

    managed_truth = has_explicit_governance_db_configuration(project_root)
    engine, ok = try_governance_db()
    if ok:
        try:
            with Session(engine) as session:
                snapshot = db_load_latest_decision_round_snapshot(session)
        except Exception as exc:
            if require_managed_db_truth and managed_truth:
                raise DBUnavailableError(
                    "governance DB latest decision round read failed; stale file fallback denied"
                ) from exc
        else:
            if snapshot:
                manifest = snapshot.get("manifest") or {}
                return {
                    "round_id": snapshot.get("round_id"),
                    "started_at": snapshot.get("started_at"),
                    "finished_at": snapshot.get("finished_at"),
                    "status": manifest.get("status"),
                    "phase": manifest.get("phase"),
                    "path": f"decision_round:{snapshot.get('round_id')}",
                    "data_source": "db",
                }
            if require_managed_db_truth and managed_truth:
                return None
        finally:
            if engine is not None:
                engine.dispose()
    elif require_managed_db_truth and managed_truth:
        raise DBUnavailableError(
            "governance DB unavailable for latest decision round; stale file fallback denied"
        )

    rounds_dir = project_root / "artifacts" / "decision_rounds"
    if not rounds_dir.exists():
        return None
    latest_dirs = sorted((p for p in rounds_dir.iterdir() if p.is_dir()), reverse=True)
    if not latest_dirs:
        return None
    latest_dir = latest_dirs[0]
    manifest = _safe_load_json(latest_dir / "round_manifest.json")
    if not isinstance(manifest, dict):
        return None
    return {
        "round_id": manifest.get("round_id", latest_dir.name),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "status": manifest.get("status"),
        "phase": manifest.get("phase"),
        "path": str(latest_dir),
        "data_source": "file",
    }


def build_gate_runtime_contract(
    project_root: Path,
    *,
    environment: str,
) -> dict[str, Any]:
    """Build the canonical runtime signals consumed by gate rules."""
    from aats.data_platform.operations.alerting import load_current_alerts

    try:
        latest_workflow_runs = _collect_latest_workflow_runs(project_root)
        workflow_runs_available = True
    except Exception as exc:
        log.error(
            "gate runtime workflow truth unavailable; blocking apply (%s)",
            type(exc).__name__,
        )
        latest_workflow_runs = {}
        workflow_runs_available = False

    contract: dict[str, Any] = {
        "version": 1,
        "environment": environment,
        "strict_environment": environment in {"staging", "prod"},
        "current_alerts": load_current_alerts(project_root),
        "latest_workflow_runs": latest_workflow_runs,
        "workflow_runs_available": workflow_runs_available,
    }

    try:
        from aats.data_platform.live_query_adapter import check_live_db_health

        contract["live_db_health"] = check_live_db_health()
    except Exception as exc:
        contract["live_db_health"] = {
            "healthy": False,
            "connection_ok": False,
            "tables_checked": {},
            "errors": [str(exc)],
        }

    return contract


def get_gate_runtime_contract(ctx: dict[str, Any]) -> dict[str, Any]:
    """Read runtime signals from the canonical contract with fallback compatibility."""
    contract = ctx.get("runtime_contract")
    if isinstance(contract, dict):
        return contract
    environment = str(ctx.get("environment") or "dev").lower()
    return {
        "version": 0,
        "environment": environment,
        "strict_environment": environment in {"staging", "prod"},
        "current_alerts": ctx.get("current_alerts"),
        "latest_workflow_runs": ctx.get("latest_workflow_runs") or {},
        "live_db_health": ctx.get("live_db_health") or {},
    }


def runtime_environment(ctx: dict[str, Any]) -> str:
    return str(get_gate_runtime_contract(ctx).get("environment") or "dev").lower()


def runtime_strict_environment(ctx: dict[str, Any]) -> bool:
    return bool(get_gate_runtime_contract(ctx).get("strict_environment"))


def runtime_current_alerts(ctx: dict[str, Any]) -> dict[str, Any] | None:
    alerts = get_gate_runtime_contract(ctx).get("current_alerts")
    return alerts if isinstance(alerts, dict) else None


def runtime_live_db_health(ctx: dict[str, Any]) -> dict[str, Any]:
    live_db = get_gate_runtime_contract(ctx).get("live_db_health")
    return live_db if isinstance(live_db, dict) else {}


def runtime_latest_workflow_runs(ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    runs = get_gate_runtime_contract(ctx).get("latest_workflow_runs")
    return runs if isinstance(runs, dict) else {}
