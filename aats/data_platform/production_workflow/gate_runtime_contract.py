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
    try:
        from aats.data_platform.governance._db_util import try_governance_db
        from aats.data_platform.governance.operational_state_db import (
            db_load_latest_workflow_runs,
        )

        engine, ok = try_governance_db()
        if ok:
            try:
                with Session(engine) as session:
                    latest_by_workflow = db_load_latest_workflow_runs(session)
                if latest_by_workflow:
                    return _augment_workflow_runs_with_decision_round(latest_by_workflow, project_root)
            finally:
                if engine is not None:
                    engine.dispose()
    except Exception:
        pass

    runs_dir = project_root / "artifacts/operations/workflow_runs"
    latest_by_workflow: dict[str, dict[str, Any]] = {}
    if not runs_dir.exists():
        return _augment_workflow_runs_with_decision_round(latest_by_workflow, project_root)

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
    return _augment_workflow_runs_with_decision_round(latest_by_workflow, project_root)


def _augment_workflow_runs_with_decision_round(
    latest_by_workflow: dict[str, dict[str, Any]],
    project_root: Path,
) -> dict[str, dict[str, Any]]:
    snapshot = _load_latest_decision_round_snapshot(project_root)
    if snapshot is None:
        return latest_by_workflow
    finished_at = snapshot.get("finished_at") or snapshot.get("started_at")
    candidate_dt = _parse_for_sort(
        str(finished_at) if finished_at else None,
        context="decision_round_snapshot.candidate",
    )
    if candidate_dt is None:
        return latest_by_workflow

    augmented = dict(latest_by_workflow)
    for workflow in ("governance_cycle", "decision_cycle"):
        current = augmented.get(workflow)
        current_dt = _parse_for_sort(
            str(current.get("finished_at") or current.get("started_at")) if current else None,
            context=f"decision_round_snapshot.{workflow}.current",
        )
        if current is None or current_dt is None or candidate_dt > current_dt:
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


def _load_latest_decision_round_snapshot(project_root: Path) -> dict[str, Any] | None:
    try:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance._db_util import try_governance_db
        from aats.data_platform.governance.decision_rounds_db import (
            db_load_latest_decision_round_snapshot,
        )

        engine, ok = try_governance_db()
        if ok:
            try:
                with Session(engine) as session:
                    snapshot = db_load_latest_decision_round_snapshot(session)
                if snapshot:
                    return {
                        "round_id": snapshot.get("round_id"),
                        "started_at": snapshot.get("started_at"),
                        "finished_at": snapshot.get("finished_at"),
                        "path": f"decision_round:{snapshot.get('round_id')}",
                        "data_source": "db",
                    }
            finally:
                if engine is not None:
                    engine.dispose()
    except Exception:
        pass

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

    contract: dict[str, Any] = {
        "version": 1,
        "environment": environment,
        "strict_environment": environment in {"staging", "prod"},
        "current_alerts": load_current_alerts(project_root),
        "latest_workflow_runs": _collect_latest_workflow_runs(project_root),
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
