"""Best-effort structured observability for workflow-dispatcher RDP runs."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RdpRunObserver:
    run_id: str
    attempt_no: int

    @classmethod
    def from_environment(cls) -> RdpRunObserver | None:
        run_id = str(os.environ.get("AATS_RDP_RUN_ID") or "").strip()
        attempt_raw = str(os.environ.get("AATS_RDP_ATTEMPT_NO") or "").strip()
        if not run_id or not attempt_raw:
            return None
        try:
            attempt_no = int(attempt_raw)
        except ValueError:
            log.warning("忽略无效 AATS_RDP_ATTEMPT_NO=%r", attempt_raw)
            return None
        if attempt_no < 1:
            log.warning("忽略无效 AATS_RDP_ATTEMPT_NO=%r", attempt_raw)
            return None
        return cls(run_id=run_id, attempt_no=attempt_no)

    def initialize(self, tasks: list[dict[str, Any]]) -> None:
        def _write(session: Any) -> None:
            from aats.data_platform.governance.rdp_runs_db import (
                db_sync_run_step_progress,
                db_upsert_run_step,
            )

            now = _utcnow()
            for index, task in enumerate(tasks, start=1):
                enabled = task.get("enabled", True) is not False
                db_upsert_run_step(
                    session,
                    run_id=self.run_id,
                    attempt_no=self.attempt_no,
                    step_key=str(task.get("name") or f"step_{index}"),
                    step_order=index,
                    status="pending" if enabled else "skipped",
                    allow_failure=bool(task.get("allow_failure", False)),
                    finished_at=None if enabled else now,
                    payload={"enabled": enabled},
                )
            db_sync_run_step_progress(
                session,
                run_id=self.run_id,
                attempt_no=self.attempt_no,
                current_step_key=None,
            )

        self._best_effort("initialize", _write)

    def step_started(self, task: dict[str, Any], step_order: int) -> None:
        step_key = str(task.get("name") or f"step_{step_order}")

        def _write(session: Any) -> None:
            from aats.data_platform.governance.rdp_runs_db import (
                db_append_run_event,
                db_sync_run_step_progress,
                db_upsert_run_step,
            )

            now = _utcnow()
            db_upsert_run_step(
                session,
                run_id=self.run_id,
                attempt_no=self.attempt_no,
                step_key=step_key,
                step_order=step_order,
                status="running",
                allow_failure=bool(task.get("allow_failure", False)),
                started_at=now,
            )
            db_sync_run_step_progress(
                session,
                run_id=self.run_id,
                attempt_no=self.attempt_no,
                current_step_key=step_key,
            )
            db_append_run_event(
                session,
                run_id=self.run_id,
                attempt_no=self.attempt_no,
                step_key=step_key,
                event_type="step.started",
                occurred_at=now,
            )

        self._best_effort(f"start:{step_key}", _write)

    def step_finished(self, result: dict[str, Any], step_order: int) -> None:
        step_key = str(result.get("name") or f"step_{step_order}")
        raw_status = str(result.get("status") or "error")
        status = {
            "success": "succeeded",
            "dry_run": "succeeded",
            "disabled": "skipped",
            "skipped": "skipped",
            "skipped_due_to_failure": "skipped",
            "failed": "failed",
            "timeout": "failed",
            "error": "failed",
        }.get(raw_status, "failed")
        error_summary = str(result.get("error") or "").strip()[:500] or None

        def _write(session: Any) -> None:
            from aats.data_platform.governance.rdp_runs_db import (
                db_append_run_event,
                db_sync_run_step_progress,
                db_upsert_run_step,
            )

            now = _utcnow()
            db_upsert_run_step(
                session,
                run_id=self.run_id,
                attempt_no=self.attempt_no,
                step_key=step_key,
                step_order=step_order,
                status=status,
                allow_failure=bool(result.get("allow_failure", False)),
                finished_at=now,
                exit_code=result.get("exit_code"),
                error_code=(f"step_{raw_status}" if status == "failed" else None),
                error_summary=error_summary,
                payload={"workflow_status": raw_status},
            )
            db_sync_run_step_progress(
                session,
                run_id=self.run_id,
                attempt_no=self.attempt_no,
                current_step_key=None,
            )
            db_append_run_event(
                session,
                run_id=self.run_id,
                attempt_no=self.attempt_no,
                step_key=step_key,
                event_type="step.completed",
                payload={"status": status, "exit_code": result.get("exit_code")},
                occurred_at=now,
            )

        self._best_effort(f"finish:{step_key}", _write)

    def _best_effort(self, operation: str, writer: Any) -> None:
        try:
            from aats.data_platform.db import get_session

            with get_session() as session:
                writer(session)
        except Exception:
            log.exception(
                "RDP run 可观测性写入失败，不改变 workflow 业务结果: run=%s attempt=%s op=%s",
                self.run_id,
                self.attempt_no,
                operation,
            )
