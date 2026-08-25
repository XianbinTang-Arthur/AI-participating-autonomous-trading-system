"""Database truth for RDP logical runs, steps, and lifecycle events.

The existing ``rdp_task_queue`` remains the daemon attempt queue.  This module
adds the stable logical ``run_id`` that groups retries and exposes structured
state to Operator APIs without parsing log text.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session


RUN_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "succeeded_with_warnings",
        "partially_succeeded",
        "failed",
        "cancelled",
    }
)
RUN_ACTIVE_STATUSES = frozenset({"queued", "running", "cancellation_requested"})
RUN_STATUSES = RUN_TERMINAL_STATUSES | RUN_ACTIVE_STATUSES
STEP_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "skipped", "cancelled"}
)
TRIGGER_KINDS = frozenset({"manual", "schedule", "auto_retry", "recovery"})


def new_run_id() -> str:
    return f"run_{uuid4().hex[:16]}"


def trigger_kind_for_request(
    requested_by: str,
    explicit: str | None = None,
) -> str:
    if explicit is not None:
        normalized = str(explicit).strip().lower()
        if normalized not in TRIGGER_KINDS:
            raise ValueError(f"invalid RDP trigger kind: {explicit!r}")
        return normalized
    actor = str(requested_by or "").strip()
    if actor.startswith("auto_retry_of_"):
        return "auto_retry"
    if actor == "scheduler":
        return "schedule"
    if actor.startswith("recovery"):
        return "recovery"
    return "manual"


def priority_class_for_trigger(trigger_kind: str) -> str:
    return {
        "recovery": "operator_recovery",
        "manual": "operator",
        "auto_retry": "retry",
        "schedule": "scheduled",
    }[trigger_kind]


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def _run_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    mapping = row._mapping if hasattr(row, "_mapping") else row
    result = dict(mapping)
    for key in (
        "eligible_at",
        "started_at",
        "finished_at",
        "heartbeat_at",
        "cancel_requested_at",
        "created_at",
        "updated_at",
    ):
        result[key] = _iso(result.get(key))
    return result


def _step_dict(row: Any) -> dict[str, Any]:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    result = dict(mapping)
    for key in ("started_at", "finished_at", "created_at", "updated_at"):
        result[key] = _iso(result.get(key))
    return result


def db_create_run(
    session: Session,
    *,
    workflow: str,
    requested_by: str,
    eligible_at: datetime,
    trigger_kind: str,
    idempotency_key: str | None = None,
    run_id: str | None = None,
    source_run_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a logical run, or return the idempotent existing run."""
    normalized_trigger = trigger_kind_for_request(requested_by, trigger_kind)
    normalized_key = str(idempotency_key or "").strip() or None
    logical_run_id = run_id or new_run_id()
    now = datetime.now(timezone.utc)
    row = session.execute(
        text(
            """
            INSERT INTO governance.rdp_runs
                (run_id, workflow, status, research_outcome, trigger_kind,
                 requested_by, idempotency_key, source_run_id, eligible_at,
                 payload, created_at, updated_at)
            VALUES
                (:run_id, :workflow, 'queued', 'unknown', :trigger_kind,
                 :requested_by, :idempotency_key, :source_run_id, :eligible_at,
                 CAST(:payload AS JSONB), :now, :now)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING run_id, workflow, status, research_outcome, trigger_kind,
                      requested_by, idempotency_key, source_run_id, eligible_at,
                      started_at, finished_at, heartbeat_at, current_step_key,
                      completed_steps, total_steps, cancel_requested_at,
                      error_code, error_summary, payload, created_at, updated_at
            """
        ),
        {
            "run_id": logical_run_id,
            "workflow": workflow,
            "trigger_kind": normalized_trigger,
            "requested_by": requested_by,
            "idempotency_key": normalized_key,
            "source_run_id": source_run_id,
            "eligible_at": eligible_at,
            "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
            "now": now,
        },
    ).fetchone()
    if row is not None:
        created = _run_dict(row)
        assert created is not None
        return created, True
    if normalized_key is None:
        raise RuntimeError("rdp_run_insert_returned_no_row_without_idempotency_conflict")
    existing = db_get_run_by_idempotency_key(session, normalized_key)
    if existing is None:
        raise RuntimeError("rdp_run_idempotency_conflict_without_existing_row")
    return existing, False


def db_delete_unstarted_run(session: Session, run_id: str) -> None:
    session.execute(
        text(
            """
            DELETE FROM governance.rdp_runs
            WHERE run_id = :run_id
              AND status = 'queued'
              AND NOT EXISTS (
                  SELECT 1 FROM governance.rdp_task_queue q WHERE q.run_id = :run_id
              )
            """
        ),
        {"run_id": run_id},
    )


def db_get_run(session: Session, run_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT run_id, workflow, status, research_outcome, trigger_kind,
                   requested_by, idempotency_key, source_run_id, eligible_at,
                   started_at, finished_at, heartbeat_at, current_step_key,
                   completed_steps, total_steps, cancel_requested_at,
                   error_code, error_summary, payload, created_at, updated_at
            FROM governance.rdp_runs
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id},
    ).fetchone()
    return _run_dict(row)


def db_get_run_by_idempotency_key(
    session: Session,
    idempotency_key: str,
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT run_id, workflow, status, research_outcome, trigger_kind,
                   requested_by, idempotency_key, source_run_id, eligible_at,
                   started_at, finished_at, heartbeat_at, current_step_key,
                   completed_steps, total_steps, cancel_requested_at,
                   error_code, error_summary, payload, created_at, updated_at
            FROM governance.rdp_runs
            WHERE idempotency_key = :idempotency_key
            """
        ),
        {"idempotency_key": idempotency_key},
    ).fetchone()
    return _run_dict(row)


def db_list_runs(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    workflow: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        clauses.append("status = :status")
        params["status"] = status
    if workflow:
        clauses.append("workflow = :workflow")
        params["workflow"] = workflow
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = session.execute(
        text(
            f"""
            SELECT run_id, workflow, status, research_outcome, trigger_kind,
                   requested_by, idempotency_key, source_run_id, eligible_at,
                   started_at, finished_at, heartbeat_at, current_step_key,
                   completed_steps, total_steps, cancel_requested_at,
                   error_code, error_summary, payload, created_at, updated_at
            FROM governance.rdp_runs
            {where}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).fetchall()
    return [item for row in rows if (item := _run_dict(row)) is not None]


def db_get_run_attempts(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT task_id, run_id, attempt_no, parent_task_id, workflow, status,
                   trigger_kind, priority_class, requested_by, requested_at,
                   earliest_start_at, started_at, finished_at, heartbeat_at,
                   cancel_requested_at, exit_code, error_message, log_tail
            FROM governance.rdp_task_queue
            WHERE run_id = :run_id
            ORDER BY attempt_no ASC, created_at ASC
            """
        ),
        {"run_id": run_id},
    ).fetchall()
    attempts: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row._mapping if hasattr(row, "_mapping") else row)
        for key in (
            "requested_at",
            "earliest_start_at",
            "started_at",
            "finished_at",
            "heartbeat_at",
            "cancel_requested_at",
        ):
            item[key] = _iso(item.get(key))
        attempts.append(item)
    return attempts


def db_get_run_steps(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT step_run_id, run_id, attempt_no, step_key, step_order, status,
                   allow_failure, started_at, finished_at, exit_code, error_code,
                   error_summary, log_ref, artifact_refs, payload, created_at, updated_at
            FROM governance.rdp_run_steps
            WHERE run_id = :run_id
            ORDER BY attempt_no ASC, step_order ASC
            """
        ),
        {"run_id": run_id},
    ).fetchall()
    return [_step_dict(row) for row in rows]


def db_get_run_events(
    session: Session,
    run_id: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT sequence_no, run_id, attempt_no, step_key, event_type,
                   payload, occurred_at
            FROM governance.rdp_run_events
            WHERE run_id = :run_id
            ORDER BY sequence_no ASC
            LIMIT :limit
            """
        ),
        {"run_id": run_id, "limit": limit},
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row._mapping if hasattr(row, "_mapping") else row)
        item["occurred_at"] = _iso(item.get("occurred_at"))
        events.append(item)
    return events


def db_append_run_event(
    session: Session,
    *,
    run_id: str,
    event_type: str,
    attempt_no: int | None = None,
    step_key: str | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> int:
    # Lock the aggregate row so concurrent writers allocate monotonically
    # increasing sequence numbers without a process-local counter.
    session.execute(
        text("SELECT run_id FROM governance.rdp_runs WHERE run_id = :run_id FOR UPDATE"),
        {"run_id": run_id},
    ).fetchone()
    sequence_no = session.execute(
        text(
            """
            SELECT COALESCE(MAX(sequence_no), 0) + 1
            FROM governance.rdp_run_events
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id},
    ).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO governance.rdp_run_events
                (run_id, sequence_no, attempt_no, step_key, event_type, payload, occurred_at)
            VALUES
                (:run_id, :sequence_no, :attempt_no, :step_key, :event_type,
                 CAST(:payload AS JSONB), :occurred_at)
            """
        ),
        {
            "run_id": run_id,
            "sequence_no": sequence_no,
            "attempt_no": attempt_no,
            "step_key": step_key,
            "event_type": event_type,
            "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
            "occurred_at": occurred_at or datetime.now(timezone.utc),
        },
    )
    return int(sequence_no)


def db_mark_run_running(
    session: Session,
    *,
    run_id: str,
    attempt_no: int,
    started_at: datetime,
) -> None:
    session.execute(
        text(
            """
            UPDATE governance.rdp_runs
            SET status = 'running',
                started_at = COALESCE(started_at, :started_at),
                heartbeat_at = :started_at,
                error_code = NULL,
                error_summary = NULL,
                updated_at = :started_at
            WHERE run_id = :run_id
              AND status IN ('queued', 'failed', 'partially_succeeded')
            """
        ),
        {"run_id": run_id, "started_at": started_at},
    )
    db_append_run_event(
        session,
        run_id=run_id,
        event_type="run.started",
        attempt_no=attempt_no,
        occurred_at=started_at,
    )


def db_touch_run_heartbeat(
    session: Session,
    *,
    run_id: str,
    task_id: str,
    heartbeat_at: datetime,
) -> None:
    session.execute(
        text(
            """
            UPDATE governance.rdp_task_queue
            SET heartbeat_at = :heartbeat_at
            WHERE task_id = :task_id AND status = 'running'
            """
        ),
        {"task_id": task_id, "heartbeat_at": heartbeat_at},
    )
    session.execute(
        text(
            """
            UPDATE governance.rdp_runs
            SET heartbeat_at = :heartbeat_at, updated_at = :heartbeat_at
            WHERE run_id = :run_id AND status IN ('running', 'cancellation_requested')
            """
        ),
        {"run_id": run_id, "heartbeat_at": heartbeat_at},
    )


def db_is_run_cancel_requested(session: Session, run_id: str) -> bool:
    status = session.execute(
        text(
            """
            SELECT status
            FROM governance.rdp_runs
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id},
    ).scalar_one_or_none()
    return status == "cancellation_requested"


def db_mark_run_terminal(
    session: Session,
    *,
    run_id: str,
    attempt_no: int,
    status: str,
    finished_at: datetime,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> None:
    if status not in RUN_TERMINAL_STATUSES:
        raise ValueError(f"invalid terminal RDP run status: {status!r}")
    session.execute(
        text(
            """
            UPDATE governance.rdp_runs
            SET status = :status,
                finished_at = :finished_at,
                heartbeat_at = :finished_at,
                current_step_key = NULL,
                error_code = :error_code,
                error_summary = :error_summary,
                updated_at = :finished_at
            WHERE run_id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "status": status,
            "finished_at": finished_at,
            "error_code": error_code,
            "error_summary": error_summary,
        },
    )
    db_append_run_event(
        session,
        run_id=run_id,
        event_type="run.completed",
        attempt_no=attempt_no,
        payload={"status": status, "error_code": error_code},
        occurred_at=finished_at,
    )


def db_mark_run_requeued(
    session: Session,
    *,
    run_id: str,
    attempt_no: int,
    eligible_at: datetime,
    parent_task_id: str,
) -> None:
    now = datetime.now(timezone.utc)
    session.execute(
        text(
            """
            UPDATE governance.rdp_runs
            SET status = 'queued',
                eligible_at = :eligible_at,
                finished_at = NULL,
                current_step_key = NULL,
                completed_steps = 0,
                total_steps = 0,
                error_code = NULL,
                error_summary = NULL,
                updated_at = :now
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id, "eligible_at": eligible_at, "now": now},
    )
    db_append_run_event(
        session,
        run_id=run_id,
        event_type="retry.scheduled",
        attempt_no=attempt_no,
        payload={
            "eligible_at": eligible_at.isoformat(),
            "parent_task_id": parent_task_id,
        },
        occurred_at=now,
    )


def db_request_run_cancel(
    session: Session,
    *,
    run_id: str,
    requested_by: str,
) -> dict[str, Any] | None:
    """Request cooperative cancellation, or cancel an unclaimed run immediately.

    Queue rows are locked before the aggregate run so this path follows the
    same lock order as daemon claim (task -> run) and avoids a lock inversion.
    """
    now = datetime.now(timezone.utc)
    cancelled_tasks = session.execute(
        text(
            """
            UPDATE governance.rdp_task_queue
            SET status = 'cancelled',
                cancel_requested_at = COALESCE(cancel_requested_at, :now),
                finished_at = :now,
                exit_code = NULL,
                error_message = 'cancelled_before_worker_claim'
            WHERE run_id = :run_id AND status = 'pending'
            RETURNING task_id
            """
        ),
        {"run_id": run_id, "now": now},
    ).fetchall()
    if cancelled_tasks:
        row = session.execute(
            text(
                """
                UPDATE governance.rdp_runs
                SET status = 'cancelled',
                    cancel_requested_at = COALESCE(cancel_requested_at, :now),
                    finished_at = :now,
                    heartbeat_at = :now,
                    current_step_key = NULL,
                    error_code = NULL,
                    error_summary = NULL,
                    updated_at = :now
                WHERE run_id = :run_id AND status IN ('queued', 'cancellation_requested')
                RETURNING run_id, workflow, status, research_outcome, trigger_kind,
                          requested_by, idempotency_key, source_run_id, eligible_at,
                          started_at, finished_at, heartbeat_at, current_step_key,
                          completed_steps, total_steps, cancel_requested_at,
                          error_code, error_summary, payload, created_at, updated_at
                """
            ),
            {"run_id": run_id, "now": now},
        ).fetchone()
        result = _run_dict(row)
        if result is not None:
            db_append_run_event(
                session,
                run_id=run_id,
                event_type="run.cancelled",
                payload={
                    "requested_by": requested_by,
                    "task_ids": [item.task_id for item in cancelled_tasks],
                    "before_worker_claim": True,
                },
                occurred_at=now,
            )
            return result

    row = session.execute(
        text(
            """
            UPDATE governance.rdp_runs
            SET status = 'cancellation_requested',
                cancel_requested_at = COALESCE(cancel_requested_at, :now),
                updated_at = :now
            WHERE run_id = :run_id
              AND status IN ('queued', 'running', 'cancellation_requested')
            RETURNING run_id, workflow, status, research_outcome, trigger_kind,
                      requested_by, idempotency_key, source_run_id, eligible_at,
                      started_at, finished_at, heartbeat_at, current_step_key,
                      completed_steps, total_steps, cancel_requested_at,
                      error_code, error_summary, payload, created_at, updated_at
            """
        ),
        {"run_id": run_id, "now": now},
    ).fetchone()
    result = _run_dict(row)
    if result is None:
        return db_get_run(session, run_id)
    session.execute(
        text(
            """
            UPDATE governance.rdp_task_queue
            SET cancel_requested_at = COALESCE(cancel_requested_at, :now)
            WHERE run_id = :run_id AND status IN ('pending', 'running')
            """
        ),
        {"run_id": run_id, "now": now},
    )
    db_append_run_event(
        session,
        run_id=run_id,
        event_type="run.cancel_requested",
        payload={"requested_by": requested_by},
        occurred_at=now,
    )
    return result


def db_upsert_run_step(
    session: Session,
    *,
    run_id: str,
    attempt_no: int,
    step_key: str,
    step_order: int,
    status: str,
    allow_failure: bool = False,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    exit_code: int | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in STEP_STATUSES:
        raise ValueError(f"invalid RDP run step status: {status!r}")
    now = datetime.now(timezone.utc)
    step_run_id = f"{run_id}:{attempt_no}:{step_key}"
    row = session.execute(
        text(
            """
            INSERT INTO governance.rdp_run_steps
                (step_run_id, run_id, attempt_no, step_key, step_order, status,
                 allow_failure, started_at, finished_at, exit_code, error_code,
                 error_summary, payload, created_at, updated_at)
            VALUES
                (:step_run_id, :run_id, :attempt_no, :step_key, :step_order, :status,
                 :allow_failure, :started_at, :finished_at, :exit_code, :error_code,
                 :error_summary, CAST(:payload AS JSONB), :now, :now)
            ON CONFLICT (run_id, attempt_no, step_key) DO UPDATE SET
                status = EXCLUDED.status,
                allow_failure = EXCLUDED.allow_failure,
                started_at = COALESCE(governance.rdp_run_steps.started_at, EXCLUDED.started_at),
                finished_at = EXCLUDED.finished_at,
                exit_code = EXCLUDED.exit_code,
                error_code = EXCLUDED.error_code,
                error_summary = EXCLUDED.error_summary,
                payload = governance.rdp_run_steps.payload || EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            RETURNING step_run_id, run_id, attempt_no, step_key, step_order, status,
                      allow_failure, started_at, finished_at, exit_code, error_code,
                      error_summary, log_ref, artifact_refs, payload, created_at, updated_at
            """
        ),
        {
            "step_run_id": step_run_id,
            "run_id": run_id,
            "attempt_no": attempt_no,
            "step_key": step_key,
            "step_order": step_order,
            "status": status,
            "allow_failure": allow_failure,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "error_code": error_code,
            "error_summary": error_summary,
            "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
            "now": now,
        },
    ).fetchone()
    if row is None:
        raise RuntimeError("rdp_run_step_upsert_returned_no_row")
    return _step_dict(row)


def db_sync_run_step_progress(
    session: Session,
    *,
    run_id: str,
    attempt_no: int,
    current_step_key: str | None,
) -> None:
    counts = session.execute(
        text(
            """
            SELECT COUNT(*) AS total_steps,
                   COUNT(*) FILTER (WHERE status IN ('succeeded', 'failed', 'skipped', 'cancelled'))
                       AS completed_steps
            FROM governance.rdp_run_steps
            WHERE run_id = :run_id AND attempt_no = :attempt_no
            """
        ),
        {"run_id": run_id, "attempt_no": attempt_no},
    ).fetchone()
    total_steps = int(getattr(counts, "total_steps", 0) or 0)
    completed_steps = int(getattr(counts, "completed_steps", 0) or 0)
    now = datetime.now(timezone.utc)
    session.execute(
        text(
            """
            UPDATE governance.rdp_runs
            SET current_step_key = :current_step_key,
                completed_steps = :completed_steps,
                total_steps = :total_steps,
                updated_at = :now
            WHERE run_id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "current_step_key": current_step_key,
            "completed_steps": completed_steps,
            "total_steps": total_steps,
            "now": now,
        },
    )
