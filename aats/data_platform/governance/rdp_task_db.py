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

VALID_WORKFLOWS = {"data_maintenance", "governance_cycle", "research_cycle", "decision_cycle"}


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
