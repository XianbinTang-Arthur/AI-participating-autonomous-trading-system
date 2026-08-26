"""RDP Task Queue DB — governance.rdp_task_queue 读写层.

提供 4 个函数，用于 gateway → daemon 的任务桥接：
  - db_create_task:        INSERT pending 任务（gateway 调用）
  - db_claim_next_task:    SELECT FOR UPDATE SKIP LOCKED 领取任务（daemon 调用）
  - db_update_task_status: UPDATE 任务状态（daemon 调用）
  - db_get_recent_tasks:   SELECT 最近 N 条任务（gateway 查询用）

依赖:
  - governance.rdp_task_queue 表 (migration 0014)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.governance.rdp_runs_db import (
    db_append_run_event,
    db_create_run,
    db_delete_unstarted_run,
    db_mark_run_requeued,
    db_mark_run_running,
    db_mark_run_terminal,
    new_run_id,
    priority_class_for_trigger,
    trigger_kind_for_request,
)

log = logging.getLogger(__name__)

VALID_WORKFLOWS = {
    "data_maintenance",
    "governance_cycle",
    "research_cycle",
    "decision_cycle",
    "release_cycle",
    # RDP Bug 1: observation_cycle 从 decision_cycle.observation_check 拆出，
    # hourly 推进 parameter_releases.observation_status，避免 24h 观察窗
    # 被 weekly decision_cycle 拉长到 7 天。
    "observation_cycle",
    # RDP roadmap R1: reliability_cycle 从 decision_cycle.reliability_check
    # 拆出 hourly，避免 current_alerts.json 因 decision_cycle 延迟而长期缺失
    # 导致 pre_apply_gate 降级放行。
    "reliability_cycle",
    # P1-D Phase 1A: microstructure Silver ETL (每 15min 构建一次 5 张
    # silver.market_*_15m 表)。走独立 workflow, 不进 daily_ingest 的 cron
    # (附录 C.3 强主张); ix_rdp_task_one_active_per_workflow 唯一索引自动
    # 串行化同 workflow 的 pending+running task。详见 §7.3 / 附录 E #7。
    "microstructure_silver_15m",
    # P0-c Option A (2026-04-20, 诊断 docs/review/p0c_candles_silver_stale_diagnosis_2026_04_20.md):
    # candles 15m rolling ingest (每 15min 从 OKX REST 增量拉 15m K 线),
    # 给路线 A research phase 0 提供 OHLC 对照基线 intra-day 新鲜度。
    # 与 microstructure_silver_15m 同 cadence 对齐; Gold/Gap/Funding 仍由
    # data_maintenance 日批负责, 本 workflow 只做 collect。
    # deploy-time 发现: scheduler 需把 workflow 名加到白名单才会被真正 enqueue。
    "candles_rolling_15m",
    # Platform hygiene (2026-04-23, 恢复 2026-04-20 后停采的 OI/mark/long-short):
    # 每小时 rolling 拉取 OKX 3 个 REST history endpoint 到 Bronze 表。
    # 对应 configs/rdp_workflows/okx_rest_history_rolling_1h.json —
    # 白名单同步契约由 test_valid_workflows_covers_all_json_configs 守护。
    "okx_rest_history_rolling_1h",
}

ENQUEUE_BLOCKED_WORKFLOWS = frozenset({"release_cycle"})


class WorkflowEnqueueBlockedError(ValueError):
    """Raised when a valid workflow is frozen from creating new queue tasks."""


def _validate_workflow_can_enqueue(workflow: str) -> None:
    if workflow not in VALID_WORKFLOWS:
        raise ValueError(f"Invalid workflow: {workflow!r}, expected one of {VALID_WORKFLOWS}")
    if workflow in ENQUEUE_BLOCKED_WORKFLOWS:
        raise WorkflowEnqueueBlockedError(
            f"Workflow {workflow!r} is blocked from task queue enqueue during golden-path freeze"
        )

# orphan-recovery 的 sentinel exit_code：daemon 崩溃 / 被 kill 后留下的
# running 任务在 startup 阶段被统一改写成 failed，用这个特殊值让运维和
# 下游看板能把 "任务自己退出非零" 与 "daemon 死了导致的补偿回收" 区分开。
# 值落库到 governance.rdp_task_queue.exit_code，不要随意改动，以免和
# 已有告警规则/仪表盘失配。
_ORPHAN_RECOVERY_EXIT_CODE = -3


# ── INSERT 新任务 ──────────────────────────────────────────────────

def db_create_task(
    session: Session,
    *,
    workflow: str,
    requested_by: str = "operator",
) -> str:
    """创建一条 pending 任务，返回 task_id.

    调用前应校验 workflow 合法性。如果同类 workflow 已有 pending/running
    任务，调用方应拒绝创建（防止重复提交）。

    并发安全性：governance.rdp_task_queue 上有
    ``ix_rdp_task_one_active_per_workflow`` 这条 partial unique index
    （workflow 列，status IN ('pending','running')）兜底，若并发 INSERT
    撞到已有 active 任务会抛 ``IntegrityError``。希望把 race 归并为优雅的
    "已有活跃任务" 响应的 caller，应改用 :func:`db_create_task_if_idle`。
    """
    _validate_workflow_can_enqueue(workflow)
    task_id = f"task_{uuid4().hex[:12]}"
    run_id = new_run_id()
    now = datetime.now(timezone.utc)
    trigger_kind = trigger_kind_for_request(requested_by)
    db_create_run(
        session,
        workflow=workflow,
        requested_by=requested_by,
        eligible_at=now,
        trigger_kind=trigger_kind,
        run_id=run_id,
    )
    session.execute(
        text("""
            INSERT INTO governance.rdp_task_queue
                (task_id, run_id, attempt_no, workflow, status, requested_by,
                 requested_at, earliest_start_at, trigger_kind, priority_class,
                 created_at)
            VALUES
                (:task_id, :run_id, 1, :workflow, 'pending', :requested_by,
                 :now, :now, :trigger_kind, :priority_class, :now)
        """),
        {
            "task_id": task_id,
            "run_id": run_id,
            "workflow": workflow,
            "requested_by": requested_by,
            "trigger_kind": trigger_kind,
            "priority_class": priority_class_for_trigger(trigger_kind),
            "now": now,
        },
    )
    db_append_run_event(
        session,
        run_id=run_id,
        event_type="run.queued",
        attempt_no=1,
        payload={"task_id": task_id, "eligible_at": now.isoformat()},
        occurred_at=now,
    )
    log.info("DB created task: %s workflow=%s by=%s", task_id, workflow, requested_by)
    return task_id


def db_create_task_if_idle(
    session: Session,
    *,
    workflow: str,
    requested_by: str = "operator",
    earliest_start_at: datetime | None = None,
    run_id: str | None = None,
    attempt_no: int = 1,
    parent_task_id: str | None = None,
    trigger_kind: str | None = None,
    priority_class: str | None = None,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """原子创建任务：同 workflow 已有 pending/running 时不插入。

    Returns
    -------
    (task_id, None)
        新任务创建成功（状态 pending）.
    (None, existing_active_task_dict)
        已有同 workflow 的活跃任务，未创建；existing dict 的结构与
        :func:`db_has_active_task` 一致（``task_id`` / ``status`` /
        ``requested_at`` / ``started_at``）.

    背景：旧路径是 ``db_has_active_task`` → ``db_create_task`` 两条 SQL，
    API handler 与 scheduler 在高并发下可能都通过了 has_active_task 再
    双双 INSERT。第二次 INSERT 虽然被 ``ix_rdp_task_one_active_per_workflow``
    partial unique index 兜底拦下（IntegrityError），但 caller 的通用 except
    会把它抹平成 "创建任务失败" 的误导消息，用户看到的是 500 一样的结果。

    本函数把判断+插入收敛到一条 ``INSERT ... ON CONFLICT DO NOTHING
    RETURNING`` SQL，用 partial unique index 的冲突语义直接吸收 race：
      * 首先抢到索引的 writer → INSERT 成功 → RETURNING 返回 task_id.
      * 后续并发 writer → ON CONFLICT DO NOTHING → RETURNING 空 → 读现有行
        并返回给 caller 一个 "已有活跃任务" 的结构化响应，无异常路径。
    """
    _validate_workflow_can_enqueue(workflow)

    if attempt_no < 1:
        raise ValueError("attempt_no must be >= 1")
    task_id = f"task_{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    # R3 Bug 6 retry: 未显式指定 earliest_start_at 时 = now() (立即可领)；
    # auto_retry 路径传 now()+15min 实现延迟入队。
    eligible_at = earliest_start_at if earliest_start_at is not None else now
    normalized_trigger = trigger_kind_for_request(requested_by, trigger_kind)
    effective_priority = priority_class or priority_class_for_trigger(normalized_trigger)
    logical_run_id = run_id
    created_run = False
    if logical_run_id is None:
        run, created_run = db_create_run(
            session,
            workflow=workflow,
            requested_by=requested_by,
            eligible_at=eligible_at,
            trigger_kind=normalized_trigger,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        logical_run_id = str(run["run_id"])
        if not created_run:
            existing_attempt = db_get_latest_task_for_run(session, logical_run_id)
            if existing_attempt is not None:
                existing_attempt["idempotent_replay"] = True
                return None, existing_attempt

    result = session.execute(
        text(
            """
            INSERT INTO governance.rdp_task_queue
                (task_id, run_id, attempt_no, parent_task_id, workflow, status,
                 requested_by, requested_at, earliest_start_at, trigger_kind,
                 priority_class, created_at)
            VALUES
                (:task_id, :run_id, :attempt_no, :parent_task_id, :workflow,
                 'pending', :requested_by, :now, :eligible_at, :trigger_kind,
                 :priority_class, :now)
            ON CONFLICT (workflow) WHERE status IN ('pending', 'running')
            DO NOTHING
            RETURNING task_id
            """
        ),
        {
            "task_id": task_id,
            "run_id": logical_run_id,
            "attempt_no": attempt_no,
            "parent_task_id": parent_task_id,
            "workflow": workflow,
            "requested_by": requested_by,
            "now": now,
            "eligible_at": eligible_at,
            "trigger_kind": normalized_trigger,
            "priority_class": effective_priority,
        },
    )
    row = result.fetchone()
    if row is not None:
        if attempt_no > 1 and parent_task_id:
            db_mark_run_requeued(
                session,
                run_id=logical_run_id,
                attempt_no=attempt_no,
                eligible_at=eligible_at,
                parent_task_id=parent_task_id,
            )
        else:
            db_append_run_event(
                session,
                run_id=logical_run_id,
                event_type="run.queued",
                attempt_no=attempt_no,
                payload={
                    "task_id": row.task_id,
                    "eligible_at": eligible_at.isoformat(),
                    "trigger_kind": normalized_trigger,
                },
                occurred_at=now,
            )
        log.info(
            "DB create_task_if_idle: created %s run=%s attempt=%s workflow=%s by=%s",
            row.task_id, logical_run_id, attempt_no, workflow, requested_by,
        )
        return row.task_id, None

    if created_run:
        db_delete_unstarted_run(session, logical_run_id)
    existing = db_has_active_task(session, workflow)
    log.info(
        "DB create_task_if_idle: skip (existing active task %s for %s)",
        (existing or {}).get("task_id") or "?", workflow,
    )
    return None, existing


# ── 领取下一条待执行任务（daemon 专用）──────────────────────────────

def db_claim_next_task(session: Session) -> dict[str, Any] | None:
    """领取最早的 pending 任务并标记为 running.

    使用 FOR UPDATE SKIP LOCKED 避免多个 daemon 竞争同一任务。
    返回 task dict 或 None（无任务可领）。

    R3 Bug 6 retry: 加 ``earliest_start_at <= now()`` 过滤，让 auto_retry 产生
    的延迟任务在 15min 窗口内不会被立即 claim。scheduler 入队的 task 默认
    earliest_start_at = now()，立即可领，不受影响。
    """
    row = session.execute(
        text("""
            SELECT task_id, run_id, attempt_no, parent_task_id, workflow,
                   requested_by, requested_at, earliest_start_at, trigger_kind,
                   priority_class, cancel_requested_at
            FROM governance.rdp_task_queue
            WHERE status = 'pending'
              AND earliest_start_at <= now()
            ORDER BY
                CASE priority_class
                    WHEN 'operator_recovery' THEN 0
                    WHEN 'operator' THEN 1
                    WHEN 'retry' THEN 2
                    WHEN 'scheduled' THEN 3
                    ELSE 4
                END ASC,
                created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        """)
    ).fetchone()

    if row is None:
        return None

    now = datetime.now(timezone.utc)
    session.execute(
        text("""
            UPDATE governance.rdp_task_queue
            SET status = 'running', started_at = :now, heartbeat_at = :now
            WHERE task_id = :task_id
        """),
        {"task_id": row.task_id, "now": now},
    )

    db_mark_run_running(
        session,
        run_id=row.run_id,
        attempt_no=int(row.attempt_no),
        started_at=now,
    )

    log.info("DB claimed task: %s workflow=%s", row.task_id, row.workflow)
    return {
        "task_id": row.task_id,
        "run_id": row.run_id,
        "attempt_no": int(row.attempt_no),
        "parent_task_id": row.parent_task_id,
        "workflow": row.workflow,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "earliest_start_at": row.earliest_start_at.isoformat() if row.earliest_start_at else None,
        "trigger_kind": row.trigger_kind,
        "priority_class": row.priority_class,
        "cancel_requested_at": (
            row.cancel_requested_at.isoformat() if row.cancel_requested_at else None
        ),
    }


# ── 更新任务状态（daemon 完成/失败时调用）──────────────────────────

_TERMINAL_STATUSES = {"done", "failed", "cancelled"}


def db_update_task_status(
    session: Session,
    task_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    error_message: str | None = None,
    log_tail: str | None = None,
    run_status: str | None = None,
    research_outcome: str | None = None,
) -> bool:
    """更新任务状态（done / failed / cancelled）."""
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"Invalid terminal status: {status!r}, expected one of {_TERMINAL_STATUSES}")
    now = datetime.now(timezone.utc)
    task_row = session.execute(
        text(
            """
            SELECT run_id, attempt_no, status
            FROM governance.rdp_task_queue
            WHERE task_id = :task_id
            FOR UPDATE
            """
        ),
        {"task_id": task_id},
    ).fetchone()
    if task_row is None:
        log.warning("DB task terminal update ignored: task not found: %s", task_id)
        return False
    if str(task_row.status) != "running":
        log.warning(
            "DB task terminal update ignored: task=%s current=%s requested=%s",
            task_id,
            task_row.status,
            status,
        )
        return False
    session.execute(
        text("""
            UPDATE governance.rdp_task_queue
            SET status = :status,
                finished_at = :now,
                exit_code = :exit_code,
                error_message = :error_message,
                log_tail = :log_tail
            WHERE task_id = :task_id AND status = 'running'
        """),
        {
            "task_id": task_id,
            "status": status,
            "exit_code": exit_code,
            "error_message": error_message,
            "log_tail": log_tail,
            "now": now,
        },
    )
    if run_status is None:
        run_status = {
            "done": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
        }[status]
    allowed_run_statuses = {
        "done": {"succeeded", "succeeded_with_warnings"},
        "failed": {"failed", "partially_succeeded"},
        "cancelled": {"cancelled"},
    }[status]
    if run_status not in allowed_run_statuses:
        raise ValueError(
            f"run_status {run_status!r} is inconsistent with task status {status!r}",
        )
    error_code = None
    if status == "failed":
        error_code = (
            "worker_orphan_recovered"
            if exit_code == _ORPHAN_RECOVERY_EXIT_CODE
            else "workflow_failed"
        )
    elif status == "cancelled":
        error_code = "operator_cancelled"
    db_mark_run_terminal(
        session,
        run_id=task_row.run_id,
        attempt_no=int(task_row.attempt_no),
        status=run_status,
        finished_at=now,
        error_code=error_code,
        error_summary=error_message,
        research_outcome=research_outcome,
    )
    log.info("DB updated task: %s -> %s (exit=%s)", task_id, status, exit_code)
    return True


def db_recover_orphaned_running_tasks(
    session: Session,
    *,
    error_message: str = "rdp_daemon_restarted_before_task_finished",
    exit_code: int = _ORPHAN_RECOVERY_EXIT_CODE,
    stale_before: datetime | None = None,
) -> list[dict[str, Any]]:
    """原子回收失去心跳的 running 任务；新鲜任务视为仍被其它 daemon 持有。"""
    now = datetime.now(timezone.utc)
    rows = session.execute(
        text(
            """
            WITH stale_tasks AS (
                SELECT task_id
                FROM governance.rdp_task_queue
                WHERE status = 'running'
                  AND (
                    CAST(:stale_before AS TIMESTAMPTZ) IS NULL
                    OR COALESCE(heartbeat_at, started_at, requested_at) <= :stale_before
                  )
                ORDER BY started_at ASC NULLS LAST, requested_at ASC
                FOR UPDATE SKIP LOCKED
            )
            UPDATE governance.rdp_task_queue AS task
            SET status = 'failed',
                finished_at = :now,
                exit_code = :exit_code,
                error_message = :error_message
            FROM stale_tasks
            WHERE task.task_id = stale_tasks.task_id
            RETURNING task.task_id, task.run_id, task.attempt_no, task.workflow,
                      task.requested_at, task.started_at
            """
        ),
        {
            "now": now,
            "exit_code": exit_code,
            "error_message": error_message,
            "stale_before": stale_before,
        },
    ).fetchall()

    if not rows:
        return []

    for row in rows:
        db_mark_run_terminal(
            session,
            run_id=row.run_id,
            attempt_no=int(row.attempt_no),
            status="failed",
            finished_at=now,
            error_code="worker_orphan_recovered",
            error_summary=error_message,
        )

    recovered = [
        {
            "task_id": row.task_id,
            "run_id": row.run_id,
            "attempt_no": int(row.attempt_no),
            "workflow": row.workflow,
            "requested_at": row.requested_at.isoformat() if row.requested_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
        }
        for row in rows
    ]
    log.warning("Recovered %d orphaned running tasks", len(recovered))
    return recovered


# ── 查询最近任务（gateway 面板用）──────────────────────────────────

def db_get_recent_tasks(
    session: Session,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """查询最近 N 条任务，按 created_at DESC 排序."""
    rows = session.execute(
        text("""
            SELECT task_id, run_id, attempt_no, parent_task_id, workflow, status,
                   requested_by, requested_at,
                   earliest_start_at, trigger_kind, priority_class,
                   started_at, finished_at, heartbeat_at, cancel_requested_at,
                   exit_code, error_message, log_tail
            FROM governance.rdp_task_queue
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"limit": limit},
    ).fetchall()

    return [
        {
            "task_id": r.task_id,
            "run_id": r.run_id,
            "attempt_no": int(r.attempt_no),
            "parent_task_id": r.parent_task_id,
            "workflow": r.workflow,
            "status": r.status,
            "requested_by": r.requested_by,
            "requested_at": r.requested_at.isoformat() if r.requested_at else None,
            "earliest_start_at": r.earliest_start_at.isoformat() if r.earliest_start_at else None,
            "trigger_kind": r.trigger_kind,
            "priority_class": r.priority_class,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "heartbeat_at": r.heartbeat_at.isoformat() if r.heartbeat_at else None,
            "cancel_requested_at": (
                r.cancel_requested_at.isoformat() if r.cancel_requested_at else None
            ),
            "exit_code": r.exit_code,
            "error_message": r.error_message,
            "log_tail": r.log_tail,
        }
        for r in rows
    ]


def db_get_latest_task_for_workflow(
    session: Session,
    workflow: str,
    *,
    statuses: tuple[str, ...] | None = None,
    requested_after: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the newest task for a workflow, optionally filtered by status/time."""
    if workflow not in VALID_WORKFLOWS:
        raise ValueError(f"Invalid workflow: {workflow!r}, expected one of {VALID_WORKFLOWS}")

    clauses = ["workflow = :workflow"]
    params: dict[str, Any] = {"workflow": workflow}

    if statuses:
        clauses.append("status = ANY(:statuses)")
        params["statuses"] = list(statuses)
    if requested_after is not None:
        clauses.append("requested_at >= :requested_after")
        params["requested_after"] = requested_after

    row = session.execute(
        text(
            f"""
            SELECT task_id, run_id, attempt_no, parent_task_id, workflow, status,
                   requested_by, requested_at,
                   earliest_start_at, trigger_kind, priority_class,
                   started_at, finished_at, heartbeat_at, cancel_requested_at,
                   exit_code, error_message, log_tail
            FROM governance.rdp_task_queue
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        params,
    ).fetchone()

    if row is None:
        return None

    return {
        "task_id": row.task_id,
        "run_id": row.run_id,
        "attempt_no": int(row.attempt_no),
        "parent_task_id": row.parent_task_id,
        "workflow": row.workflow,
        "status": row.status,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "earliest_start_at": row.earliest_start_at.isoformat() if row.earliest_start_at else None,
        "trigger_kind": row.trigger_kind,
        "priority_class": row.priority_class,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        "cancel_requested_at": (
            row.cancel_requested_at.isoformat() if row.cancel_requested_at else None
        ),
        "exit_code": row.exit_code,
        "error_message": row.error_message,
        "log_tail": row.log_tail,
    }


def db_get_latest_task_for_run(
    session: Session,
    run_id: str,
) -> dict[str, Any] | None:
    """Return the newest queue attempt for one logical RDP run."""
    row = session.execute(
        text(
            """
            SELECT task_id, run_id, attempt_no, parent_task_id, workflow, status,
                   requested_by, requested_at,
                   earliest_start_at, trigger_kind, priority_class,
                   started_at, finished_at, heartbeat_at, cancel_requested_at,
                   exit_code, error_message, log_tail
            FROM governance.rdp_task_queue
            WHERE run_id = :run_id
            ORDER BY attempt_no DESC, created_at DESC
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).fetchone()

    if row is None:
        return None
    return {
        "task_id": row.task_id,
        "run_id": row.run_id,
        "attempt_no": int(row.attempt_no),
        "parent_task_id": row.parent_task_id,
        "workflow": row.workflow,
        "status": row.status,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "earliest_start_at": row.earliest_start_at.isoformat() if row.earliest_start_at else None,
        "trigger_kind": row.trigger_kind,
        "priority_class": row.priority_class,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        "cancel_requested_at": (
            row.cancel_requested_at.isoformat() if row.cancel_requested_at else None
        ),
        "exit_code": row.exit_code,
        "error_message": row.error_message,
        "log_tail": row.log_tail,
    }


# ── 查询是否有同类活跃任务（防重复提交）──────────────────────────

def db_has_active_task(
    session: Session,
    workflow: str,
) -> dict[str, Any] | None:
    """查询指定 workflow 是否有 pending/running 任务.

    Returns task dict if exists, None otherwise.
    """
    row = session.execute(
        text("""
            SELECT task_id, run_id, attempt_no, parent_task_id, status,
                   requested_by, requested_at, earliest_start_at, started_at,
                   trigger_kind, priority_class, cancel_requested_at
            FROM governance.rdp_task_queue
            WHERE workflow = :workflow
              AND status IN ('pending', 'running')
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"workflow": workflow},
    ).fetchone()

    if row is None:
        return None
    return {
        "task_id": row.task_id,
        "run_id": row.run_id,
        "attempt_no": int(row.attempt_no),
        "parent_task_id": row.parent_task_id,
        "status": row.status,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "earliest_start_at": row.earliest_start_at.isoformat() if row.earliest_start_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "trigger_kind": row.trigger_kind,
        "priority_class": row.priority_class,
        "cancel_requested_at": (
            row.cancel_requested_at.isoformat() if row.cancel_requested_at else None
        ),
    }


def db_get_task_queue_summary(session: Session) -> dict[str, Any]:
    """聚合任务队列状态，供健康检查与 Operator 使用.

    ``failed_count`` 是审计口径的历史终态总数，不能直接解释为当前积压。
    ``latest_failed_count`` 只统计“各 workflow 最新一条任务仍失败”的流程，
    用于判断是否存在尚未被后续成功任务修复的执行故障。
    """
    counts_row = session.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
                COUNT(*) FILTER (WHERE status = 'running') AS running_count,
                COUNT(*) FILTER (WHERE status = 'done') AS done_count,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed_count,
                COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled_count,
                (
                    SELECT COUNT(*)
                    FROM (
                        SELECT DISTINCT ON (workflow) workflow, status
                        FROM governance.rdp_task_queue
                        ORDER BY workflow, created_at DESC, task_id DESC
                    ) AS latest_by_workflow
                    WHERE latest_by_workflow.status = 'failed'
                ) AS latest_failed_count,
                MAX(requested_at) FILTER (WHERE status = 'pending') AS latest_pending_at,
                MAX(started_at) FILTER (WHERE status = 'running') AS latest_running_at,
                MAX(finished_at) FILTER (
                    WHERE status IN ('done', 'failed', 'cancelled')
                ) AS latest_finished_at
            FROM governance.rdp_task_queue
        """),
    ).fetchone()

    recent_rows = session.execute(
        text("""
            SELECT task_id, run_id, attempt_no, workflow, status, trigger_kind,
                   priority_class, requested_at, earliest_start_at, started_at,
                   finished_at, heartbeat_at, exit_code
            FROM governance.rdp_task_queue
            ORDER BY created_at DESC
            LIMIT 5
        """),
    ).fetchall()

    return {
        "pending_count": int(counts_row.pending_count or 0),
        "running_count": int(counts_row.running_count or 0),
        "done_count": int(counts_row.done_count or 0),
        "failed_count": int(counts_row.failed_count or 0),
        "latest_failed_count": int(counts_row.latest_failed_count or 0),
        "cancelled_count": int(counts_row.cancelled_count or 0),
        "latest_pending_at": (
            counts_row.latest_pending_at.isoformat()
            if counts_row.latest_pending_at else None
        ),
        "latest_running_at": (
            counts_row.latest_running_at.isoformat()
            if counts_row.latest_running_at else None
        ),
        "latest_finished_at": (
            counts_row.latest_finished_at.isoformat()
            if counts_row.latest_finished_at else None
        ),
        "recent_tasks": [
            {
                "task_id": row.task_id,
                "run_id": row.run_id,
                "attempt_no": int(row.attempt_no),
                "workflow": row.workflow,
                "status": row.status,
                "trigger_kind": row.trigger_kind,
                "priority_class": row.priority_class,
                "requested_at": row.requested_at.isoformat() if row.requested_at else None,
                "earliest_start_at": (
                    row.earliest_start_at.isoformat() if row.earliest_start_at else None
                ),
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
                "exit_code": row.exit_code,
            }
            for row in recent_rows
        ],
    }
