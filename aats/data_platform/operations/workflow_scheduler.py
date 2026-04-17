"""Workflow scheduler that enqueues due workflows into the task queue."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aats.data_platform.db import get_session
from aats.data_platform.governance._atomic_io import atomic_json_write
from aats.data_platform.governance._db_util import try_governance_db
from aats.data_platform.governance.rdp_task_db import (
    db_create_task_if_idle,
    db_get_latest_task_for_workflow,
    db_has_active_task,
)
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
_BOOTSTRAP_SEQUENCE = ("data_maintenance", "research_cycle")


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
    empty_state = {
        "generated_at": None,
        "initialized_at": None,
        "bootstrap_stage": None,
        "bootstrap_completed_at": None,
        "workflows": {},
    }
    if not path.exists():
        return dict(empty_state)
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return dict(empty_state)
    if not isinstance(payload, dict):
        return dict(empty_state)
    payload.setdefault("workflows", {})
    payload.setdefault("bootstrap_stage", None)
    payload.setdefault("bootstrap_completed_at", None)
    # 注：H3 审查提出"DB 不可达时文件里 stale bootstrap_stage 会误导调度器"
    # 的担忧。实际场景需要 DB 不可达 + 文件被人工篡改（或来自旧代码版本）双重
    # 巧合才会触发；而 save_scheduler_state 写入路径在 DB 可用时一定先写 DB 再
    # 写文件，所以文件里 bootstrap_stage 等同于最近一次 DB 同步后的快照。
    # 强制 reset 会破坏 graceful degrade 语义（7 个 scheduler 测试失败为证）。
    # 保留 setdefault 即可，对应的 regression 通过 DB 端 meta-row sentinel 测试
    # (test_operational_state_db.py) 保护。
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


def _parse_iso_dt(value: Any) -> datetime | None:
    """Scheduler timestamp parse; illegal → None to keep the scheduler running.

    A corrupt workflow_runs/*.json timestamp should skip that run from
    scheduling, not abort the whole tick.
    """
    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    try:
        return parse_iso_datetime_utc(value, context="workflow_scheduler")
    except ValueError:
        return None


def _canonical_slot_key(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    else:
        parsed = _parse_iso_dt(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def _initialize_bootstrap_state(
    state: dict[str, Any],
    *,
    now: datetime,
    scheduled_workflows: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    workflows_state = state.setdefault("workflows", {})
    for workflow_name, _, schedule in scheduled_workflows:
        workflow_state = workflows_state.setdefault(workflow_name, {})
        workflow_state["last_checked_at"] = now.isoformat()
        workflow_state["schedule"] = format_schedule(schedule)
        if workflow_name in _BOOTSTRAP_SEQUENCE:
            workflow_state.setdefault("last_action", "bootstrap_pending")
            continue
        slot = _latest_slot_for_schedule(schedule, now=now)
        workflow_state["last_processed_slot"] = _canonical_slot_key(slot)
        workflow_state["last_action"] = "initialized"
    state["initialized_at"] = now.isoformat()
    state["bootstrap_stage"] = _BOOTSTRAP_SEQUENCE[0]


def _current_bootstrap_stage(
    state: dict[str, Any],
    *,
    now: datetime,
) -> str | None:
    initialized_at = _parse_iso_dt(state.get("initialized_at"))
    if initialized_at is None:
        return None

    # M-A3-1 修复：``bootstrap_completed_at`` 和 ``bootstrap_stage`` 是互斥的两个
    # 状态——要么 bootstrap 已完成（completed_at 有值，stage 为 None），要么正
    # 在某一阶段（stage 有值，completed_at 为 None）。但如果因为旧代码 bug /
    # 人工编辑 / 文件被 race 改过，两个字段同时有值，应该以 completed_at 为
    # 权威信号：bootstrap 既然已经完成，绝不能再触发一次（否则 data_maintenance
    # 会被重复入队）。这里强制清理 stage 并短路返回 None。
    if state.get("bootstrap_completed_at"):
        if state.get("bootstrap_stage"):
            log.warning(
                "scheduler state inconsistent: bootstrap_completed_at=%s 与 bootstrap_stage=%s "
                "同时存在，以 completed_at 为权威，清空 stage",
                state.get("bootstrap_completed_at"),
                state.get("bootstrap_stage"),
            )
            state["bootstrap_stage"] = None
        return None

    workflows_state = state.setdefault("workflows", {})
    stage = state.get("bootstrap_stage")
    if stage in _BOOTSTRAP_SEQUENCE:
        return str(stage)

    with get_session() as session:
        data_done = db_get_latest_task_for_workflow(
            session,
            "data_maintenance",
            statuses=("done",),
            requested_after=initialized_at,
        )
        if not data_done:
            state["bootstrap_stage"] = "data_maintenance"
            workflows_state.setdefault("data_maintenance", {})["last_action"] = "bootstrap_pending"
            return "data_maintenance"

        research_done = db_get_latest_task_for_workflow(
            session,
            "research_cycle",
            statuses=("done",),
            requested_after=initialized_at,
        )
        if not research_done:
            state["bootstrap_stage"] = "research_cycle"
            workflows_state.setdefault("research_cycle", {})["last_action"] = "bootstrap_pending"
            return "research_cycle"

    state["bootstrap_stage"] = None
    state["bootstrap_completed_at"] = now.isoformat()
    return None


def _enqueue_single_workflow(
    *,
    workflow_name: str,
    schedule: dict[str, Any],
    workflow_state: dict[str, Any],
    slot_key: str,
    actor: str,
    dry_run: bool,
    report: dict[str, Any],
    action_label: str = "enqueued",
    reason_label: str = "scheduled",
) -> None:
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
        return

    try:
        with get_session() as session:
            # dry_run 只读：用 has_active_task 照查看有没有活跃任务即可，不插入。
            if dry_run:
                active_task = db_has_active_task(session, workflow_name)
                if active_task:
                    report["skipped"].append(
                        {
                            "workflow": workflow_name,
                            "reason": "已有 active task",
                            "slot": slot_key,
                            "task_id": active_task["task_id"],
                        },
                    )
                    return
                task_id = None
            else:
                # 真插入：原子创建，如已有活跃任务则由 ON CONFLICT 分支返回
                # existing，避免 scheduler-vs-operator 并发打到 IntegrityError。
                task_id, active_task = db_create_task_if_idle(
                    session,
                    workflow=workflow_name,
                    requested_by=actor,
                )
                if task_id is None:
                    workflow_state["last_processed_slot"] = slot_key
                    workflow_state["last_action"] = "active_task_present"
                    workflow_state["last_reason"] = (
                        active_task["task_id"] if active_task else "unknown_active_task"
                    )
                    report["skipped"].append(
                        {
                            "workflow": workflow_name,
                            "reason": "已有 active task",
                            "slot": slot_key,
                            "task_id": (active_task or {}).get("task_id"),
                        },
                    )
                    return
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
        return

    if not dry_run:
        workflow_state["last_processed_slot"] = slot_key
        workflow_state["last_action"] = action_label
        workflow_state["last_task_id"] = task_id
        workflow_state["last_reason"] = reason_label
    report["enqueued"].append(
        {
            "workflow": workflow_name,
            "slot": slot_key,
            "task_id": task_id,
        },
    )


def _run_bootstrap_sequence(
    *,
    state: dict[str, Any],
    scheduled_workflows: list[tuple[str, dict[str, Any], dict[str, Any]]],
    now: datetime,
    actor: str,
    dry_run: bool,
    report: dict[str, Any],
) -> bool:
    stage = _current_bootstrap_stage(state, now=now)
    if not stage:
        return False

    workflows_by_name = {name: schedule for name, _, schedule in scheduled_workflows}
    schedule = workflows_by_name.get(stage)
    if schedule is None:
        report["skipped"].append(
            {
                "workflow": stage,
                "reason": "bootstrap workflow 缺少 schedule 配置",
            },
        )
        return True

    workflow_state = state.setdefault("workflows", {}).setdefault(stage, {})
    workflow_state["last_checked_at"] = now.isoformat()
    workflow_state["schedule"] = format_schedule(schedule)
    slot_key = _canonical_slot_key(_latest_slot_for_schedule(schedule, now=now)) or ""

    initialized_at = _parse_iso_dt(state.get("initialized_at"))
    # 一次 session 内同时查"最近 done"和"最近任意状态"，前者驱动门控推进，
    # 后者用于 bootstrap 卡 failed 时的告警——failed 任务不会满足 done 过滤器，
    # 导致 bootstrap 永远停在该阶段（除非人工干预），必须让运营者能看到。
    with get_session() as session:
        latest_success = db_get_latest_task_for_workflow(
            session,
            stage,
            statuses=("done",),
            requested_after=initialized_at,
        )
        latest_any = db_get_latest_task_for_workflow(
            session,
            stage,
            requested_after=initialized_at,
        )

    if latest_success:
        if stage == _BOOTSTRAP_SEQUENCE[0]:
            next_stage = _BOOTSTRAP_SEQUENCE[1]
            log.info(
                "scheduler bootstrap: %s 完成，推进到下一阶段 %s (latest_done_task=%s)",
                stage, next_stage, latest_success.get("task_id"),
            )
            state["bootstrap_stage"] = next_stage
            workflow_state["last_action"] = "bootstrap_completed"
            report["skipped"].append(
                {
                    "workflow": stage,
                    "reason": "bootstrap 已完成，切换到 research_cycle",
                    "slot": slot_key,
                },
            )
            return _run_bootstrap_sequence(
                state=state,
                scheduled_workflows=scheduled_workflows,
                now=now,
                actor=actor,
                dry_run=dry_run,
                report=report,
            )

        log.info(
            "scheduler bootstrap: %s 完成，bootstrap 阶段全部结束 (latest_done_task=%s)",
            stage, latest_success.get("task_id"),
        )
        state["bootstrap_stage"] = None
        state["bootstrap_completed_at"] = now.isoformat()
        workflow_state["last_action"] = "bootstrap_completed"
        report["skipped"].append(
            {
                "workflow": stage,
                "reason": "bootstrap 全部完成，等待下一个调度周期",
                "slot": slot_key,
            },
        )
        return True

    # 每次 tick 都会走到这里，INFO 会淹没日志 → 降级为 DEBUG。
    # 阶段推进和 bootstrap 收尾的两条 INFO 保留，确保状态变化可观测。
    log.debug(
        "scheduler bootstrap: 当前阶段 %s 等待完成，其它 workflow 全部被门控 (dry_run=%s)",
        stage, dry_run,
    )

    # bootstrap 只认 status=done。若最近一条任务是 failed，阶段会被无限卡住，
    # 必须让运营者通过 API report.warnings 看到并手工介入。
    #
    # M2 设计：这里只写 report.warnings（操作人面向的真源信号），不写
    # workflow_state["last_reason"] —— _enqueue_single_workflow 会用
    # reason_label="bootstrap" 覆盖 last_reason，在那里设 warning 语义留不住。
    # 本 tick 仍需继续 _enqueue_single_workflow 让 data_maintenance 自动重试
    # （fail-closed 会把 research_cycle 等其它 workflow 仍然挡在门外，由
    # bootstrap 阶段机制本身保证）。
    if latest_any is not None and str(latest_any.get("status")) == "failed":
        warnings = report.setdefault("warnings", [])
        warning_entry = {
            "workflow": stage,
            "reason": "bootstrap 阶段最近一次任务失败，需人工介入",
            "task_id": latest_any.get("task_id"),
            "error_message": latest_any.get("error_message"),
            "finished_at": latest_any.get("finished_at"),
        }
        warnings.append(warning_entry)
        log.warning(
            "scheduler bootstrap: 阶段 %s 最近一次任务失败 task_id=%s error=%s",
            stage,
            latest_any.get("task_id"),
            latest_any.get("error_message"),
        )
    _enqueue_single_workflow(
        workflow_name=stage,
        schedule=schedule,
        workflow_state=workflow_state,
        slot_key=slot_key,
        actor=actor,
        dry_run=dry_run,
        report=report,
        action_label="bootstrap_enqueued",
        reason_label="bootstrap",
    )
    report["skipped"].extend(
        {
            "workflow": workflow_name,
            "reason": f"cold-start bootstrap 等待 {stage} 完成",
            "slot": _canonical_slot_key(_latest_slot_for_schedule(schedule_cfg, now=now)) or "",
        }
        for workflow_name, _, schedule_cfg in scheduled_workflows
        if workflow_name != stage
    )
    return True


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
    state = load_scheduler_state(project_root)

    scheduled_workflows = _load_scheduled_workflows(project_root)
    if initialize_if_missing and not state.get("initialized_at"):
        _initialize_bootstrap_state(
            state,
            now=now,
            scheduled_workflows=scheduled_workflows,
        )
        report["initialized"] = True
        report["skipped"].append(
            {
                "workflow": "*",
                "reason": "首次启动仅建立调度基线，并进入 cold-start bootstrap：先数据刷新，再研究流程。",
            },
        )
        if save_state and not dry_run:
            save_scheduler_state(project_root, state)
        return report

    if _run_bootstrap_sequence(
        state=state,
        scheduled_workflows=scheduled_workflows,
        now=now,
        actor=actor,
        dry_run=dry_run,
        report=report,
    ):
        if save_state and not dry_run:
            save_scheduler_state(project_root, state)
        return report

    for workflow_name, _, schedule in scheduled_workflows:
        slot = _latest_slot_for_schedule(schedule, now=now)
        slot_key = _canonical_slot_key(slot) or ""
        workflow_state = state.setdefault("workflows", {}).setdefault(workflow_name, {})
        workflow_state["last_checked_at"] = now.isoformat()
        workflow_state["schedule"] = format_schedule(schedule)

        if _canonical_slot_key(workflow_state.get("last_processed_slot")) == slot_key:
            report["skipped"].append(
                {
                    "workflow": workflow_name,
                    "reason": "当前窗口已处理",
                    "slot": slot_key,
                },
            )
            continue

        _enqueue_single_workflow(
            workflow_name=workflow_name,
            schedule=schedule,
            workflow_state=workflow_state,
            slot_key=slot_key,
            actor=actor,
            dry_run=dry_run,
            report=report,
        )

    if save_state and not dry_run:
        save_scheduler_state(project_root, state)

    return report
