"""Workflow scheduler that enqueues due workflows into the task queue."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aats.data_platform.governance._atomic_io import atomic_json_write
from aats.data_platform.governance._db_util import try_governance_db
from aats.data_platform.governance.rdp_task_db import db_create_task, db_has_active_task
from aats.data_platform.operations.environment_guard import guard_workflow_execution
from aats.data_platform.operations.workflow_dispatcher import (
    list_available_workflows,
    load_workflow_config,
)

log = logging.getLogger(__name__)

_STATE_PATH = Path("artifacts/operations/workflow_scheduler_state.json")
_WEEKDAY_MAP = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _state_path(project_root: Path) -> Path:
    return project_root / _STATE_PATH


def load_scheduler_state(project_root: Path) -> dict[str, Any]:
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_load_scheduler_state,
            )

            with Session(engine) as session:
                state = db_load_scheduler_state(session)
            # DB 是真源：空 state 也直接返回，避免把旧 slot 进度重新注入调度去重
            return state
        except Exception as exc:
            log.warning(
                "从 DB 读取 workflow scheduler state 失败 (%s)，退化到文件（stale 风险）",
                exc,
            )
        finally:
            if engine is not None:
                engine.dispose()

    path = _state_path(project_root)
    if not path.exists():
        return {"generated_at": None, "initialized_at": None, "workflows": {}}
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {"generated_at": None, "initialized_at": None, "workflows": {}}
    if not isinstance(payload, dict):
        return {"generated_at": None, "initialized_at": None, "workflows": {}}
    payload.setdefault("workflows", {})
    return payload


def save_scheduler_state(project_root: Path, state: dict[str, Any]) -> Path:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["generated_at"] = _utcnow().isoformat()

    # 顺序：DB 先、文件后。DB 写失败则文件保持旧状态，避免留下"DB 未同步"的
    # ghost slot 进度，被之后 DB 不可达时的 fallback 误当成真源。
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_save_scheduler_state,
            )

            with Session(engine) as session, session.begin():
                db_save_scheduler_state(session, state)
        except Exception as exc:
            log.exception("workflow scheduler state DB 同步失败，保存未完成")
            raise RuntimeError(
                f"workflow scheduler state DB 同步失败，状态未持久化到真源: {exc}"
            ) from exc
        finally:
            if engine is not None:
                engine.dispose()

    atomic_json_write(state, path)
    return path


def get_workflow_schedule(config: dict[str, Any]) -> dict[str, Any] | None:
    schedule = config.get("schedule")
    if not isinstance(schedule, dict):
        return None
    if schedule.get("enabled", True) is False:
        return None
    frequency = str(schedule.get("frequency") or "").strip().lower()
    if frequency not in {"daily", "weekly", "hourly"}:
        return None
    return schedule


def format_schedule(schedule: dict[str, Any] | None) -> str:
    if not schedule:
        return "未配置"
    frequency = str(schedule.get("frequency") or "").strip().lower()
    minute = int(schedule.get("minute_utc", 0))
    if frequency == "daily":
        return f"daily {int(schedule.get('hour_utc', 0)):02d}:{minute:02d} UTC"
    if frequency == "weekly":
        weekday = str(schedule.get("weekday_utc") or "SUN").upper()
        return (
            f"weekly {weekday} "
            f"{int(schedule.get('hour_utc', 0)):02d}:{minute:02d} UTC"
        )
    interval_hours = max(int(schedule.get("interval_hours", 1)), 1)
    return f"hourly every {interval_hours}h @ minute {minute:02d} UTC"


def _latest_slot_for_schedule(
    schedule: dict[str, Any],
    *,
    now: datetime,
) -> datetime:
    frequency = str(schedule.get("frequency") or "").strip().lower()
    minute = int(schedule.get("minute_utc", 0))

    if frequency == "daily":
        hour = int(schedule.get("hour_utc", 0))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            candidate -= timedelta(days=1)
        return candidate

    if frequency == "weekly":
        hour = int(schedule.get("hour_utc", 0))
        weekday = _WEEKDAY_MAP.get(str(schedule.get("weekday_utc") or "SUN").upper(), 6)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta_days = (candidate.weekday() - weekday) % 7
        candidate -= timedelta(days=delta_days)
        if candidate > now:
            candidate -= timedelta(days=7)
        return candidate

    interval_hours = max(int(schedule.get("interval_hours", 1)), 1)
    anchor = datetime(1970, 1, 1, 0, minute, tzinfo=UTC)
    elapsed_seconds = int((now - anchor).total_seconds())
    slot_seconds = interval_hours * 3600
    slot_index = elapsed_seconds // slot_seconds
    return anchor + timedelta(seconds=slot_index * slot_seconds)


def _load_scheduled_workflows(project_root: Path) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    scheduled: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for workflow_name in list_available_workflows(project_root):
        config = load_workflow_config(project_root, workflow_name)
        schedule = get_workflow_schedule(config)
        if schedule:
            scheduled.append((workflow_name, config, schedule))
    return scheduled


def enqueue_due_workflows(
    project_root: Path,
    *,
    now: datetime | None = None,
    actor: str = "scheduler",
    dry_run: bool = False,
    save_state: bool = True,
    initialize_if_missing: bool = True,
) -> dict[str, Any]:
    now = now or _utcnow()
    report: dict[str, Any] = {
        "ok": True,
        "scheduler_at": now.isoformat(),
        "actor": actor,
        "dry_run": dry_run,
        "initialized": False,
        "enqueued": [],
        "skipped": [],
        "errors": [],
    }

    # 跨进程互斥：同一时刻只允许一个 scheduler 运行，避免 load→compute→save 跨事务竞态
    # 导致同一时间窗被重复 enqueue。DB 不可用时退化为单进程（文件 state），也是历史行为。
    lock_engine = None
    lock_session = None
    if not dry_run:
        try:
            from sqlalchemy.orm import Session as SQLSession

            from aats.data_platform.governance._db_util import try_governance_db
            from aats.data_platform.governance.operational_state_db import (
                try_acquire_scheduler_lock,
            )

            lock_engine, lock_ok = try_governance_db()
            if lock_ok and lock_engine is not None:
                lock_session = SQLSession(lock_engine)
                if not try_acquire_scheduler_lock(lock_session):
                    lock_session.close()
                    if lock_engine is not None:
                        lock_engine.dispose()
                    report["skipped"].append(
                        {
                            "workflow": "*",
                            "reason": "另一个 scheduler 正在运行（advisory lock 被持有）",
                        },
                    )
                    return report
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("scheduler advisory lock 获取失败，继续运行但无并发保护: %s", exc)
            lock_session = None

    try:
        return _enqueue_due_workflows_locked(
            project_root,
            now=now,
            actor=actor,
            dry_run=dry_run,
            save_state=save_state,
            initialize_if_missing=initialize_if_missing,
            report=report,
        )
    finally:
        if lock_session is not None:
            try:
                from aats.data_platform.governance.operational_state_db import (
                    release_scheduler_lock,
                )

                release_scheduler_lock(lock_session)
            finally:
                lock_session.close()
        if lock_engine is not None:
            lock_engine.dispose()


def _enqueue_due_workflows_locked(
    project_root: Path,
    *,
    now: datetime,
    actor: str,
    dry_run: bool,
    save_state: bool,
    initialize_if_missing: bool,
    report: dict[str, Any],
) -> dict[str, Any]:
    from aats.data_platform.db import get_session

    state = load_scheduler_state(project_root)

    scheduled_workflows = _load_scheduled_workflows(project_root)
    if initialize_if_missing and not state.get("initialized_at"):
        for workflow_name, _, schedule in scheduled_workflows:
            slot = _latest_slot_for_schedule(schedule, now=now)
            state.setdefault("workflows", {})[workflow_name] = {
                "last_processed_slot": slot.isoformat(),
                "last_action": "initialized",
                "last_checked_at": now.isoformat(),
            }
        state["initialized_at"] = now.isoformat()
        report["initialized"] = True
        report["skipped"].append(
            {
                "workflow": "*",
                "reason": "首次启动，仅建立调度基线，不补跑历史窗口。",
            },
        )
        if save_state and not dry_run:
            save_scheduler_state(project_root, state)
        return report

    for workflow_name, _, schedule in scheduled_workflows:
        slot = _latest_slot_for_schedule(schedule, now=now)
        slot_key = slot.isoformat()
        workflow_state = state.setdefault("workflows", {}).setdefault(workflow_name, {})
        workflow_state["last_checked_at"] = now.isoformat()
        workflow_state["schedule"] = format_schedule(schedule)

        if workflow_state.get("last_processed_slot") == slot_key:
            report["skipped"].append(
                {
                    "workflow": workflow_name,
                    "reason": "当前窗口已处理",
                    "slot": slot_key,
                },
            )
            continue

        guard = guard_workflow_execution(workflow_name)
        if not guard.allowed:
            if not dry_run:
                workflow_state["last_processed_slot"] = slot_key
                workflow_state["last_action"] = "blocked_by_environment"
                workflow_state["last_reason"] = guard.reason
            report["skipped"].append(
                {
                    "workflow": workflow_name,
                    "reason": guard.reason,
                    "slot": slot_key,
                },
            )
            continue

        try:
            with get_session() as session:
                active_task = db_has_active_task(session, workflow_name)
                if active_task:
                    if not dry_run:
                        workflow_state["last_processed_slot"] = slot_key
                        workflow_state["last_action"] = "active_task_present"
                        workflow_state["last_reason"] = active_task["task_id"]
                    report["skipped"].append(
                        {
                            "workflow": workflow_name,
                            "reason": "已有 active task",
                            "slot": slot_key,
                            "task_id": active_task["task_id"],
                        },
                    )
                    continue
                task_id = None
                if not dry_run:
                    task_id = db_create_task(
                        session,
                        workflow=workflow_name,
                        requested_by=actor,
                    )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("scheduler failed to enqueue workflow %s", workflow_name)
            report["ok"] = False
            report["errors"].append(
                {
                    "workflow": workflow_name,
                    "slot": slot_key,
                    "error": str(exc),
                },
            )
            continue

        if not dry_run:
            workflow_state["last_processed_slot"] = slot_key
            workflow_state["last_action"] = "enqueued"
            workflow_state["last_task_id"] = task_id
            workflow_state["last_reason"] = "scheduled"
        report["enqueued"].append(
            {
                "workflow": workflow_name,
                "slot": slot_key,
                "task_id": task_id,
            },
        )

    if save_state and not dry_run:
        save_scheduler_state(project_root, state)

    return report
