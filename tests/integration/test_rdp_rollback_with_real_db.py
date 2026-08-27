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

import json
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
                "governance.release_effectiveness_action_proofs, "
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
        recommendation_id: str | None = None,
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
                       :from_ps, :to_ps, :recommendation_id,
                       'operator', 'seed', now())
                    """
                ),
                {
                    "op": op_id,
                    "op_type": operation_type,
                    "family": family,
                    "tf": timeframe,
                    "from_ps": from_ps,
                    "to_ps": to_ps,
                    "recommendation_id": recommendation_id,
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
        self._seed_parameter_set("ps_v2", status="released")

        self._seed_recommendation("rec_v1", target_ps_id="ps_v1")
        self._seed_recommendation("rec_v2", target_ps_id="ps_v2")

        self._seed_apply_history(
            "op_1",
            from_ps="ps_v0",
            to_ps="ps_v1",
            recommendation_id="rec_v1",
        )
        self._seed_apply_history(
            "op_2",
            from_ps="ps_v1",
            to_ps="ps_v2",
            recommendation_id="rec_v2",
        )

        self._seed_active("ps_v2")

    def test_pending_rollback_query_fails_closed_on_non_boolean_flags(self) -> None:
        """Postgres JSONB truth checks must not coerce string booleans."""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.active_params_db import (
            db_get_pending_rollback_release_id,
        )

        cases = [
            (
                "rel_string_active",
                {"rollback_enforced": "true"},
                "rel_string_active",
            ),
            (
                "rel_string_opposite",
                {
                    "rollback_enforced": True,
                    "rollback_cancelled": "true",
                    "rollback_enforcement_status": "enforced",
                },
                "rel_string_opposite",
            ),
            (
                "rel_exact_terminal",
                {
                    "rollback_enforced": True,
                    "rollback_cancelled": False,
                    "rollback_enforcement_status": "enforced",
                },
                "rel_exact_terminal",
            ),
            (
                "rel_unpersisted_soft_pause",
                {
                    "rollback_cancelled": True,
                    "rollback_soft_pause_applied": False,
                    "rollback_cancelled_reason": (
                        "soft_paused_no_valid_rollback_target: no target"
                    ),
                },
                "rel_unpersisted_soft_pause",
            ),
            (
                "rel_persisted_soft_pause",
                {
                    "rollback_cancelled": True,
                    "rollback_soft_pause_applied": True,
                    "rollback_cancelled_reason": (
                        "soft_paused_no_valid_rollback_target: no target"
                    ),
                },
                "rel_persisted_soft_pause",
            ),
            (
                "rel_invalid_calendar_terminal",
                {
                    "rollback_enforced": True,
                    "rollback_cancelled": False,
                    "rollback_soft_pause_applied": False,
                    "rollback_enforcement_status": "enforced",
                    "rollback_enforcement_attempt_id": "attempt_invalid_date",
                    "rollback_enforcement_started_at": (
                        "2026-02-30T10:00:00+00:00"
                    ),
                    "rollback_enforcement_finished_at": (
                        "2026-02-30T10:05:00+00:00"
                    ),
                    "rollback_enforced_at": "2026-02-30T10:05:00+00:00",
                    "rollback_to_parameter_set_id": "ps_v1",
                    "rollback_capital_proof_version": (
                        "rdp-rollback-capital-proof/v1"
                    ),
                    "rollback_capital_proof_kind": "rollback",
                    "rollback_capital_operation_id": "op_forged",
                    "rollback_capital_proof_verified": True,
                },
                "rel_invalid_calendar_terminal",
            ),
            (
                "rel_future_terminal",
                {
                    "rollback_enforced": True,
                    "rollback_cancelled": False,
                    "rollback_soft_pause_applied": False,
                    "rollback_enforcement_status": "enforced",
                    "rollback_enforcement_attempt_id": "attempt_future",
                    "rollback_enforcement_started_at": (
                        "2099-01-01T10:00:00+00:00"
                    ),
                    "rollback_enforcement_finished_at": (
                        "2099-01-01T10:05:00+00:00"
                    ),
                    "rollback_enforced_at": "2099-01-01T10:05:00+00:00",
                    "rollback_to_parameter_set_id": "ps_v1",
                    "rollback_capital_proof_version": (
                        "rdp-rollback-capital-proof/v1"
                    ),
                    "rollback_capital_proof_kind": "rollback",
                    "rollback_capital_operation_id": "op_forged_future",
                    "rollback_capital_proof_verified": True,
                },
                "rel_future_terminal",
            ),
        ]
        for release_id, payload, expected in cases:
            with self.engine.begin() as conn:  # type: ignore[attr-defined]
                conn.execute(text("DELETE FROM governance.release_effectiveness"))
                conn.execute(
                    text(
                        """
                        INSERT INTO governance.release_effectiveness
                          (evaluation_id, release_id, family, timeframe,
                           conclusion, evaluated_at, payload)
                        VALUES
                          (:evaluation_id, :release_id, 'independent', '15m',
                           'rollback_triggered', now(), CAST(:payload AS jsonb))
                        """
                    ),
                    {
                        "evaluation_id": f"eval_{release_id}",
                        "release_id": release_id,
                        "payload": json.dumps(payload),
                    },
                )
            with Session(self.engine) as session:  # type: ignore[arg-type]
                actual = db_get_pending_rollback_release_id(
                    session,
                    family="independent",
                    timeframe="15m",
                )
            self.assertEqual(actual, expected)

    def test_forged_payload_attestation_has_no_immutable_proof(
        self,
    ) -> None:
        """A direct JSONB claim cannot replace the DB-owned proof ledger."""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.active_params_db import (
            db_get_pending_rollback_release_id,
        )

        self._seed_chain_v0_v1_v2()
        release_id = "rel_forged_active_change"
        payload = {
            "evaluation_id": "eval_forged_active_change",
            "release_id": release_id,
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "conclusion": "rollback_triggered",
            "rollback_cancelled": True,
            "rollback_enforced": False,
            "rollback_soft_pause_applied": False,
            "rollback_enforcement_status": "cancelled",
            "rollback_enforcement_attempt_id": "attempt_forged_active",
            "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
            "rollback_enforcement_finished_at": "2026-08-27T10:05:00+00:00",
            "rollback_cancelled_at": "2026-08-27T10:05:00+00:00",
            "rollback_cancelled_reason": (
                "active_parameter_set_changed_before_rollback: forged"
            ),
            "rollback_capital_proof_version": "rdp-rollback-capital-proof/v1",
            "rollback_capital_proof_kind": "active_parameter_changed",
            "rollback_capital_proof_active_parameter_set_id": "ps_v1",
            "rollback_capital_proof_verified": True,
        }
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.parameter_releases
                      (release_id, family, timeframe, combo_key,
                       recommendation_id, parameter_set_id,
                       previous_parameter_set_id, apply_result,
                       observation_status, payload)
                    VALUES
                      (:rid, 'independent', '15m', 'independent_15m',
                       'rec_v2', 'ps_v2', 'ps_v1', 'success', 'observing',
                       '{}'::jsonb)
                    """
                ),
                {"rid": release_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO governance.release_effectiveness
                      (evaluation_id, release_id, family, timeframe,
                       conclusion, evaluated_at, payload)
                    VALUES
                      (:eid, :rid, 'independent', '15m',
                       'rollback_triggered', now(), CAST(:payload AS jsonb))
                    """
                ),
                {
                    "eid": payload["evaluation_id"],
                    "rid": release_id,
                    "payload": json.dumps(payload),
                },
            )

        with Session(self.engine) as session:  # type: ignore[arg-type]
            actual = db_get_pending_rollback_release_id(
                session,
                family="independent",
                timeframe="15m",
            )

        # There is no immutable proof row, so even verified=true in JSON fails.
        self.assertEqual(actual, release_id)

    def test_terminal_proof_survives_two_later_active_changes(self) -> None:
        """Historical terminal truth must not depend on mutable current active."""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.active_params_db import (
            db_get_pending_rollback_release_id,
        )

        self._seed_chain_v0_v1_v2()
        release_id = "rel_immutable_active_change"
        started = "2026-08-27T10:00:00+00:00"
        finished = "2026-08-27T10:05:00+00:00"
        payload = {
            "evaluation_id": "eval_immutable_active_change",
            "release_id": release_id,
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "conclusion": "rollback_triggered",
            "rollback_cancelled": True,
            "rollback_enforced": False,
            "rollback_soft_pause_applied": False,
            "rollback_enforcement_status": "cancelled",
            "rollback_enforcement_attempt_id": "attempt_immutable_active",
            "rollback_enforcement_started_at": started,
            "rollback_enforcement_finished_at": finished,
            "rollback_cancelled_at": finished,
            "rollback_cancelled_reason": (
                "active_parameter_set_changed_before_rollback: proven"
            ),
            "rollback_capital_proof_version": "rdp-rollback-capital-proof/v1",
            "rollback_capital_proof_kind": "active_parameter_changed",
            "rollback_capital_proof_active_parameter_set_id": "ps_v1",
            "rollback_capital_proof_verified": True,
        }
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.parameter_releases
                      (release_id, family, timeframe, combo_key,
                       recommendation_id, parameter_set_id,
                       previous_parameter_set_id, apply_result,
                       observation_status, payload)
                    VALUES
                      (:rid, 'independent', '15m', 'independent_15m',
                       'rec_v1', 'ps_v0', NULL, 'success', 'observing',
                       '{}'::jsonb)
                    """
                ),
                {"rid": release_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO governance.release_effectiveness
                      (evaluation_id, release_id, family, timeframe,
                       conclusion, evaluated_at, payload)
                    VALUES
                      (:eid, :rid, 'independent', '15m',
                       'rollback_triggered', now(), CAST(:payload AS jsonb))
                    """
                ),
                {
                    "eid": payload["evaluation_id"],
                    "rid": release_id,
                    "payload": json.dumps(payload),
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO governance.release_effectiveness_action_proofs
                      (release_id, attempt_id, outcome, proof_kind,
                       started_at_utc, finished_at_utc,
                       observed_active_parameter_set_id, fact_observed_at)
                    VALUES
                      (:rid, :attempt, 'cancelled', 'active_parameter_changed',
                       :started, :finished, 'ps_v1', now())
                    """
                ),
                {
                    "rid": release_id,
                    "attempt": payload["rollback_enforcement_attempt_id"],
                    "started": started,
                    "finished": finished,
                },
            )

        # The terminal proof observed ps_v1.  ps_v2 is already a later active.
        with Session(self.engine) as session:  # type: ignore[arg-type]
            first = db_get_pending_rollback_release_id(
                session,
                family="independent",
                timeframe="15m",
            )
        self.assertIsNone(first)

        self._seed_parameter_set("ps_v3")
        self._seed_active("ps_v3")
        with Session(self.engine) as session:  # type: ignore[arg-type]
            second = db_get_pending_rollback_release_id(
                session,
                family="independent",
                timeframe="15m",
            )
        self.assertIsNone(second)

    def test_release_rollback_before_terminal_ledger_still_blocks_apply(
        self,
    ) -> None:
        """The transaction seam after capital rollback remains fail-closed."""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.active_params_db import (
            db_get_pending_rollback_release_id,
        )

        self._seed_chain_v0_v1_v2()
        self._seed_apply_history(
            "op_rb_seam",
            from_ps="ps_v2",
            to_ps="ps_v1",
            operation_type="rollback",
        )
        self._seed_active("ps_v1")
        release_id = "rel_rollback_before_ledger"
        release_payload = {
            "release_id": release_id,
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "recommendation_id": "rec_v2",
            "parameter_set_id": "ps_v2",
            "apply_result": "success",
            "observation_status": "rolled_back",
            "rollback_to_parameter_set_id": "ps_v1",
            "rollback_operation_id": "op_rb_seam",
            "rollback_capital_proof_version": (
                "rdp-release-rollback-capital-proof/v1"
            ),
            "rollback_capital_proof_verified": True,
        }
        effectiveness_payload = {
            "evaluation_id": "eval_rollback_before_ledger",
            "release_id": release_id,
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "conclusion": "rollback_triggered",
            "rollback_enforcement_status": "in_progress",
            "rollback_enforcement_attempt_id": "attempt_before_ledger",
            "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
        }
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.parameter_releases
                      (release_id, family, timeframe, combo_key,
                       recommendation_id, parameter_set_id,
                       previous_parameter_set_id, apply_result,
                       observation_status, payload)
                    VALUES
                      (:rid, 'independent', '15m', 'independent_15m',
                       'rec_v2', 'ps_v2', 'ps_v1', 'success', 'rolled_back',
                       CAST(:payload AS jsonb))
                    """
                ),
                {"rid": release_id, "payload": json.dumps(release_payload)},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO governance.observation_results
                      (release_id, family, timeframe, combo_key, status,
                       recommendation, observation_window_hours,
                       window_active, evaluated_at, payload)
                    VALUES
                      (:rid, 'independent', '15m', 'independent_15m',
                       'rollback_recommended', 'rollback_recommended', 24,
                       false, now(), '{}'::jsonb)
                    """
                ),
                {"rid": release_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO governance.release_effectiveness
                      (evaluation_id, release_id, family, timeframe,
                       conclusion, evaluated_at, payload)
                    VALUES
                      (:eid, :rid, 'independent', '15m',
                       'rollback_triggered', now(), CAST(:payload AS jsonb))
                    """
                ),
                {
                    "eid": effectiveness_payload["evaluation_id"],
                    "rid": release_id,
                    "payload": json.dumps(effectiveness_payload),
                },
            )

        with Session(self.engine) as session:  # type: ignore[arg-type]
            pending = db_get_pending_rollback_release_id(
                session,
                family="independent",
                timeframe="15m",
            )

        self.assertEqual(pending, release_id)

    def test_operator_rollback_resolves_pending_effectiveness_as_rollback(
        self,
    ) -> None:
        """A completed Operator rollback is enforced truth, not cancellation."""
        from datetime import datetime, timedelta, timezone
        import tempfile

        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.active_params_db import (
            db_get_pending_rollback_release_id,
        )
        from aats.data_platform.governance.operational_state_db import (
            db_load_effectiveness_registry,
            db_load_release_history,
        )
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
        )
        from aats.data_platform.production_workflow.post_apply_evidence import (
            POST_APPLY_EVIDENCE_CONTRACT_VERSION,
            make_source_provenance,
        )

        self._seed_chain_v0_v1_v2()
        release_id = "rel_operator_rollback_pending_effectiveness"
        now = datetime.now(timezone.utc)
        created_at = now - timedelta(hours=3)
        applied_at = now - timedelta(hours=2)
        evaluated_at = now - timedelta(hours=1)
        release_payload = {
            "release_id": release_id,
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "recommendation_id": "rec_v2",
            "parameter_set_id": "ps_v2",
            "previous_parameter_set_id": "ps_v1",
            "actor": "release_test",
            "created_at": created_at.isoformat(),
            "applied_at": applied_at.isoformat(),
            "apply_operation_id": "op_2",
            "apply_result": "success",
            "observation_status": "rollback_recommended",
            "observation_window_hours": 24,
        }
        effectiveness_payload = {
            "evaluation_id": "eval_operator_rollback_pending_effectiveness",
            "release_id": release_id,
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "conclusion": "rollback_triggered",
            "evaluated_at": evaluated_at.isoformat(),
            "rollback_enforcement_status": "pending",
        }
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    UPDATE governance.active_parameter_sets
                    SET approval_recommendation_id = 'rec_v2'
                    WHERE family = 'independent' AND timeframe = '15m'
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO governance.parameter_releases
                      (release_id, family, timeframe, combo_key,
                       recommendation_id, parameter_set_id,
                       previous_parameter_set_id, actor, apply_result,
                       observation_status, observation_window_hours,
                       payload, created_at)
                    VALUES
                      (:rid, 'independent', '15m', 'independent_15m',
                       'rec_v2', 'ps_v2', 'ps_v1', 'release_test', 'success',
                       'rollback_recommended', 24, CAST(:payload AS jsonb),
                       :created_at)
                    """
                ),
                {
                    "rid": release_id,
                    "payload": json.dumps(release_payload),
                    "created_at": created_at,
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO governance.release_effectiveness
                      (evaluation_id, release_id, family, timeframe,
                       conclusion, evaluated_at, payload)
                    VALUES
                      (:eid, :rid, 'independent', '15m',
                       'rollback_triggered', :evaluated_at,
                       CAST(:payload AS jsonb))
                    """
                ),
                {
                    "eid": effectiveness_payload["evaluation_id"],
                    "rid": release_id,
                    "evaluated_at": evaluated_at,
                    "payload": json.dumps(effectiveness_payload),
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            operator_result = self._rollback_helper(
                Path(tmp), to_parameter_set_id="ps_v1"
            )
            self.assertTrue(operator_result["ok"], operator_result)
            self.assertEqual(operator_result["release_id"], release_id)

            with Session(self.engine) as session:  # type: ignore[arg-type]
                release_history = db_load_release_history(session)

            source = make_source_provenance(
                source_kind="governance_snapshot",
                source_id="governance_snapshot_operator_rollback",
                source_timestamp=evaluated_at,
                source_payload={"status": "regression"},
            )
            rollback_evidence = {
                "release_id": release_id,
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "evaluated_at": evaluated_at.isoformat(),
                "rollback_recommended": True,
                "severity": "high",
                "triggers": [
                    {
                        "trigger": "governance_regression",
                        "fired": True,
                        "evidence_status": "valid",
                        "severity": "high",
                        "source_provenance": source,
                    }
                ],
                "fired_trigger_count": 1,
                "evidence_contract_version": (
                    POST_APPLY_EVIDENCE_CONTRACT_VERSION
                ),
                "source_provenance": [source],
            }
            with (
                patch(
                    "aats.data_platform.metrics.release_effectiveness."
                    "try_governance_db",
                    return_value=(self.engine, True),
                ),
                patch(
                    "aats.data_platform.metrics.release_effectiveness."
                    "has_explicit_governance_db_configuration",
                    return_value=True,
                ),
                patch(
                    "aats.data_platform.production_workflow.release_registry."
                    "load_release_history",
                    return_value=release_history,
                ),
                patch(
                    "aats.data_platform.production_workflow.observation_window."
                    "load_observation_result",
                    return_value=None,
                ),
                patch(
                    "aats.data_platform.production_workflow.rollback_policy."
                    "load_rollback_recommendation",
                    return_value=rollback_evidence,
                ),
                patch(
                    "aats.data_platform.decision_system.active_parameter_apply."
                    "rollback_active_parameter_set",
                    return_value={
                        "ok": False,
                        "code": "ACTIVE_SET_CHANGED",
                        "from_parameter_set_id": "ps_v1",
                    },
                ) as duplicate_rollback,
            ):
                results = enforce_pending_rollbacks(Path(tmp))

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"], results)
        self.assertTrue(results[0]["resolved_by_existing_rollback"])
        duplicate_rollback.assert_not_called()

        with Session(self.engine) as session:  # type: ignore[arg-type]
            registry = db_load_effectiveness_registry(session)
            pending = db_get_pending_rollback_release_id(
                session,
                family="independent",
                timeframe="15m",
            )
            proof = session.execute(
                text(
                    """
                    SELECT outcome, proof_kind, operation_id,
                           target_parameter_set_id
                    FROM governance.release_effectiveness_action_proofs
                    WHERE release_id = :release_id
                    """
                ),
                {"release_id": release_id},
            ).fetchone()

        evaluation = next(
            item
            for item in registry["evaluations"]
            if item["release_id"] == release_id
        )
        self.assertEqual(evaluation["rollback_enforcement_status"], "enforced")
        self.assertEqual(evaluation["rollback_capital_proof_kind"], "rollback")
        self.assertTrue(evaluation["rollback_capital_proof_verified"])
        self.assertIsNone(pending)
        self.assertIsNotNone(proof)
        self.assertEqual(proof.outcome, "enforced")
        self.assertEqual(proof.proof_kind, "rollback")
        self.assertEqual(proof.operation_id, operator_result["operation_id"])
        self.assertEqual(proof.target_parameter_set_id, "ps_v1")

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

    def test_validate_rejects_deprecated_without_timestamp(self) -> None:
        self._seed_parameter_set("ps_dep", status="deprecated")
        ok, reason = self._run_validate("ps_dep")
        self.assertFalse(ok)
        self.assertEqual(reason, "target_deprecated_without_timestamp")

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
        # Only draft recommendation — rule 6 rejects since status not in allowlist.
        self._seed_recommendation(
            "rec_draft", target_ps_id="ps_orphan", status="draft"
        )
        self._seed_apply_history(
            "op_orphan",
            from_ps=None,
            to_ps="ps_orphan",
            recommendation_id="rec_draft",
        )
        # Active something else so rule 5 passes.
        self._seed_parameter_set("ps_active_other")
        self._seed_active("ps_active_other")

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

    def test_rollback_rejects_deprecated_without_timestamp(self) -> None:
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
        self.assertEqual(result["reason"], "target_deprecated_without_timestamp")

    def test_rollback_auto_derives_previous_when_not_specified(self) -> None:
        self._seed_chain_v0_v1_v2()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self._rollback_helper(Path(tmp), to_parameter_set_id=None)

        # db_get_previous_set_id returns the most recent apply's from_parameter_set_id
        # which for chain v0→v1→v2 is ps_v1 (from op_2: from=v1, to=v2).
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["to_parameter_set_id"], "ps_v1")
