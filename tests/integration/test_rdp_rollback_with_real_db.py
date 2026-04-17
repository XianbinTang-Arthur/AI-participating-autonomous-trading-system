"""A-0.1 · real Postgres verification of rollback-target validation.

Scope (docs/task/rdp_hardening_batch_a_detailed_design.md §2.5):

- ``validate_rollback_target`` against a real schema — the 6 rules fire the
  right rejections when rows exist (or don't) as expected.
- ``rollback_active_parameter_set`` end-to-end: DB-backed target lookup,
  single-transaction write + ``FOR UPDATE`` locking.

Run conditions (same as other real-DB integration tests):

    docker daemon running
    pip install -e .[postgres-integration]   # testcontainers + psycopg2
    AATS_RUN_POSTGRES_INTEGRATION=1

WSL2 entry:
    AATS_RUN_POSTGRES_INTEGRATION=1 pytest \\
        tests/integration/test_rdp_rollback_with_real_db.py -x -q
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[postgres-integration] to run",
)
class TestRollbackWithRealDb(unittest.TestCase):
    """Exercise validate_rollback_target + rollback_active_parameter_set."""

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
        create_rdp_schema(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine") and cls.engine is not None:
            cls.engine.dispose()  # type: ignore[attr-defined]
        if hasattr(cls, "container") and cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
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

    # ── Fixture helpers ────────────────────────────────────────────────

    def _seed_parameter_set(
        self,
        ps_id: str,
        *,
        family: str = "independent",
        timeframe: str = "15m",
        status: str = "frozen",
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
                      (:pid, :family, 'BTC-USDT-SWAP', :tf,
                       :round, '{"entry_threshold": 0.4}'::jsonb,
                       :status, now())
                    """
                ),
                {
                    "pid": ps_id,
                    "family": family,
                    "tf": timeframe,
                    "round": source_round_id,
                    "status": status,
                },
            )

    def _seed_recommendation(
        self,
        rec_id: str,
        *,
        target_ps_id: str,
        family: str = "independent",
        timeframe: str = "15m",
        status: str = "approved",
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
                      (:rec, :family, :tf, 'parameter_upgrade',
                       :target, NULL, 'high', 'seed', :status, now())
                    """
                ),
                {
                    "rec": rec_id,
                    "family": family,
                    "tf": timeframe,
                    "target": target_ps_id,
                    "status": status,
                },
            )

    def _seed_apply_history(
        self,
        op_id: str,
        *,
        from_ps: str | None,
        to_ps: str,
        family: str = "independent",
        timeframe: str = "15m",
        operation_type: str = "apply",
    ) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.parameter_apply_history
                      (operation_id, operation_type, family, timeframe,
                       from_parameter_set_id, to_parameter_set_id,
                       recommendation_id, actor, notes, created_at)
                    VALUES
                      (:op, :op_type, :family, :tf,
                       :from_ps, :to_ps, NULL, 'operator', 'seed', now())
                    """
                ),
                {
                    "op": op_id,
                    "op_type": operation_type,
                    "family": family,
                    "tf": timeframe,
                    "from_ps": from_ps,
                    "to_ps": to_ps,
                },
            )

    def _seed_active(
        self,
        ps_id: str,
        *,
        family: str = "independent",
        timeframe: str = "15m",
    ) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.active_parameter_sets
                      (family, timeframe, parameter_set_id, values,
                       applied_by, applied_at, updated_at)
                    VALUES
                      (:family, :tf, :pid, '{"entry_threshold": 0.4}'::jsonb,
                       'seed', now(), now())
                    ON CONFLICT (family, timeframe) DO UPDATE SET
                      parameter_set_id = EXCLUDED.parameter_set_id,
                      values = EXCLUDED.values,
                      applied_at = EXCLUDED.applied_at,
                      updated_at = EXCLUDED.updated_at
                    """
                ),
                {"family": family, "tf": timeframe, "pid": ps_id},
            )

    def _seed_chain_v0_v1_v2(self) -> None:
        """v0 → v1 → v2 with approved recs + apply history; v2 is live."""
        self._seed_parameter_set("ps_v0")
        self._seed_parameter_set("ps_v1")
        self._seed_parameter_set("ps_v2")

        self._seed_recommendation("rec_v1", target_ps_id="ps_v1")
        self._seed_recommendation("rec_v2", target_ps_id="ps_v2")

        self._seed_apply_history("op_1", from_ps="ps_v0", to_ps="ps_v1")
        self._seed_apply_history("op_2", from_ps="ps_v1", to_ps="ps_v2")

        self._seed_active("ps_v2")

    # ── validate_rollback_target (6 rules) ─────────────────────────────

    def _run_validate(self, target: str, **kwargs: object) -> tuple[bool, str]:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.active_params_db import (
            validate_rollback_target,
        )

        family = str(kwargs.get("family", "independent"))
        timeframe = str(kwargs.get("timeframe", "15m"))

        with Session(self.engine) as session:  # type: ignore[arg-type]
            ok, reason = validate_rollback_target(
                session, family, timeframe, target
            )
        return ok, reason

    def test_validate_rejects_nonexistent_target(self) -> None:
        self._seed_chain_v0_v1_v2()
        ok, reason = self._run_validate("ps_does_not_exist")
        self.assertFalse(ok)
        self.assertEqual(reason, "target_not_found_or_wrong_combo")

    def test_validate_rejects_wrong_family(self) -> None:
        self._seed_chain_v0_v1_v2()
        # ps_v1 exists but under independent/15m; asking under overlay/15m rejects
        ok, reason = self._run_validate("ps_v1", family="overlay")
        self.assertFalse(ok)
        self.assertEqual(reason, "target_not_found_or_wrong_combo")

    def test_validate_rejects_wrong_timeframe(self) -> None:
        self._seed_chain_v0_v1_v2()
        ok, reason = self._run_validate("ps_v1", timeframe="1h")
        self.assertFalse(ok)
        self.assertEqual(reason, "target_not_found_or_wrong_combo")

    def test_validate_rejects_draft_status(self) -> None:
        self._seed_parameter_set("ps_draft", status="draft")
        ok, reason = self._run_validate("ps_draft")
        self.assertFalse(ok)
        self.assertEqual(reason, "target_status_illegal:draft")

    def test_validate_rejects_deprecated_status(self) -> None:
        self._seed_parameter_set("ps_dep", status="deprecated")
        ok, reason = self._run_validate("ps_dep")
        self.assertFalse(ok)
        self.assertEqual(reason, "target_status_illegal:deprecated")

    def test_validate_rejects_target_without_apply_history(self) -> None:
        self._seed_parameter_set("ps_never_applied")
        self._seed_recommendation("rec_orphan", target_ps_id="ps_never_applied")
        # Active set so rule 5 doesn't mistakenly accept early.
        self._seed_parameter_set("ps_active_other")
        self._seed_apply_history("op_seed", from_ps=None, to_ps="ps_active_other")
        self._seed_active("ps_active_other")

        ok, reason = self._run_validate("ps_never_applied")
        self.assertFalse(ok)
        self.assertEqual(reason, "no_apply_history_for_target")

    def test_validate_rejects_self_rollback(self) -> None:
        self._seed_chain_v0_v1_v2()
        # ps_v2 is current active — rolling back to it is a self-rollback.
        ok, reason = self._run_validate("ps_v2")
        self.assertFalse(ok)
        self.assertEqual(reason, "target_is_currently_active")

    def test_validate_rejects_target_without_approved_recommendation(self) -> None:
        self._seed_parameter_set("ps_orphan")
        self._seed_apply_history("op_orphan", from_ps=None, to_ps="ps_orphan")
        # Active something else so rule 5 passes.
        self._seed_parameter_set("ps_active_other")
        self._seed_active("ps_active_other")
        # Only draft recommendation — rule 6 rejects since status not in allowlist.
        self._seed_recommendation(
            "rec_draft", target_ps_id="ps_orphan", status="draft"
        )

        ok, reason = self._run_validate("ps_orphan")
        self.assertFalse(ok)
        self.assertEqual(reason, "no_approved_recommendation_lineage")

    def test_validate_accepts_valid_frozen_target(self) -> None:
        """Happy path: v2 is live, rolling back to v1 satisfies all 6 rules."""
        self._seed_chain_v0_v1_v2()
        ok, reason = self._run_validate("ps_v1")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    # ── rollback_active_parameter_set end-to-end ───────────────────────

    def _rollback_helper(
        self,
        tmp_path: Path,
        *,
        to_parameter_set_id: str | None,
        family: str = "independent",
        timeframe: str = "15m",
    ) -> dict:
        """Call rollback_active_parameter_set with get_session patched to the
        test container's engine and the env guard forced to allow.
        """
        import contextlib
        from sqlalchemy.orm import Session

        from aats.data_platform.decision_system.active_parameter_apply import (
            rollback_active_parameter_set,
        )

        @contextlib.contextmanager
        def _make_session():
            # 镜像 aats.data_platform.db.get_session 的 commit/rollback/close 语义，
            # 否则生产代码里的 with get_session() 退出时不会把变更落库。
            session = Session(self.engine)  # type: ignore[arg-type]
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        guard_ok = SimpleNamespace(allowed=True, reason="")
        with (
            patch(
                "aats.data_platform.db.get_session", side_effect=_make_session
            ),
            patch(
                "aats.data_platform.operations.environment_guard.get_current_environment",
                return_value="dev",
            ),
            patch(
                "aats.data_platform.operations.environment_guard.guard_parameter_rollback",
                return_value=guard_ok,
            ),
        ):
            return rollback_active_parameter_set(
                tmp_path,
                family=family,
                timeframe=timeframe,
                to_parameter_set_id=to_parameter_set_id,
                actor="integration_test",
            )

    def test_rollback_succeeds_and_writes_to_db(self) -> None:
        from sqlalchemy import text

        self._seed_chain_v0_v1_v2()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self._rollback_helper(
                Path(tmp), to_parameter_set_id="ps_v1"
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["to_parameter_set_id"], "ps_v1")
        self.assertEqual(result["from_parameter_set_id"], "ps_v2")

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            row = conn.execute(text(
                "SELECT parameter_set_id FROM governance.active_parameter_sets "
                "WHERE family='independent' AND timeframe='15m'"
            )).fetchone()
            self.assertEqual(row.parameter_set_id, "ps_v1")

            hist = conn.execute(text(
                "SELECT operation_type, from_parameter_set_id, to_parameter_set_id "
                "FROM governance.parameter_apply_history "
                "ORDER BY created_at DESC LIMIT 1"
            )).fetchone()
            self.assertEqual(hist.operation_type, "rollback")
            self.assertEqual(hist.from_parameter_set_id, "ps_v2")
            self.assertEqual(hist.to_parameter_set_id, "ps_v1")

    def test_rollback_rejects_deprecated_with_validation_failed(self) -> None:
        self._seed_chain_v0_v1_v2()
        self._seed_parameter_set("ps_dep_target", status="deprecated")
        self._seed_apply_history(
            "op_dep", from_ps=None, to_ps="ps_dep_target"
        )
        self._seed_recommendation("rec_dep", target_ps_id="ps_dep_target")

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self._rollback_helper(
                Path(tmp), to_parameter_set_id="ps_dep_target"
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VALIDATION_FAILED")
        self.assertEqual(result["reason"], "target_status_illegal:deprecated")

    def test_rollback_auto_derives_previous_when_not_specified(self) -> None:
        self._seed_chain_v0_v1_v2()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self._rollback_helper(Path(tmp), to_parameter_set_id=None)

        # db_get_previous_set_id returns the most recent apply's from_parameter_set_id
        # which for chain v0→v1→v2 is ps_v1 (from op_2: from=v1, to=v2).
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["to_parameter_set_id"], "ps_v1")
