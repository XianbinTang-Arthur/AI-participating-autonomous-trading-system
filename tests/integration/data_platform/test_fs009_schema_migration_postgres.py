"""FS-009 PostgreSQL migration/ledger integration contract.

Runs only with an explicitly enabled isolated Testcontainers database.  It
never reads a repository ``.env.*`` file or connects to an operator database.
"""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

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


_SHOULD_RUN = (
    os.getenv("AATS_RUN_POSTGRES_INTEGRATION") == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)


@unittest.skipUnless(
    _SHOULD_RUN,
    "need isolated docker + testcontainers + AATS_RUN_POSTGRES_INTEGRATION=1",
)
class Fs009SchemaMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert PostgresContainer is not None
        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()

    @classmethod
    def tearDownClass(cls) -> None:
        from aats.data_platform.db import reset_engine

        reset_engine()
        cls.container.stop()

    def test_full_chain_is_idempotent_and_rollback_repair_is_ledgered(self) -> None:
        from aats.data_platform.config import ResearchPlatformSettings
        from aats.data_platform.db import (
            apply_rdp_migrations,
            get_engine,
            validate_rdp_schema,
        )
        from aats.data_platform.migrations._batch_b import (
            BATCH_B_STAGES,
            run_batch_b_rollback,
        )

        settings = ResearchPlatformSettings(
            database_url=self.container.get_connection_url(driver="psycopg2"),
            _env_file=None,
        )

        first = apply_rdp_migrations(settings)
        self.assertTrue(first.ok, first.error_message)
        self.assertEqual(
            [stage.stage for stage in first.stages],
            list(BATCH_B_STAGES),
        )
        self.assertTrue(all(stage.applied for stage in first.stages))

        second = apply_rdp_migrations(settings)
        self.assertTrue(second.ok, second.error_message)
        self.assertTrue(all(not stage.applied for stage in second.stages))
        validate_rdp_schema(settings)

        last_stage = BATCH_B_STAGES[-1]
        rollback = run_batch_b_rollback(
            get_engine(settings),
            stages=(last_stage,),
        )
        self.assertTrue(rollback.ok, rollback.error_message)
        with self.assertRaisesRegex(
            RuntimeError,
            r"rdp_schema_(?:orm|migration)_contract_failed",
        ):
            validate_rdp_schema(settings)

        repaired = apply_rdp_migrations(settings)
        self.assertTrue(repaired.ok, repaired.error_message)
        self.assertEqual(
            [stage.stage for stage in repaired.stages if stage.applied],
            [last_stage],
        )
        validate_rdp_schema(settings)

    def test_runtime_lineage_migration_and_schema_guard(self) -> None:
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        from aats.storage.session import (
            apply_current_migrations,
            create_database_runtime,
            create_schema,
            validate_runtime_schema,
        )

        base_url = make_url(self.container.get_connection_url(driver="psycopg2"))
        admin_engine = create_engine(
            base_url.render_as_string(hide_password=False),
            future=True,
        )
        schema_name = f"aats_rdp_lineage_{uuid.uuid4().hex[:12]}"
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

        query = dict(base_url.query)
        query["options"] = f"-csearch_path={schema_name}"
        runtime = create_database_runtime(
            base_url.set(query=query).render_as_string(hide_password=False)
        )
        try:
            create_schema(runtime)
            with runtime.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE strategy_sleeve_intents "
                        "DROP COLUMN signal_bar_start CASCADE, "
                        "DROP COLUMN feature_snapshot_ref"
                    )
                )

            applied = apply_current_migrations(runtime)

            self.assertIn(
                "006_strategy_sleeve_intent_attribution_lineage.sql",
                applied,
            )
            validate_runtime_schema(runtime)

            event_1 = datetime(2026, 8, 26, 12, 1, tzinfo=timezone.utc)
            event_2 = event_1 + timedelta(minutes=1)
            with runtime.engine.begin() as connection:
                for index, created_at in enumerate(
                    (event_1 - timedelta(seconds=1), event_2 - timedelta(seconds=1)),
                    start=1,
                ):
                    reconciliation_id = f"recon_{index}"
                    connection.execute(
                        text(
                            """
                            INSERT INTO reconciliation_reports (
                                reconciliation_id, as_of_ts, created_at, severity,
                                halt_required, primary_symbol, payload
                            ) VALUES (
                                :reconciliation_id, :created_at, :created_at, 'normal',
                                FALSE, 'BTC-USDT-SWAP', '{}'::jsonb
                            )
                            """
                        ),
                        {
                            "reconciliation_id": reconciliation_id,
                            "created_at": created_at,
                        },
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO reconciliation_state_snapshots (
                                snapshot_id, reconciliation_id, primary_symbol,
                                recovery_state, resume_eligible, safe_to_trade,
                                review_required, only_reduce_required, halt_required,
                                bundle_recovery_required, resume_blocked_reasons_json,
                                details, created_at
                            ) VALUES (
                                :snapshot_id, :reconciliation_id, 'BTC-USDT-SWAP',
                                'normal_operation', TRUE, TRUE,
                                FALSE, FALSE, FALSE, FALSE,
                                '[]'::jsonb, '{}'::jsonb, :created_at
                            )
                            """
                        ),
                        {
                            "snapshot_id": f"snapshot_{index}",
                            "reconciliation_id": reconciliation_id,
                            "created_at": created_at,
                        },
                    )

            from aats.data_platform.attribution.alignment import (
                query_reconciliation_snapshots,
            )

            with runtime.session_factory() as session:
                snapshots = query_reconciliation_snapshots(
                    session,
                    symbol="BTC-USDT-SWAP",
                    event_times=[event_1, event_2],
                )
            self.assertEqual(
                [snapshot["snapshot_id"] for snapshot in snapshots],
                ["snapshot_1", "snapshot_2"],
            )

            with runtime.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE strategy_sleeve_intents "
                        "DROP COLUMN feature_snapshot_ref"
                    )
                )
            with self.assertRaisesRegex(
                RuntimeError,
                r"missing=strategy_sleeve_intents\.feature_snapshot_ref",
            ):
                validate_runtime_schema(runtime)
        finally:
            runtime.dispose()
            with admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
            admin_engine.dispose()


if __name__ == "__main__":
    unittest.main()
