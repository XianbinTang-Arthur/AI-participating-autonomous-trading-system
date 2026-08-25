"""FS-009 PostgreSQL migration/ledger integration contract.

Runs only with an explicitly enabled isolated Testcontainers database.  It
never reads a repository ``.env.*`` file or connects to an operator database.
"""

from __future__ import annotations

import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
