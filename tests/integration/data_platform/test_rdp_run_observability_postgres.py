"""RDP logical Run/Attempt lifecycle on an isolated PostgreSQL container."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import psycopg2  # type: ignore[import-not-found]  # noqa: F401

    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG2_AVAILABLE = False

from sqlalchemy import text
from sqlalchemy.orm import Session


_SHOULD_RUN = (
    os.getenv("AATS_RUN_POSTGRES_INTEGRATION") == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)


@unittest.skipUnless(
    _SHOULD_RUN,
    "need isolated docker + testcontainers + AATS_RUN_POSTGRES_INTEGRATION=1",
)
class RdpRunObservabilityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert PostgresContainer is not None
        from aats.data_platform.config import ResearchPlatformSettings
        from aats.data_platform.db import apply_rdp_migrations, get_engine

        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()
        cls.settings = ResearchPlatformSettings(
            database_url=cls.container.get_connection_url(driver="psycopg2"),
            _env_file=None,
        )
        report = apply_rdp_migrations(cls.settings)
        if not report.ok:
            raise AssertionError(report.error_message)
        cls.engine = get_engine(cls.settings)

    @classmethod
    def tearDownClass(cls) -> None:
        from aats.data_platform.db import reset_engine

        reset_engine()
        cls.container.stop()

    def test_create_idempotent_claim_retry_steps_and_cancel(self) -> None:
        from aats.data_platform.governance.rdp_runs_db import (
            db_get_run,
            db_get_run_attempts,
            db_get_run_events,
            db_get_run_steps,
            db_request_run_cancel,
            db_sync_run_step_progress,
            db_upsert_run_step,
        )
        from aats.data_platform.governance.rdp_task_db import (
            db_claim_next_task,
            db_create_task_if_idle,
            db_update_task_status,
        )

        with Session(self.engine) as session, session.begin():
            task_id, existing = db_create_task_if_idle(
                session,
                workflow="research_cycle",
                requested_by="alice",
                idempotency_key="integration-run-123",
                trigger_kind="manual",
            )
            self.assertIsNotNone(task_id)
            self.assertIsNone(existing)

        with Session(self.engine) as session, session.begin():
            replay_task_id, replay = db_create_task_if_idle(
                session,
                workflow="research_cycle",
                requested_by="alice",
                idempotency_key="integration-run-123",
                trigger_kind="manual",
            )
            self.assertIsNone(replay_task_id)
            self.assertTrue((replay or {}).get("idempotent_replay"))
            run_id = str((replay or {})["run_id"])

        with Session(self.engine) as session, session.begin():
            claimed = db_claim_next_task(session)
            self.assertEqual(claimed["task_id"], task_id)
            self.assertEqual(claimed["run_id"], run_id)
            self.assertEqual(claimed["attempt_no"], 1)
            db_upsert_run_step(
                session,
                run_id=run_id,
                attempt_no=1,
                step_key="full_pipeline",
                step_order=1,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            db_upsert_run_step(
                session,
                run_id=run_id,
                attempt_no=1,
                step_key="full_pipeline",
                step_order=1,
                status="failed",
                finished_at=datetime.now(timezone.utc),
                exit_code=1,
                error_code="step_failed",
            )
            db_sync_run_step_progress(
                session,
                run_id=run_id,
                attempt_no=1,
                current_step_key=None,
            )
            db_update_task_status(
                session,
                str(task_id),
                status="failed",
                exit_code=1,
                error_message="controlled integration failure",
            )

        with Session(self.engine) as session, session.begin():
            run = db_get_run(session, run_id)
            self.assertEqual(run["status"], "failed")
            retry_task_id, existing = db_create_task_if_idle(
                session,
                workflow="research_cycle",
                requested_by="alice",
                run_id=run_id,
                attempt_no=2,
                parent_task_id=str(task_id),
                trigger_kind="manual",
            )
            self.assertIsNotNone(retry_task_id)
            self.assertIsNone(existing)

        with Session(self.engine) as session, session.begin():
            claimed_retry = db_claim_next_task(session)
            self.assertEqual(claimed_retry["task_id"], retry_task_id)
            self.assertEqual(claimed_retry["run_id"], run_id)
            self.assertEqual(claimed_retry["attempt_no"], 2)
            db_update_task_status(
                session,
                str(retry_task_id),
                status="done",
                exit_code=0,
            )

        with Session(self.engine) as session, session.begin():
            run = db_get_run(session, run_id)
            attempts = db_get_run_attempts(session, run_id)
            steps = db_get_run_steps(session, run_id)
            events = db_get_run_events(session, run_id)
            self.assertEqual(run["status"], "succeeded")
            self.assertEqual([item["attempt_no"] for item in attempts], [1, 2])
            self.assertEqual(steps[0]["status"], "failed")
            self.assertEqual(
                [item["sequence_no"] for item in events],
                list(range(1, len(events) + 1)),
            )

            queued_task_id, _ = db_create_task_if_idle(
                session,
                workflow="governance_cycle",
                requested_by="alice",
                idempotency_key="integration-cancel-123",
                trigger_kind="manual",
            )
            self.assertIsNotNone(queued_task_id)

        with Session(self.engine) as session, session.begin():
            active = db_get_run_attempts(
                session,
                str(
                    session.execute(
                        text(
                            "SELECT run_id FROM governance.rdp_task_queue WHERE task_id=:task_id"
                        ),
                        {"task_id": queued_task_id},
                    ).scalar_one()
                ),
            )[0]
            cancelled_run_id = str(active["run_id"])
            cancelled = db_request_run_cancel(
                session,
                run_id=cancelled_run_id,
                requested_by="alice",
            )
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(db_get_run(session, cancelled_run_id)["status"], "cancelled")
