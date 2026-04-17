"""Batch A · real Postgres verification of FK / UQ / CHECK constraints.

Scope (docs/task/rdp_hardening_batch_a_detailed_design.md §4.12):

- FKs (stage 4.4.2) actually block dangling parameter_set_id references
- ON DELETE RESTRICT prevents deleting a parameter_set that's still referenced
- Partial UQ (stage 4.4.3) blocks duplicate recommendations in the same round
- Partial UQ does NOT block superseded/rejected retries (normal flow)
- CHECKs (stage 4.4.4) reject illegal allowlist values
- Full migration chain is idempotent (rollback → fks → uqs → checks → same again)

Run conditions (same as other *_db_postgres.py tests):
    docker daemon running
    pip install -e .[postgres-integration]  (testcontainers + psycopg2)
    AATS_RUN_POSTGRES_INTEGRATION=1

WSL2 entry:
    AATS_RUN_POSTGRES_INTEGRATION=1 pytest \\
        tests/integration/test_rdp_batch_a_db_constraints.py -x -q
"""

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


_INTEGRATION_ENV_FLAG = "AATS_RUN_POSTGRES_INTEGRATION"
_SHOULD_RUN = (
    os.getenv(_INTEGRATION_ENV_FLAG) == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[postgres-integration] to run",
)
class TestBatchADbConstraints(unittest.TestCase):
    """End-to-end enforcement checks for batch-A constraints on real Postgres."""

    container: "PostgresContainer"
    engine: object

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()

        from sqlalchemy import create_engine

        from aats.data_platform.rdp_models import create_rdp_schema

        url = cls.container.get_connection_url()
        cls.engine = create_engine(url, future=True)
        # create_rdp_schema lays down ORM-level constraints up front. The SQL
        # migration files are there for existing prod DBs — a fresh schema via
        # the ORM is already hardened.
        create_rdp_schema(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine") and cls.engine is not None:
            cls.engine.dispose()  # type: ignore[attr-defined]
        if hasattr(cls, "container") and cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        """Clear governance tables before each test so state doesn't leak."""
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            # Order matters — child tables first to avoid FK conflicts during
            # TRUNCATE. CASCADE keeps this resilient to future FK additions.
            conn.execute(text(
                "TRUNCATE TABLE "
                "governance.release_effectiveness, "
                "governance.rollback_recommendations, "
                "governance.observation_results, "
                "governance.parameter_releases, "
                "governance.active_decisions, "
                "governance.parameter_apply_history, "
                "governance.active_parameter_sets, "
                "governance.recommendations, "
                "governance.parameter_sets "
                "CASCADE"
            ))

    def _insert_parameter_set(
        self, ps_id: str, *, status: str = "frozen",
        source_round_id: str | None = None,
    ) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.parameter_sets
                      (parameter_set_id, family, symbol, timeframe,
                       source_round_id, values, status, created_at)
                    VALUES
                      (:ps_id, 'independent', 'BTC-USDT-SWAP', '15m',
                       :round_id, '{}'::jsonb, :status, now())
                    """,
                ),
                {"ps_id": ps_id, "round_id": source_round_id, "status": status},
            )

    def _insert_recommendation(
        self, rec_id: str, *, target_ps_id: str,
        source_round_id: str | None = None, status: str = "draft",
    ) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.recommendations
                      (recommendation_id, family, timeframe, recommendation_type,
                       target_parameter_set_id, source_round_id, confidence,
                       reason, status, created_at)
                    VALUES
                      (:rec_id, 'independent', '15m', 'parameter_upgrade',
                       :target, :round_id, 'high', 'test', :status, now())
                    """,
                ),
                {
                    "rec_id": rec_id,
                    "target": target_ps_id,
                    "round_id": source_round_id,
                    "status": status,
                },
            )

    # ── FK enforcement ──────────────────────────────────────────────

    def test_fk_blocks_active_with_nonexistent_ps(self) -> None:
        """fk_active_ps_id must reject a row that references a non-existent PS."""
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "INSERT INTO governance.active_parameter_sets "
                    "(family, timeframe, parameter_set_id, values) "
                    "VALUES ('independent', '15m', 'ps_ghost', '{}'::jsonb)"
                ))

    def test_fk_blocks_apply_history_with_nonexistent_ps(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "INSERT INTO governance.parameter_apply_history "
                    "(operation_id, operation_type, family, timeframe, to_parameter_set_id) "
                    "VALUES ('op_ghost', 'apply', 'independent', '15m', 'ps_ghost')"
                ))

    def test_fk_blocks_release_with_nonexistent_ps(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "INSERT INTO governance.parameter_releases "
                    "(release_id, family, timeframe, combo_key, recommendation_id, "
                    " parameter_set_id, apply_result, observation_status) "
                    "VALUES ('rel_ghost', 'independent', '15m', 'independent_15m', "
                    " 'rec_ghost', 'ps_ghost', 'pending', 'pending')"
                ))

    def test_fk_restrict_prevents_ps_delete_when_active(self) -> None:
        """Deleting a parameter_set that is still referenced from active_parameter_sets
        must fail with ON DELETE RESTRICT.
        """
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        self._insert_parameter_set("ps_live_restrict")
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(text(
                "INSERT INTO governance.active_parameter_sets "
                "(family, timeframe, parameter_set_id, values) "
                "VALUES ('independent', '15m', 'ps_live_restrict', '{}'::jsonb)"
            ))

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "DELETE FROM governance.parameter_sets "
                    "WHERE parameter_set_id = 'ps_live_restrict'"
                ))

    # ── Partial UQ on recommendations ───────────────────────────────

    def test_uq_blocks_duplicate_recommendation_in_same_round(self) -> None:
        """Two non-superseded recommendations for the same (round, family, timeframe)
        must collide on the partial unique index.
        """
        from sqlalchemy.exc import IntegrityError

        self._insert_parameter_set("ps_round_a", source_round_id="round_1")
        self._insert_recommendation(
            "rec_round_1_a", target_ps_id="ps_round_a", source_round_id="round_1",
        )
        with self.assertRaises(IntegrityError):
            self._insert_recommendation(
                "rec_round_1_b", target_ps_id="ps_round_a",
                source_round_id="round_1",
            )

    def test_uq_allows_superseded_recommendation_retry(self) -> None:
        """A superseded recommendation should not block a fresh draft in the same round
        — supersede is part of the normal retry flow.
        """
        self._insert_parameter_set("ps_round_b", source_round_id="round_2")
        self._insert_recommendation(
            "rec_round_2_old", target_ps_id="ps_round_b",
            source_round_id="round_2", status="superseded",
        )
        # Should NOT raise — partial index excludes superseded rows
        self._insert_recommendation(
            "rec_round_2_new", target_ps_id="ps_round_b",
            source_round_id="round_2", status="draft",
        )

    def test_uq_allows_null_source_round(self) -> None:
        """Legacy recommendations without source_round_id must not be constrained."""
        self._insert_parameter_set("ps_null_round")
        self._insert_recommendation(
            "rec_legacy_a", target_ps_id="ps_null_round", source_round_id=None,
        )
        # Second NULL-round recommendation for the same combo — also allowed
        self._insert_recommendation(
            "rec_legacy_b", target_ps_id="ps_null_round", source_round_id=None,
        )

    # ── CHECK constraints ───────────────────────────────────────────

    def test_check_rejects_illegal_ps_status(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "INSERT INTO governance.parameter_sets "
                    "(parameter_set_id, family, symbol, timeframe, values, status) "
                    "VALUES ('ps_bad_status', 'independent', 'BTC-USDT-SWAP', "
                    "'15m', '{}'::jsonb, 'not_a_real_status')"
                ))

    def test_check_rejects_illegal_observation_status(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        self._insert_parameter_set("ps_obs")
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "INSERT INTO governance.parameter_releases "
                    "(release_id, family, timeframe, combo_key, recommendation_id, "
                    " parameter_set_id, apply_result, observation_status) "
                    "VALUES ('rel_obs_bad', 'independent', '15m', 'independent_15m', "
                    " 'rec_obs_bad', 'ps_obs', 'pending', 'not_observing')"
                ))

    def test_check_rejects_illegal_apply_op_type(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "INSERT INTO governance.parameter_apply_history "
                    "(operation_id, operation_type, family, timeframe) "
                    "VALUES ('op_bad_type', 'nuke', 'independent', '15m')"
                ))

    def test_check_rejects_illegal_rollback_severity(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "INSERT INTO governance.rollback_recommendations "
                    "(release_id, family, timeframe, combo_key, "
                    " rollback_recommended, severity, evaluated_at) "
                    "VALUES ('rel_rollback_bad', 'independent', '15m', "
                    "'independent_15m', false, 'catastrophic', now())"
                ))

    # ── Migration idempotency ───────────────────────────────────────

    def test_migration_stages_are_idempotent(self) -> None:
        """All DDL stages use DO $$ EXCEPTION / IF NOT EXISTS. Running the
        full chain against a schema that already has batch-A applied must
        be a no-op, not a failure.
        """
        from aats.data_platform.migrations._batch_a import load_migration_sql

        # Reuse the class-level engine — schema already has batch-A constraints
        # from create_rdp_schema(). Re-applying must not raise.
        for stage in ("fks", "uqs", "checks"):
            sql = load_migration_sql(stage)
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.exec_driver_sql(sql)

        # Second pass — still idempotent
        for stage in ("fks", "uqs", "checks"):
            sql = load_migration_sql(stage)
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.exec_driver_sql(sql)

    def test_rollback_then_reapply_restores_constraints(self) -> None:
        """Emergency rollback (stage 99) should drop every batch-A constraint.
        Re-running fks/uqs/checks afterwards must restore enforcement end-to-end.
        """
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        from aats.data_platform.migrations._batch_a import load_migration_sql

        # Strip constraints
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.exec_driver_sql(load_migration_sql("rollback"))

        # After rollback, the FK guard is gone — insert should succeed
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(text(
                "INSERT INTO governance.active_parameter_sets "
                "(family, timeframe, parameter_set_id, values) "
                "VALUES ('independent', '1h', 'ps_post_rollback_ghost', '{}'::jsonb)"
            ))
            # clean up before re-adding FKs (or the ADD would fail on this orphan)
            conn.execute(text(
                "DELETE FROM governance.active_parameter_sets "
                "WHERE parameter_set_id = 'ps_post_rollback_ghost'"
            ))

        # Re-apply in order
        for stage in ("fks", "uqs", "checks"):
            sql = load_migration_sql(stage)
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.exec_driver_sql(sql)

        # FK should be back — same insert must now fail
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text(
                    "INSERT INTO governance.active_parameter_sets "
                    "(family, timeframe, parameter_set_id, values) "
                    "VALUES ('independent', '4h', 'ps_post_reapply_ghost', '{}'::jsonb)"
                ))


if __name__ == "__main__":
    unittest.main()
