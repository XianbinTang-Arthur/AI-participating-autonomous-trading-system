"""Canonical runtime data contract for pre-apply gate checks."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _safe_load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


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
        candidate_dt = _parse_iso_datetime(str(finished_at) if finished_at else None)
        current_dt = _parse_iso_datetime(
            str(current.get("finished_at") or current.get("started_at"))
            if current else None
        )
        if current is None or (
            candidate_dt is not None and current_dt is not None and candidate_dt > current_dt
        ) or (current_dt is None and candidate_dt is not None):
            latest_by_workflow[workflow] = candidate
    return latest_by_workflow


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
