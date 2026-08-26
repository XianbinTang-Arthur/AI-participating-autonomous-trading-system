from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from aats.data_platform.governance import rdp_runs_db, rdp_task_db


class _Result:
    def __init__(self, *, row: Any = None, rows: list[Any] | None = None, rowcount: int = 1):
        self._row = row
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(self, results: list[_Result]):
        self.results = list(results)
        self.statements: list[str] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        self.statements.append(str(statement))
        if not self.results:
            raise AssertionError("unexpected SQL execution")
        return self.results.pop(0)


def test_late_worker_cannot_overwrite_recovered_terminal_task() -> None:
    task = SimpleNamespace(
        run_id="run_1",
        attempt_no=1,
        status="failed",
    )
    session = _Session([_Result(row=task)])

    updated = rdp_task_db.db_update_task_status(
        session,
        "task_1",
        status="done",
        exit_code=0,
    )

    assert updated is False
    assert len(session.statements) == 1


def test_terminal_run_update_requires_active_current_state(monkeypatch) -> None:
    session = _Session([_Result(rowcount=0)])
    appended: list[dict[str, Any]] = []
    monkeypatch.setattr(
        rdp_runs_db,
        "db_append_run_event",
        lambda *args, **kwargs: appended.append(kwargs),
    )

    updated = rdp_runs_db.db_mark_run_terminal(
        session,
        run_id="run_1",
        attempt_no=1,
        status="succeeded",
        finished_at=datetime.now(timezone.utc),
    )

    assert updated is False
    assert appended == []
    assert "status IN ('running', 'cancellation_requested')" in session.statements[0]


def test_run_event_query_returns_latest_window_in_chronological_order() -> None:
    session = _Session([_Result(rows=[])])

    assert rdp_runs_db.db_get_run_events(session, "run_1", limit=20) == []
    sql = session.statements[0]
    assert "ORDER BY sequence_no DESC" in sql
    assert "AS recent_events" in sql
    assert sql.rstrip().endswith("ORDER BY sequence_no ASC")


def test_orphan_recovery_only_claims_stale_rows_atomically() -> None:
    session = _Session([_Result(rows=[])])
    stale_before = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    assert rdp_task_db.db_recover_orphaned_running_tasks(
        session,
        stale_before=stale_before,
    ) == []
    sql = session.statements[0]
    assert "COALESCE(heartbeat_at, started_at, requested_at) <= :stale_before" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "RETURNING task.task_id" in sql


def test_step_progress_update_cannot_reopen_terminal_run() -> None:
    counts = SimpleNamespace(total_steps=2, completed_steps=1)
    session = _Session([_Result(row=counts), _Result()])

    rdp_runs_db.db_sync_run_step_progress(
        session,
        run_id="run_1",
        attempt_no=1,
        current_step_key="late_step",
    )

    assert "status IN ('queued', 'running', 'cancellation_requested')" in session.statements[1]


def test_terminal_step_upsert_preserves_terminal_payload_and_metadata() -> None:
    returned_step = {
        "step_run_id": "step_1",
        "run_id": "run_1",
        "attempt_no": 1,
        "step_key": "phase4",
        "step_order": 4,
        "status": "failed",
        "allow_failure": False,
        "started_at": None,
        "finished_at": None,
        "exit_code": 1,
        "error_code": "step_failed",
        "error_summary": "original",
        "payload": {"failure_class": "deterministic_code_or_contract"},
        "created_at": None,
        "updated_at": None,
    }
    session = _Session([_Result(row=returned_step)])

    rdp_runs_db.db_upsert_run_step(
        session,
        run_id="run_1",
        attempt_no=1,
        step_key="phase4",
        step_order=4,
        status="running",
        payload={"failure_class": "transient_infrastructure"},
    )

    sql = session.statements[0]
    assert "THEN governance.rdp_run_steps.allow_failure" in sql
    assert "THEN governance.rdp_run_steps.payload" in sql
    assert "THEN governance.rdp_run_steps.updated_at" in sql
