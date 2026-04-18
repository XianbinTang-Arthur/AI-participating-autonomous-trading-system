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
}

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
    if workflow not in VALID_WORKFLOWS:
        raise ValueError(f"Invalid workflow: {workflow!r}, expected one of {VALID_WORKFLOWS}")
    task_id = f"task_{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    session.execute(
        text("""
            INSERT INTO governance.rdp_task_queue
                (task_id, workflow, status, requested_by, requested_at, created_at)
            VALUES
                (:task_id, :workflow, 'pending', :requested_by, :now, :now)
        """),
        {
            "task_id": task_id,
            "workflow": workflow,
            "requested_by": requested_by,
            "now": now,
        },
    )
    log.info("DB created task: %s workflow=%s by=%s", task_id, workflow, requested_by)
    return task_id


def db_create_task_if_idle(
    session: Session,
    *,
    workflow: str,
    requested_by: str = "operator",
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
    if workflow not in VALID_WORKFLOWS:
        raise ValueError(f"Invalid workflow: {workflow!r}, expected one of {VALID_WORKFLOWS}")

    task_id = f"task_{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    result = session.execute(
        text(
            """
            INSERT INTO governance.rdp_task_queue
                (task_id, workflow, status, requested_by, requested_at, created_at)
            VALUES
                (:task_id, :workflow, 'pending', :requested_by, :now, :now)
            ON CONFLICT (workflow) WHERE status IN ('pending', 'running')
            DO NOTHING
            RETURNING task_id
            """
        ),
        {
            "task_id": task_id,
            "workflow": workflow,
            "requested_by": requested_by,
            "now": now,
        },
    )
    row = result.fetchone()
    if row is not None:
        log.info(
            "DB create_task_if_idle: created %s workflow=%s by=%s",
            row.task_id, workflow, requested_by,
        )
        return row.task_id, None

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
    """
    row = session.execute(
        text("""
            SELECT task_id, workflow, requested_by, requested_at
            FROM governance.rdp_task_queue
            WHERE status = 'pending'
            ORDER BY created_at ASC
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
            SET status = 'running', started_at = :now
            WHERE task_id = :task_id
        """),
        {"task_id": row.task_id, "now": now},
    )

    log.info("DB claimed task: %s workflow=%s", row.task_id, row.workflow)
    return {
        "task_id": row.task_id,
        "workflow": row.workflow,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
    }


# ── 更新任务状态（daemon 完成/失败时调用）──────────────────────────

_TERMINAL_STATUSES = {"done", "failed"}


def db_update_task_status(
    session: Session,
    task_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    error_message: str | None = None,
    log_tail: str | None = None,
) -> None:
    """更新任务状态（done / failed）."""
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"Invalid terminal status: {status!r}, expected one of {_TERMINAL_STATUSES}")
    now = datetime.now(timezone.utc)
    session.execute(
        text("""
            UPDATE governance.rdp_task_queue
            SET status = :status,
                finished_at = :now,
                exit_code = :exit_code,
                error_message = :error_message,
                log_tail = :log_tail
            WHERE task_id = :task_id
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
    log.info("DB updated task: %s -> %s (exit=%s)", task_id, status, exit_code)


def db_recover_orphaned_running_tasks(
    session: Session,
    *,
    error_message: str = "rdp_daemon_restarted_before_task_finished",
    exit_code: int = _ORPHAN_RECOVERY_EXIT_CODE,
) -> list[dict[str, Any]]:
    """将 daemon 异常退出后遗留的 running 任务统一回收成 failed。"""
    rows = session.execute(
        text(
            """
            SELECT task_id, workflow, requested_at, started_at
            FROM governance.rdp_task_queue
            WHERE status = 'running'
            ORDER BY started_at ASC NULLS LAST, requested_at ASC
            """
        ),
    ).fetchall()

    if not rows:
        return []

    now = datetime.now(timezone.utc)
    session.execute(
        text(
            """
            UPDATE governance.rdp_task_queue
            SET status = 'failed',
                finished_at = :now,
                exit_code = :exit_code,
                error_message = :error_message
            WHERE status = 'running'
            """
        ),
        {
            "now": now,
            "exit_code": exit_code,
            "error_message": error_message,
        },
    )

    recovered = [
        {
            "task_id": row.task_id,
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
            SELECT task_id, workflow, status,
                   requested_by, requested_at,
                   started_at, finished_at,
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
            "workflow": r.workflow,
            "status": r.status,
            "requested_by": r.requested_by,
            "requested_at": r.requested_at.isoformat() if r.requested_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
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
            SELECT task_id, workflow, status,
                   requested_by, requested_at,
                   started_at, finished_at,
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
        "workflow": row.workflow,
        "status": row.status,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
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
            SELECT task_id, status, requested_at, started_at
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
        "status": row.status,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
    }


def db_get_task_queue_summary(session: Session) -> dict[str, Any]:
    """聚合任务队列状态，供健康检查与 Operator 使用."""
    counts_row = session.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
                COUNT(*) FILTER (WHERE status = 'running') AS running_count,
                COUNT(*) FILTER (WHERE status = 'done') AS done_count,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed_count,
                MAX(requested_at) FILTER (WHERE status = 'pending') AS latest_pending_at,
                MAX(started_at) FILTER (WHERE status = 'running') AS latest_running_at,
                MAX(finished_at) FILTER (WHERE status IN ('done', 'failed')) AS latest_finished_at
            FROM governance.rdp_task_queue
        """),
    ).fetchone()

    recent_rows = session.execute(
        text("""
            SELECT task_id, workflow, status, requested_at, started_at, finished_at, exit_code
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
                "workflow": row.workflow,
                "status": row.status,
                "requested_at": row.requested_at.isoformat() if row.requested_at else None,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "exit_code": row.exit_code,
            }
            for row in recent_rows
        ],
    }
