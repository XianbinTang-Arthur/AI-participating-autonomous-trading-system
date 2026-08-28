"""Real-PostgreSQL contract tests for RDP promotion content identity."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from testcontainers.community.postgres import (  # type: ignore[import-not-found]
        PostgresContainer,
    )

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional integration dependency
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import psycopg2  # type: ignore[import-not-found]  # noqa: F401

    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover - optional integration dependency
    _PSYCOPG2_AVAILABLE = False


_SHOULD_RUN = (
    os.getenv("AATS_RUN_POSTGRES_INTEGRATION") == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)


def _managed_publication_inputs(
    *,
    round_id: str,
    readiness_value: int | float = 1,
) -> dict[str, object]:
    return {
        "round_id": round_id,
        "started_at": "2026-08-28T01:00:00+00:00",
        "finished_at": "2026-08-28T01:01:00+00:00",
        "upgrade_candidates": [],
        "ft_decisions": [
            {
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "decision": "keep_active",
                "confidence": "high",
                "reasons": ["stable"],
            }
        ],
        "evidence_bundle": {
            "evidence_completeness": {
                "phases_with_data": ["phase3", "phase4"],
                "completeness_ratio": 0.5,
            }
        },
        "evidence_summary_path": "evidence_summary.json",
        "readiness_report": {
            "readiness": "not_ready_more_research_needed",
            "typed_value": readiness_value,
        },
        "manifest": {"status": "succeeded"},
        "conclusion_markdown": "# conclusion",
    }


@unittest.skipUnless(
    _SHOULD_RUN,
    "Set AATS_RUN_POSTGRES_INTEGRATION=1 and install .[postgres-integration]",
)
class TestRdpPromotionIdentityPostgres(unittest.TestCase):
    container: "PostgresContainer"
    engine: object

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine

        from aats.data_platform.rdp_models import create_rdp_schema

        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()
        cls.engine = create_engine(cls.container.get_connection_url(), future=True)
        create_rdp_schema(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine") and cls.engine is not None:
            cls.engine.dispose()  # type: ignore[attr-defined]
        if hasattr(cls, "container") and cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        from sqlalchemy import text

        with self.engine.begin() as connection:  # type: ignore[attr-defined]
            connection.execute(
                text(
                    "TRUNCATE TABLE governance.decision_round_snapshots, "
                    "governance.research_round_snapshots, "
                    "governance.decision_evidence_bundles, "
                    "governance.active_decisions, governance.recommendations, "
                    "governance.parameter_sets CASCADE"
                )
            )

    def _read_atomic_publication_state(self) -> dict[str, list[dict[str, object]]]:
        from sqlalchemy import text

        queries = {
            "recommendations": (
                "SELECT recommendation_id, family, symbol, timeframe, "
                "recommendation_type, target_parameter_set_id, "
                "source_round_id, confidence, reason, evidence_bundle_ref, "
                "status, superseded_by_recommendation_id, created_at "
                "FROM governance.recommendations ORDER BY recommendation_id"
            ),
            "active_decisions": (
                "SELECT family, symbol, timeframe, current_status, "
                "active_parameter_set_id, last_recommendation_id, "
                "last_updated_at, notes FROM governance.active_decisions "
                "ORDER BY family, timeframe"
            ),
            "decision_evidence_bundles": (
                "SELECT round_id, evidence_summary_path, phases_with_data, "
                "completeness_ratio, payload, created_at, updated_at "
                "FROM governance.decision_evidence_bundles ORDER BY round_id"
            ),
            "decision_round_snapshots": (
                "SELECT round_id, started_at, finished_at, "
                "evidence_summary_json, parameter_upgrade_candidates_json, "
                "family_timeframe_decisions_json, promotion_readiness_json, "
                "manifest_json, conclusion_markdown, created_at, updated_at "
                "FROM governance.decision_round_snapshots ORDER BY round_id"
            ),
        }
        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            return {
                name: [dict(row) for row in connection.execute(text(sql)).mappings()]
                for name, sql in queries.items()
            }

    def _seed_parameter_set(
        self,
        *,
        parameter_set_id: str,
        family: str,
        timeframe: str,
    ) -> None:
        from sqlalchemy import text

        with self.engine.begin() as connection:  # type: ignore[attr-defined]
            connection.execute(
                text(
                    """
                    INSERT INTO governance.parameter_sets
                        (parameter_set_id, family, symbol, timeframe, values,
                         confidence, status)
                    VALUES
                        (:parameter_set_id, :family, 'BTC-USDT-SWAP',
                         :timeframe, CAST('{}' AS jsonb), 'high', 'candidate')
                    """
                ),
                {
                    "parameter_set_id": parameter_set_id,
                    "family": family,
                    "timeframe": timeframe.lower(),
                },
            )

    def _write_step3_round(
        self,
        project_root: Path,
        *,
        entry_threshold: float,
        round_id: str = "20260827_120000_a1b2c3d4",
        started_at: str = "2026-08-27T12:00:00+00:00",
        finished_at: str = "2026-08-27T12:01:00+00:00",
    ) -> None:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance._exceptions import DBConflictError
        from aats.data_platform.governance.snapshot_db import (
            ROUND_PHASE_STEP2,
            ROUND_PHASE_STEP3,
            db_load_research_round_snapshot,
            db_upsert_research_round_snapshot,
            research_round_snapshot_fingerprint,
        )

        round_dir = project_root / "artifacts/research/step3_rounds" / round_id
        round_dir.mkdir(parents=True)
        candidate_path = round_dir / "parameter_candidates_merged.json"
        combo_keys = [
            "directional_15m",
            "directional_1h",
            "independent_15m",
            "independent_1h",
        ]
        window = {"start": "2026-08-01", "end": "2026-08-27"}

        def _calibration_artifacts(
            target_round_dir: Path,
            topology: dict[str, tuple[str, str, tuple[str, ...]]],
        ) -> list[dict[str, object]]:
            rows: list[dict[str, object]] = []
            child_index = 1
            for round_key, (family, timeframe, batch_keys) in topology.items():
                batches: list[dict[str, object]] = []
                for batch_key in batch_keys:
                    batch_run_id = f"20260827_100000_{child_index:08x}"
                    child_index += 1
                    batch_dir = target_round_dir / "batches" / batch_run_id
                    batch_dir.mkdir(parents=True, exist_ok=True)
                    summary = {
                        "batch_run_id": batch_run_id,
                        "batch_name": f"{family}_{batch_key}_{timeframe.lower()}",
                        "family": family,
                        "symbol": "BTC-USDT-SWAP",
                        "timeframe": timeframe,
                        "dataset_version": "v1.0",
                        "window": f"{window['start']} ~ {window['end']}",
                        "total_experiments": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "experiments": [
                            {
                                "label": f"{batch_key}_baseline",
                                "experiment_id": (
                                    "00000000-0000-4000-8000-"
                                    f"{child_index:012x}"
                                ),
                                "status": "succeeded",
                                "params": {"test_parameter": child_index},
                            }
                        ],
                        "failures": [],
                    }
                    summary_bytes = json.dumps(
                        summary,
                        sort_keys=True,
                    ).encode("utf-8")
                    (batch_dir / "batch_summary.json").write_bytes(summary_bytes)
                    batches.append(
                        {
                            "key": batch_key,
                            "batch_run_id": batch_run_id,
                            "batch_dir": str(batch_dir.resolve()),
                            "status": "succeeded",
                            "summary_sha256": hashlib.sha256(
                                summary_bytes
                            ).hexdigest(),
                            "summary_size_bytes": len(summary_bytes),
                            "total_experiments": 1,
                            "succeeded": 1,
                            "failed": 0,
                        }
                    )
                rows.append(
                    {
                        "round_key": round_key,
                        "family": family,
                        "timeframe": timeframe,
                        "status": "succeeded",
                        "batches": batches,
                    }
                )
            return rows

        candidate_payload = {
            "schema_version": "aats.step3_candidates.v1",
            "round_id": round_id,
            "dataset_version": "v1.0",
            "scope": {
                "symbol": "BTC-USDT-SWAP",
                "step": "step3_merged",
                "combo_keys": combo_keys,
                "combo_count": len(combo_keys),
            },
            "candidates": {
                "independent_15m": {"entry_threshold": entry_threshold},
                "independent_1h": {"entry_threshold": 0.36},
                "directional_15m": {"close_threshold": 0.19},
                "directional_1h": {"close_threshold": 0.2},
            },
            "pending_validation": [],
            "constraint_check": {
                "all_passed": True,
                "violation_count": 0,
                "auto_fix_count": 0,
            },
        }
        candidate_bytes = json.dumps(candidate_payload, sort_keys=True).encode("utf-8")
        candidate_path.write_bytes(candidate_bytes)
        step2_round_id = f"{round_id.rsplit('_', 1)[0]}_1234abcd"
        step2_path = (
            project_root
            / "artifacts/research/step2_rounds"
            / step2_round_id
            / "parameter_candidates.json"
        )
        step2_path.parent.mkdir(parents=True)
        step2_combo_keys = [
            "directional_15m",
            "directional_1h",
            "independent_1h",
        ]
        step2_bytes = json.dumps(
            {
                "schema_version": "aats.step2_candidates.v1",
                "round_id": step2_round_id,
                "dataset_version": "v1.0",
                "scope": {
                    "symbol": "BTC-USDT-SWAP",
                    "step": "step2_candidates",
                    "combo_keys": step2_combo_keys,
                    "combo_count": len(step2_combo_keys),
                },
                "candidates": {
                    key: {"signal_edge_scale_bps": 12.0}
                    for key in step2_combo_keys
                },
                "pending_validation": [],
            },
            sort_keys=True,
        ).encode("utf-8")
        step2_path.write_bytes(step2_bytes)
        step2_calibrations = _calibration_artifacts(
            step2_path.parent,
            {
                "independent_1h": (
                    "independent",
                    "1H",
                    ("scale_calibration", "cost_sensitivity", "confirm_ticks"),
                ),
                "directional_15m": (
                    "directional",
                    "15m",
                    (
                        "scale_calibration",
                        "cost_sensitivity",
                        "confirm_ticks",
                        "trend_weight",
                        "return_clamp",
                    ),
                ),
                "directional_1h": (
                    "directional",
                    "1H",
                    (
                        "scale_calibration",
                        "cost_sensitivity",
                        "confirm_ticks",
                        "trend_weight",
                        "return_clamp",
                    ),
                ),
            },
        )
        step2_scans: list[dict[str, object]] = []
        for scan_index, (scan_key, family, timeframe) in enumerate(
            (
                ("independent_15m", "independent", "15m"),
                ("independent_1h", "independent", "1H"),
                ("directional_15m", "directional", "15m"),
                ("directional_1h", "directional", "1H"),
            ),
            start=1,
        ):
            scan_run_id = (
                f"{round_id.rsplit('_', 1)[-1]}-0000-4000-8000-"
                f"{scan_index:012x}"
            )
            scan_dir = (
                project_root
                / "artifacts/research/experiments"
                / scan_run_id
            )
            scan_dir.mkdir(parents=True, exist_ok=True)
            comparison_bytes = json.dumps(
                {
                    "experiment_count": 1,
                    "comparison": [{"label": f"{scan_key}_baseline"}],
                },
                sort_keys=True,
            ).encode("utf-8")
            (scan_dir / "comparison_summary.json").write_bytes(
                comparison_bytes
            )
            step2_scans.append(
                {
                    "scan_key": scan_key,
                    "family": family,
                    "timeframe": timeframe,
                    "status": "succeeded",
                    "scan_run_id": scan_run_id,
                    "scan_dir": str(scan_dir.resolve()),
                    "comparison_sha256": hashlib.sha256(
                        comparison_bytes
                    ).hexdigest(),
                    "comparison_size_bytes": len(comparison_bytes),
                    "window": window,
                    "dataset_version": "v1.0",
                    "grid_sha256": hashlib.sha256(
                        scan_key.encode("utf-8")
                    ).hexdigest(),
                    "total_combinations": 1,
                    "completed_count": 1,
                    "failed_count": 0,
                }
            )
        step2_manifest = {
            "schema_version": "aats.step2_round.v1",
            "round_id": step2_round_id,
            "phase": "step2",
            "status": "succeeded",
            "started_at": started_at,
            "finished_at": finished_at,
            "symbol": "BTC-USDT-SWAP",
            "dataset_version": "v1.0",
            "scope": {
                "symbol": "BTC-USDT-SWAP",
                "combo_keys": step2_combo_keys,
                "combo_count": len(step2_combo_keys),
                "window": window,
            },
            "input_refs": {
                "dataset_version": "v1.0",
                "window": window,
            },
            "artifact_sha256": {
                step2_path.name: hashlib.sha256(step2_bytes).hexdigest()
            },
            "artifact_size_bytes": {step2_path.name: len(step2_bytes)},
            "calibrations": step2_calibrations,
            "scans": step2_scans,
        }
        step2_manifest_path = step2_path.with_name("round_manifest.json")
        step2_manifest_path.write_text(
            json.dumps(step2_manifest), encoding="utf-8"
        )
        step2_family_summary = {
            "round_id": step2_round_id,
            "results": step2_calibrations,
        }
        step2_scan_summary = {
            "round_id": step2_round_id,
            "results": step2_scans,
        }
        step2_family_summary_path = step2_path.with_name(
            "family_timeframe_summary.json"
        )
        step2_scan_summary_path = step2_path.with_name(
            "scan_comparison_summary.json"
        )
        step2_family_summary_path.write_text(
            json.dumps(step2_family_summary), encoding="utf-8"
        )
        step2_scan_summary_path.write_text(
            json.dumps(step2_scan_summary), encoding="utf-8"
        )
        step2_report_path = step2_path.with_name("report.md")
        step2_report_path.write_text("# Step 2 integration fixture\n", encoding="utf-8")
        try:
            with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                db_upsert_research_round_snapshot(
                    session,
                    round_id=step2_round_id,
                    phase=ROUND_PHASE_STEP2,
                    status="succeeded",
                    round_path=str(step2_path.parent.resolve()),
                    started_at=started_at,
                    finished_at=finished_at,
                    replay_only=False,
                    manifest_payload=step2_manifest,
                    summary_payload={
                        "family_timeframe_summary": step2_family_summary,
                        "scan_comparison_summary": step2_scan_summary,
                        "parameter_candidates": json.loads(
                            step2_bytes.decode("utf-8")
                        ),
                    },
                    conclusion_payload={
                        "report_markdown_path": str(step2_report_path.resolve())
                    },
                    artifacts_payload={
                        "round_dir": str(step2_path.parent.resolve()),
                        "family_timeframe_summary_json": str(
                            step2_family_summary_path.resolve()
                        ),
                        "scan_comparison_summary_json": str(
                            step2_scan_summary_path.resolve()
                        ),
                        "parameter_candidates_json": str(step2_path.resolve()),
                        "round_manifest_json": str(step2_manifest_path.resolve()),
                    },
                )
        except DBConflictError:
            # The content-fork concurrency test deliberately builds the same
            # round ID under two roots.  The first immutable DB snapshot is the
            # authority; the adversarial second root must not replace it.
            pass
        with Session(self.engine) as session:  # type: ignore[arg-type]
            step2_snapshot = db_load_research_round_snapshot(
                session,
                round_id=step2_round_id,
            )
        assert step2_snapshot is not None
        step2_snapshot_sha256 = research_round_snapshot_fingerprint(
            step2_snapshot
        )
        manifest = {
            "schema_version": "aats.step3_round.v1",
            "round_id": round_id,
            "phase": "step3",
            "status": "succeeded",
            "started_at": started_at,
            "finished_at": finished_at,
            "symbol": "BTC-USDT-SWAP",
            "dataset_version": "v1.0",
            "scope": {
                "symbol": "BTC-USDT-SWAP",
                "families": ["directional", "independent"],
                "timeframes": ["15m", "1h"],
                "combo_keys": combo_keys,
                "combo_count": len(combo_keys),
                "window": window,
            },
            "input_refs": {
                "dataset_version": "v1.0",
                "window": window,
                "step2": {
                    "round_id": step2_round_id,
                    "status": "succeeded",
                    "symbol": "BTC-USDT-SWAP",
                    "dataset_version": "v1.0",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "candidate_sha256": hashlib.sha256(step2_bytes).hexdigest(),
                    "window": window,
                },
            },
            "artifact_sha256": {
                candidate_path.name: hashlib.sha256(candidate_bytes).hexdigest()
            },
            "artifact_size_bytes": {candidate_path.name: len(candidate_bytes)},
            "calibrations": _calibration_artifacts(
                round_dir,
                {
                    "independent_15m_expanded": (
                        "independent",
                        "15m",
                        (
                            "entry_threshold",
                            "close_threshold",
                            "de_risk_edge",
                            "failed_thesis_edge",
                            "timing",
                            "cost_buffer",
                        ),
                    ),
                    "independent_1h_expanded": (
                        "independent",
                        "1H",
                        (
                            "entry_threshold",
                            "close_threshold",
                            "de_risk_edge",
                            "failed_thesis_edge",
                            "timing",
                            "cost_buffer",
                        ),
                    ),
                },
            ),
            "scans": [],
        }
        manifest_path = round_dir / "round_manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_bytes = manifest_path.read_bytes()
        report_path = round_dir / "report.md"
        report_path.write_text("# Step 3 integration fixture\n", encoding="utf-8")
        try:
            with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                db_upsert_research_round_snapshot(
                    session,
                    round_id=round_id,
                    phase=ROUND_PHASE_STEP3,
                    status="succeeded",
                    round_path=str(round_dir.resolve()),
                    started_at=started_at,
                    finished_at=finished_at,
                    replay_only=False,
                    manifest_payload=manifest,
                    summary_payload={
                        "parameter_candidates_merged": candidate_payload
                    },
                    conclusion_payload={
                        "report_markdown_path": str(report_path.resolve())
                    },
                    artifacts_payload={
                        "round_dir": str(round_dir.resolve()),
                        "manifest_path": str(manifest_path.resolve()),
                        "manifest_sha256": hashlib.sha256(
                            manifest_bytes
                        ).hexdigest(),
                        "manifest_size_bytes": len(manifest_bytes),
                        "manifest_utf8": manifest_bytes.decode("utf-8"),
                        "candidate_path": str(candidate_path.resolve()),
                        "candidate_sha256": hashlib.sha256(
                            candidate_bytes
                        ).hexdigest(),
                        "candidate_size_bytes": len(candidate_bytes),
                        "candidate_utf8": candidate_bytes.decode("utf-8"),
                        "step2_round_id": step2_round_id,
                        "step2_candidate_sha256": hashlib.sha256(
                            step2_bytes
                        ).hexdigest(),
                        "step2_snapshot_sha256": step2_snapshot_sha256,
                    },
                )
        except DBConflictError:
            # Preserve the producer-owned first snapshot in the deliberate
            # same-ID content-fork test.
            pass

    @staticmethod
    def _parameter_payload() -> dict[str, object]:
        return {
            "parameter_set_id": "ps_identity_postgres",
            "family": "independent",
            "symbol": "BTC-USDT-SWAP",
            "timeframe": "15m",
            "source_round_id": "round_identity_postgres",
            "source_phase": "step3_merged",
            "dataset_version": "v1.0",
            "values": {"entry_threshold": 0.35, "signed_zero": -0.0},
            "confidence": "high",
            "status": "candidate",
        }

    def test_parameter_identity_is_insert_once_and_status_is_cas_bound(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance._exceptions import DBConflictError
        from aats.data_platform.governance.parameter_identity import (
            parameter_values_fingerprint,
        )
        from aats.data_platform.governance.parameter_sets_db import (
            db_get_parameter_set,
            db_update_parameter_set_status,
            db_upsert_parameter_set,
        )

        payload = self._parameter_payload()
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_parameter_set(session, **payload)
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_parameter_set(session, **payload)

        replacement = {**payload, "values": {"entry_threshold": 9.99}}
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            with self.assertRaisesRegex(
                DBConflictError,
                "parameter_set_immutable_identity_conflict",
            ):
                db_upsert_parameter_set(session, **replacement)

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            with self.assertRaisesRegex(
                ValueError,
                "parameter_set_transition_not_allowed",
            ):
                db_update_parameter_set_status(
                    session,
                    "ps_identity_postgres",
                    status="released",
                    expected_current_status="candidate",
                )
            promoted = session.execute(
                text(
                    "UPDATE governance.parameter_sets SET status='released' "
                    "WHERE parameter_set_id=:ps_id AND status='candidate'"
                ),
                {"ps_id": "ps_identity_postgres"},
            ).rowcount
        self.assertEqual(promoted, 1)

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            stale_import = db_update_parameter_set_status(
                session,
                "ps_identity_postgres",
                status="deprecated",
                expected_current_status="candidate",
            )
        self.assertFalse(stale_import)

        with Session(self.engine) as session:  # type: ignore[arg-type]
            stored = db_get_parameter_set(session, "ps_identity_postgres")
        assert stored is not None
        self.assertEqual(stored["values"], payload["values"])
        self.assertEqual(
            parameter_values_fingerprint(stored["values"]),
            parameter_values_fingerprint(payload["values"]),
        )
        self.assertEqual(stored["status"], "released")

    def test_parameter_set_typed_json_identity_survives_jsonb_semantics(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance._exceptions import DBConflictError
        from aats.data_platform.governance.parameter_sets_db import (
            db_upsert_parameter_set,
        )

        base = self._parameter_payload()
        integer_payload = {
            **base,
            "parameter_set_id": "ps_typed_integer",
            "values": {"nested": {"value": 1}, "other": 2.0},
        }
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_parameter_set(session, **integer_payload)
        with self.engine.begin() as connection:  # type: ignore[attr-defined]
            connection.execute(
                text(
                    "UPDATE governance.parameter_sets "
                    "SET typed_json_identity_sha256 = NULL "
                    "WHERE parameter_set_id = 'ps_typed_integer'"
                )
            )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            with self.assertRaisesRegex(
                DBConflictError,
                "parameter_set_immutable_identity_conflict",
            ):
                db_upsert_parameter_set(
                    session,
                    **{
                        **integer_payload,
                        "values": {"nested": {"value": 1.0}, "other": 2.0},
                    },
                )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_parameter_set(session, **integer_payload)

        retry_cases = [
            (
                "ps_typed_zero",
                {"zero": -0.0},
                {"zero": 0.0},
            ),
            (
                "ps_typed_key_order",
                {"a": 1, "b": 2.0},
                {"b": 2.0, "a": 1},
            ),
        ]
        for parameter_set_id, first, retry in retry_cases:
            with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                db_upsert_parameter_set(
                    session,
                    **{
                        **base,
                        "parameter_set_id": parameter_set_id,
                        "values": first,
                    },
                )
            with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                db_upsert_parameter_set(
                    session,
                    **{
                        **base,
                        "parameter_set_id": parameter_set_id,
                        "values": retry,
                    },
                )

        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            missing = connection.execute(
                text(
                    "SELECT count(*) FROM governance.parameter_sets "
                    "WHERE parameter_set_id LIKE 'ps_typed_%' "
                    "AND typed_json_identity_sha256 IS NULL"
                )
            ).scalar_one()
        self.assertEqual(missing, 0)

    def test_research_snapshot_typed_json_identity_survives_jsonb_semantics(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance._exceptions import DBConflictError
        from aats.data_platform.governance.snapshot_db import (
            db_upsert_research_round_snapshot,
        )

        def write(round_id: str, payload: dict[str, object]) -> None:
            with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                db_upsert_research_round_snapshot(
                    session,
                    round_id=round_id,
                    phase="phase3",
                    status="succeeded",
                    manifest_payload=payload,
                )

        write("research_typed_integer", {"nested": {"value": 1}})
        with self.engine.begin() as connection:  # type: ignore[attr-defined]
            connection.execute(
                text(
                    "UPDATE governance.research_round_snapshots "
                    "SET typed_json_identity_sha256 = NULL "
                    "WHERE round_id = 'research_typed_integer'"
                )
            )
        with self.assertRaisesRegex(
            DBConflictError,
            "research_round_snapshot_immutable_identity_conflict",
        ):
            write("research_typed_integer", {"nested": {"value": 1.0}})
        write("research_typed_integer", {"nested": {"value": 1}})
        write("research_typed_zero", {"zero": -0.0})
        write("research_typed_zero", {"zero": 0.0})
        write("research_typed_order", {"a": 1, "b": 2.0})
        write("research_typed_order", {"b": 2.0, "a": 1})
        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            missing = connection.execute(
                text(
                    "SELECT count(*) FROM governance.research_round_snapshots "
                    "WHERE round_id LIKE 'research_typed_%' "
                    "AND typed_json_identity_sha256 IS NULL"
                )
            ).scalar_one()
        self.assertEqual(missing, 0)

    def test_decision_snapshot_typed_json_identity_survives_text_storage(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance._exceptions import DBConflictError
        from aats.data_platform.governance.decision_rounds_db import (
            db_upsert_decision_round_snapshot,
        )

        def write(round_id: str, payload: dict[str, object]) -> None:
            with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                db_upsert_decision_round_snapshot(
                    session,
                    round_id=round_id,
                    manifest=payload,
                )

        write("decision_typed_integer", {"nested": {"value": 1}})
        with self.engine.begin() as connection:  # type: ignore[attr-defined]
            connection.execute(
                text(
                    "UPDATE governance.decision_round_snapshots "
                    "SET typed_json_identity_sha256 = NULL "
                    "WHERE round_id = 'decision_typed_integer'"
                )
            )
        with self.assertRaisesRegex(
            DBConflictError,
            "decision_round_snapshot_immutable_identity_conflict",
        ):
            write("decision_typed_integer", {"nested": {"value": 1.0}})
        write("decision_typed_integer", {"nested": {"value": 1}})
        write("decision_typed_zero", {"zero": -0.0})
        write("decision_typed_zero", {"zero": 0.0})
        write("decision_typed_order", {"a": 1, "b": 2.0})
        write("decision_typed_order", {"b": 2.0, "a": 1})
        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            missing = connection.execute(
                text(
                    "SELECT count(*) FROM governance.decision_round_snapshots "
                    "WHERE round_id LIKE 'decision_typed_%' "
                    "AND typed_json_identity_sha256 IS NULL"
                )
            ).scalar_one()
        self.assertEqual(missing, 0)

    def test_waiting_stale_import_cannot_demote_committed_release(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.parameter_sets_db import (
            db_get_parameter_set,
            db_update_parameter_set_status,
            db_upsert_parameter_set,
        )

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_parameter_set(session, **self._parameter_payload())

        release_session = Session(self.engine)  # type: ignore[arg-type]
        release_session.begin()
        try:
            release_session.execute(
                text(
                    "SELECT parameter_set_id FROM governance.parameter_sets "
                    "WHERE parameter_set_id = :ps_id FOR UPDATE"
                ),
                {"ps_id": "ps_identity_postgres"},
            )
            self.assertEqual(
                release_session.execute(
                    text(
                        "UPDATE governance.parameter_sets SET status='released' "
                        "WHERE parameter_set_id=:ps_id AND status='candidate'"
                    ),
                    {"ps_id": "ps_identity_postgres"},
                ).rowcount,
                1,
            )

            waiter_started = threading.Event()
            waiter_result: dict[str, object] = {}

            def _stale_import() -> None:
                try:
                    with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                        session.execute(
                            text(
                                "SET LOCAL application_name = "
                                "'rdp_identity_import_waiter'"
                            )
                        )
                        session.execute(text("SET LOCAL statement_timeout = '5s'"))
                        waiter_started.set()
                        waiter_result["updated"] = db_update_parameter_set_status(
                            session,
                            "ps_identity_postgres",
                            status="deprecated",
                            expected_current_status="candidate",
                        )
                except Exception as exc:  # pragma: no cover - asserted below
                    waiter_result["error"] = exc

            waiter = threading.Thread(target=_stale_import, daemon=True)
            waiter.start()
            self.assertTrue(waiter_started.wait(timeout=2.0))

            deadline = time.monotonic() + 3.0
            observed_lock_wait = False
            while time.monotonic() < deadline:
                with self.engine.connect() as connection:  # type: ignore[attr-defined]
                    observed_lock_wait = bool(
                        connection.execute(
                            text(
                                "SELECT EXISTS ("
                                "SELECT 1 FROM pg_stat_activity "
                                "WHERE application_name = "
                                "'rdp_identity_import_waiter' "
                                "AND wait_event_type = 'Lock')"
                            )
                        ).scalar_one()
                    )
                if observed_lock_wait:
                    break
                time.sleep(0.01)
            self.assertTrue(observed_lock_wait, "stale importer never waited on row lock")

            release_session.commit()
            waiter.join(timeout=5.0)
            self.assertFalse(waiter.is_alive())
            self.assertNotIn("error", waiter_result)
            self.assertFalse(waiter_result.get("updated"))
        finally:
            if release_session.in_transaction():
                release_session.rollback()
            release_session.close()

        with Session(self.engine) as session:  # type: ignore[arg-type]
            stored = db_get_parameter_set(session, "ps_identity_postgres")
        assert stored is not None
        self.assertEqual(stored["status"], "released")

    def test_global_import_lock_prevents_same_round_content_fork(self) -> None:
        from sqlalchemy import text

        from aats.data_platform.governance import auto_import_candidates as auto_import

        with (
            tempfile.TemporaryDirectory(prefix="rdp-import-a-") as root_a_raw,
            tempfile.TemporaryDirectory(prefix="rdp-import-b-") as root_b_raw,
        ):
            root_a = Path(root_a_raw)
            root_b = Path(root_b_raw)
            self._write_step3_round(root_a, entry_threshold=0.35)
            self._write_step3_round(root_b, entry_threshold=9.99)

            importer_a_entered = threading.Event()
            release_importer_a = threading.Event()
            first_result: dict[str, object] = {}
            original_find = auto_import.find_latest_step3_candidates

            def _controlled_find(project_root: Path) -> Path | None:
                if project_root == root_a and not importer_a_entered.is_set():
                    importer_a_entered.set()
                    if not release_importer_a.wait(timeout=5.0):
                        raise TimeoutError("importer A test barrier timed out")
                return original_find(project_root)

            def _run_first_importer() -> None:
                try:
                    first_result["result"] = auto_import.auto_import_latest_candidates(
                        root_a
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    first_result["error"] = exc

            db_url = self.container.get_connection_url()
            with (
                patch.dict(
                    os.environ,
                    {"AATS_ACTIVE_PARAMETER_DB_URL": db_url},
                    clear=False,
                ),
                patch.object(
                    auto_import,
                    "find_latest_step3_candidates",
                    side_effect=_controlled_find,
                ),
            ):
                first = threading.Thread(target=_run_first_importer, daemon=True)
                first.start()
                self.assertTrue(importer_a_entered.wait(timeout=5.0))

                with self.engine.connect() as connection:  # type: ignore[attr-defined]
                    self.assertEqual(
                        connection.execute(
                            text("SELECT count(*) FROM governance.parameter_sets")
                        ).scalar_one(),
                        0,
                    )

                busy = auto_import.auto_import_latest_candidates(root_b)
                self.assertEqual(busy["status"], "import_lock_busy")

                release_importer_a.set()
                first.join(timeout=10.0)
                self.assertFalse(first.is_alive())
                self.assertNotIn("error", first_result)
                self.assertEqual(
                    first_result["result"]["status"],  # type: ignore[index]
                    "imported",
                )

                conflicting_retry = auto_import.auto_import_latest_candidates(root_b)
                self.assertEqual(
                    conflicting_retry["status"],
                    "round_metadata_invalid",
                )
                # The immutable producer-owned Step 3 snapshot now rejects
                # the alternate root before candidate publication reaches the
                # older parameter-set content-conflict boundary.
                exact_retry = auto_import.auto_import_latest_candidates(root_a)
                self.assertEqual(exact_retry["status"], "already_imported")

                # 模拟其中一项由受控 apply 路径完成 candidate -> released；
                # importer 的同 artifact 精确重试必须保持生命周期且仍然幂等。
                with self.engine.begin() as connection:  # type: ignore[attr-defined]
                    released_id = connection.execute(
                        text(
                            "SELECT parameter_set_id "
                            "FROM governance.parameter_sets "
                            "WHERE source_round_id = "
                            "'20260827_120000_a1b2c3d4' "
                            "ORDER BY parameter_set_id LIMIT 1"
                        )
                    ).scalar_one()
                    connection.execute(
                        text(
                            "UPDATE governance.parameter_sets "
                            "SET status = 'released' "
                            "WHERE parameter_set_id = :parameter_set_id"
                        ),
                        {"parameter_set_id": released_id},
                    )
                released_retry = auto_import.auto_import_latest_candidates(root_a)
                self.assertEqual(released_retry["status"], "already_imported")
                released_row = next(
                    row
                    for row in released_retry["parameter_sets"]
                    if row["id"] == released_id
                )
                self.assertEqual(released_row["status"], "released")

            with self.engine.connect() as connection:  # type: ignore[attr-defined]
                rows = connection.execute(
                    text(
                        "SELECT values, status FROM governance.parameter_sets "
                        "WHERE source_round_id = '20260827_120000_a1b2c3d4'"
                    )
                ).all()
            self.assertEqual(len(rows), 4)
            self.assertEqual(
                sorted(row.status for row in rows),
                ["candidate", "candidate", "candidate", "released"],
            )
            independent_15m = next(
                row for row in rows if "entry_threshold" in row.values
                and float(row.values["entry_threshold"]) == 0.35
            )
            self.assertEqual(
                float(independent_15m.values["entry_threshold"]),
                0.35,
            )

    def test_terminal_member_retry_does_not_remove_older_fallback_candidate(
        self,
    ) -> None:
        """已 deprecated 的本轮成员不能在 importer 重试时替代旧候选。"""
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance import auto_import_candidates as auto_import
        from aats.data_platform.governance.parameter_sets_db import (
            db_upsert_parameter_set,
        )

        old_round_id = "20260827_110000_deadbeef"
        old_parameter_set_id = "ps_import_fallback_old"
        with tempfile.TemporaryDirectory(prefix="rdp-import-terminal-") as root_raw:
            root = Path(root_raw)
            self._write_step3_round(
                root,
                entry_threshold=0.1,
                round_id=old_round_id,
                started_at="2026-08-27T11:00:00+00:00",
                finished_at="2026-08-27T11:01:00+00:00",
            )
            self._write_step3_round(root, entry_threshold=0.35)

            with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                db_upsert_parameter_set(
                    session,
                    parameter_set_id=old_parameter_set_id,
                    family="independent",
                    symbol="BTC-USDT-SWAP",
                    timeframe="15m",
                    source_round_id=old_round_id,
                    source_phase="step3_merged",
                    dataset_version="v1.0",
                    values={"entry_threshold": 0.1},
                    confidence="medium",
                    status="candidate",
                )

            with patch.dict(
                os.environ,
                {"AATS_ACTIVE_PARAMETER_DB_URL": self.container.get_connection_url()},
                clear=False,
            ):
                initial = auto_import.auto_import_latest_candidates(root)
                self.assertEqual(initial["status"], "imported")

                with self.engine.begin() as connection:  # type: ignore[attr-defined]
                    current_id = connection.execute(
                        text(
                            "SELECT parameter_set_id "
                            "FROM governance.parameter_sets "
                            "WHERE source_round_id = '20260827_120000_a1b2c3d4' "
                            "AND family = 'independent' AND timeframe = '15m'"
                        )
                    ).scalar_one()
                    connection.execute(
                        text(
                            "UPDATE governance.parameter_sets "
                            "SET status = 'deprecated', deprecated_at = now() "
                            "WHERE parameter_set_id = :current_id"
                        ),
                        {"current_id": current_id},
                    )
                    connection.execute(
                        text(
                            "UPDATE governance.parameter_sets "
                            "SET status = 'candidate', deprecated_at = NULL "
                            "WHERE parameter_set_id = :old_id"
                        ),
                        {"old_id": old_parameter_set_id},
                    )

                retry = auto_import.auto_import_latest_candidates(root)
                self.assertEqual(retry["status"], "already_imported")

            with self.engine.connect() as connection:  # type: ignore[attr-defined]
                statuses = dict(
                    connection.execute(
                        text(
                            "SELECT parameter_set_id, status "
                            "FROM governance.parameter_sets "
                            "WHERE parameter_set_id IN (:current_id, :old_id)"
                        ),
                        {"current_id": current_id, "old_id": old_parameter_set_id},
                    ).all()
                )
            self.assertEqual(statuses[current_id], "deprecated")
            self.assertEqual(statuses[old_parameter_set_id], "candidate")

    def test_candidate_round_is_not_visible_until_all_drafts_are_published(
        self,
    ) -> None:
        from sqlalchemy import text

        from aats.data_platform.governance import auto_import_candidates as auto_import

        with tempfile.TemporaryDirectory(prefix="rdp-import-stage-") as root_raw:
            root = Path(root_raw)
            self._write_step3_round(root, entry_threshold=0.35)
            first_draft_committed = threading.Event()
            continue_import = threading.Event()
            import_result: dict[str, object] = {}
            original_add = auto_import.add_parameter_set
            add_count = 0

            def _controlled_add(
                registry: dict[str, object],
                parameter_set: dict[str, object],
            ) -> None:
                nonlocal add_count
                original_add(registry, parameter_set)  # type: ignore[arg-type]
                add_count += 1
                if add_count == 1:
                    first_draft_committed.set()
                    if not continue_import.wait(timeout=5.0):
                        raise TimeoutError("draft publication test barrier timed out")

            def _run_import() -> None:
                try:
                    import_result["result"] = (
                        auto_import.auto_import_latest_candidates(root)
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    import_result["error"] = exc

            with (
                patch.dict(
                    os.environ,
                    {"AATS_ACTIVE_PARAMETER_DB_URL": self.container.get_connection_url()},
                    clear=False,
                ),
                patch.object(
                    auto_import,
                    "add_parameter_set",
                    side_effect=_controlled_add,
                ),
            ):
                worker = threading.Thread(target=_run_import, daemon=True)
                worker.start()
                self.assertTrue(first_draft_committed.wait(timeout=5.0))

                with self.engine.connect() as connection:  # type: ignore[attr-defined]
                    counts = connection.execute(
                        text(
                            "SELECT status, count(*) AS n "
                            "FROM governance.parameter_sets GROUP BY status"
                        )
                    ).all()
                by_status = {row.status: int(row.n) for row in counts}
                self.assertEqual(by_status.get("candidate", 0), 0)
                self.assertEqual(by_status.get("draft", 0), 1)

                continue_import.set()
                worker.join(timeout=10.0)
                self.assertFalse(worker.is_alive())
                self.assertNotIn("error", import_result)
                self.assertEqual(
                    import_result["result"]["status"],  # type: ignore[index]
                    "imported",
                )

            with self.engine.connect() as connection:  # type: ignore[attr-defined]
                counts = connection.execute(
                    text(
                        "SELECT status, count(*) AS n "
                        "FROM governance.parameter_sets GROUP BY status"
                    )
                ).all()
            by_status = {row.status: int(row.n) for row in counts}
            self.assertEqual(by_status.get("draft", 0), 0)
            self.assertEqual(by_status.get("candidate", 0), 4)

    def test_decision_round_snapshot_cannot_be_rebound(self) -> None:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance._exceptions import DBConflictError
        from aats.data_platform.governance.decision_rounds_db import (
            db_load_decision_round_snapshot,
            db_upsert_decision_round_snapshot,
        )

        first = [
            {
                "parameter_set_id": "ps_identity_postgres",
                "parameter_values_fingerprint": "a" * 64,
            }
        ]
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_decision_round_snapshot(
                session,
                round_id="round_identity_postgres",
                parameter_upgrade_candidates=first,
            )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_decision_round_snapshot(
                session,
                round_id="round_identity_postgres",
                parameter_upgrade_candidates=first,
            )

        replacement = [
            {
                "parameter_set_id": "ps_identity_postgres",
                "parameter_values_fingerprint": "b" * 64,
            }
        ]
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            with self.assertRaisesRegex(
                DBConflictError,
                "decision_round_snapshot_immutable_identity_conflict",
            ):
                db_upsert_decision_round_snapshot(
                    session,
                    round_id="round_identity_postgres",
                    parameter_upgrade_candidates=replacement,
                )

        with Session(self.engine) as session:  # type: ignore[arg-type]
            stored = db_load_decision_round_snapshot(
                session,
                round_id="round_identity_postgres",
            )
        assert stored is not None
        self.assertEqual(stored["parameter_upgrade_candidates"], first)

    def test_decision_round_control_plane_publishes_atomically(self) -> None:
        from sqlalchemy import text

        from aats.data_platform.decision_system.recommendation_registry import (
            publish_managed_decision_round,
        )
        from aats.data_platform.decision_system import recommendation_registry

        round_id = "20260828_000000_1234abcd"
        with patch.object(
            recommendation_registry,
            "try_governance_db",
            return_value=(self.engine, True),
        ):
            stats = publish_managed_decision_round(
                round_id=round_id,
                started_at="2026-08-28T00:00:00+00:00",
                finished_at="2026-08-28T00:01:00+00:00",
                upgrade_candidates=[],
                ft_decisions=[
                    {
                        "family": "independent",
                        "timeframe": "15m",
                        "combo_key": "independent_15m",
                        "decision": "keep_active",
                        "confidence": "high",
                        "reasons": ["stable"],
                    }
                ],
                evidence_bundle={
                    "evidence_completeness": {
                        "phases_with_data": ["phase3", "phase4"],
                        "completeness_ratio": 0.5,
                    }
                },
                evidence_summary_path="evidence_summary.json",
                readiness_report={
                    "readiness": "not_ready_more_research_needed"
                },
                manifest={"status": "succeeded"},
                conclusion_markdown="# conclusion",
            )

        self.assertEqual(
            stats,
            {
                "recommendations_added": 1,
                "decisions_updated": 1,
                "bundles_registered": 1,
            },
        )
        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            counts = {
                table: connection.execute(
                    text(f"SELECT count(*) FROM governance.{table}")
                ).scalar_one()
                for table in (
                    "recommendations",
                    "active_decisions",
                    "decision_evidence_bundles",
                    "decision_round_snapshots",
                )
            }
        self.assertEqual(counts, {key: 1 for key in counts})

    def test_snapshot_failure_rolls_back_every_decision_round_record(self) -> None:
        from sqlalchemy import text

        from aats.data_platform.decision_system import recommendation_registry
        from aats.data_platform.governance import decision_rounds_db

        def _fail_snapshot(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected snapshot failure")

        with (
            patch.object(
                recommendation_registry,
                "try_governance_db",
                return_value=(self.engine, True),
            ),
            patch.object(
                decision_rounds_db,
                "db_upsert_decision_round_snapshot",
                side_effect=_fail_snapshot,
            ),
            self.assertRaisesRegex(RuntimeError, "injected snapshot failure"),
        ):
            recommendation_registry.publish_managed_decision_round(
                round_id="20260828_000100_1234abcd",
                started_at="2026-08-28T00:01:00+00:00",
                finished_at="2026-08-28T00:02:00+00:00",
                upgrade_candidates=[],
                ft_decisions=[
                    {
                        "family": "independent",
                        "timeframe": "15m",
                        "combo_key": "independent_15m",
                        "decision": "keep_active",
                        "confidence": "high",
                        "reasons": ["stable"],
                    }
                ],
                evidence_bundle={"evidence_completeness": {}},
                evidence_summary_path="evidence_summary.json",
                readiness_report={
                    "readiness": "not_ready_more_research_needed"
                },
                manifest={"status": "succeeded"},
                conclusion_markdown="# conclusion",
            )

        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            counts = {
                table: connection.execute(
                    text(f"SELECT count(*) FROM governance.{table}")
                ).scalar_one()
                for table in (
                    "recommendations",
                    "active_decisions",
                    "decision_evidence_bundles",
                    "decision_round_snapshots",
                )
            }
        self.assertEqual(counts, {key: 0 for key in counts})

    def test_sticky_pause_aborts_atomic_round_without_clearing_pause(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.decision_system import recommendation_registry
        from aats.data_platform.governance._exceptions import DBConflictError
        from aats.data_platform.governance.recommendations_db import (
            db_upsert_active_decision,
        )

        self._seed_parameter_set(
            parameter_set_id="ps_sticky_pause",
            family="independent",
            timeframe="15m",
        )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            self.assertTrue(
                db_upsert_active_decision(
                    session,
                    family="independent",
                    timeframe="15m",
                    current_status="pause",
                    active_parameter_set_id="ps_sticky_pause",
                    notes="operator safety pause",
                )
            )

        with (
            patch.object(
                recommendation_registry,
                "try_governance_db",
                return_value=(self.engine, True),
            ),
            self.assertRaisesRegex(DBConflictError, "sticky_pause"),
        ):
            recommendation_registry.publish_managed_decision_round(
                round_id="20260828_000200_1234abcd",
                started_at="2026-08-28T00:02:00+00:00",
                finished_at="2026-08-28T00:03:00+00:00",
                upgrade_candidates=[],
                ft_decisions=[
                    {
                        "family": "independent",
                        "timeframe": "15m",
                        "combo_key": "independent_15m",
                        "decision": "keep_active",
                        "confidence": "high",
                        "reasons": ["stable"],
                    }
                ],
                evidence_bundle={"evidence_completeness": {}},
                evidence_summary_path="evidence_summary.json",
                readiness_report={
                    "readiness": "not_ready_more_research_needed"
                },
                manifest={"status": "succeeded"},
                conclusion_markdown="# conclusion",
            )

        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            active_row = connection.execute(
                text(
                    "SELECT current_status, active_parameter_set_id "
                    "FROM governance.active_decisions "
                    "WHERE family = 'independent' AND timeframe = '15m'"
                )
            ).mappings().one()
            control_plane_count = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM governance.recommendations) + "
                    "(SELECT count(*) FROM governance.decision_evidence_bundles) + "
                    "(SELECT count(*) FROM governance.decision_round_snapshots)"
                )
            ).scalar_one()
        self.assertEqual(active_row["current_status"], "pause")
        self.assertEqual(
            active_row["active_parameter_set_id"],
            "ps_sticky_pause",
        )
        self.assertEqual(control_plane_count, 0)

    def test_exact_decision_round_retry_is_zero_mutation(self) -> None:
        from aats.data_platform.decision_system import recommendation_registry

        round_id = "20260828_010000_1234abcd"
        first_inputs = _managed_publication_inputs(round_id=round_id)
        with patch.object(
            recommendation_registry,
            "try_governance_db",
            return_value=(self.engine, True),
        ):
            first_stats = (
                recommendation_registry.publish_managed_decision_round(
                    **first_inputs,  # type: ignore[arg-type]
                )
            )
        before = self._read_atomic_publication_state()

        retry_inputs = _managed_publication_inputs(round_id=round_id)
        with patch.object(
            recommendation_registry,
            "try_governance_db",
            return_value=(self.engine, True),
        ):
            retry_stats = (
                recommendation_registry.publish_managed_decision_round(
                    **retry_inputs,  # type: ignore[arg-type]
                )
            )
        after = self._read_atomic_publication_state()

        self.assertEqual(retry_stats, first_stats)
        self.assertEqual(after, before)
        self.assertEqual(
            retry_inputs["manifest"],
            first_inputs["manifest"],
        )

    def test_extra_bundle_recommendation_makes_round_retry_conflict(self) -> None:
        from sqlalchemy.orm import Session

        from aats.data_platform.decision_system import recommendation_registry
        from aats.data_platform.governance._exceptions import DBConflictError
        from aats.data_platform.governance.recommendations_db import (
            db_upsert_recommendation,
        )

        round_id = "20260828_010050_1234abcd"
        with patch.object(
            recommendation_registry,
            "try_governance_db",
            return_value=(self.engine, True),
        ):
            recommendation_registry.publish_managed_decision_round(
                **_managed_publication_inputs(  # type: ignore[arg-type]
                    round_id=round_id
                )
            )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_recommendation(
                session,
                recommendation_id="rec_fabricated_bundle_ref",
                family="independent",
                symbol="BTC-USDT-SWAP",
                timeframe="15m",
                recommendation_type="require_review",
                confidence="low",
                reason="fabricated",
                evidence_bundle_ref=round_id,
                status="draft",
                created_at="2026-08-28T01:01:00+00:00",
            )
        before = self._read_atomic_publication_state()

        with (
            patch.object(
                recommendation_registry,
                "try_governance_db",
                return_value=(self.engine, True),
            ),
            self.assertRaisesRegex(
                DBConflictError,
                "decision_round_recommendation_mapping_conflict",
            ),
        ):
            recommendation_registry.publish_managed_decision_round(
                **_managed_publication_inputs(  # type: ignore[arg-type]
                    round_id=round_id
                )
            )

        self.assertEqual(self._read_atomic_publication_state(), before)

    def test_typed_drift_retry_is_rejected_before_any_mutation(self) -> None:
        from aats.data_platform.decision_system import recommendation_registry
        from aats.data_platform.governance._exceptions import DBConflictError

        round_id = "20260828_010100_1234abcd"
        with patch.object(
            recommendation_registry,
            "try_governance_db",
            return_value=(self.engine, True),
        ):
            recommendation_registry.publish_managed_decision_round(
                **_managed_publication_inputs(  # type: ignore[arg-type]
                    round_id=round_id,
                    readiness_value=1,
                )
            )
        before = self._read_atomic_publication_state()

        with (
            patch.object(
                recommendation_registry,
                "try_governance_db",
                return_value=(self.engine, True),
            ),
            self.assertRaisesRegex(
                DBConflictError,
                "decision_round_publication_identity_conflict",
            ),
        ):
            recommendation_registry.publish_managed_decision_round(
                **_managed_publication_inputs(  # type: ignore[arg-type]
                    round_id=round_id,
                    readiness_value=1.0,
                )
            )

        self.assertEqual(self._read_atomic_publication_state(), before)

    def test_keep_and_pause_preserve_existing_active_parameter_sets(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.decision_system import recommendation_registry
        from aats.data_platform.governance.recommendations_db import (
            db_upsert_active_decision,
        )

        self._seed_parameter_set(
            parameter_set_id="ps_keep_old",
            family="independent",
            timeframe="15m",
        )
        self._seed_parameter_set(
            parameter_set_id="ps_pause_old",
            family="directional",
            timeframe="1h",
        )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            self.assertTrue(
                db_upsert_active_decision(
                    session,
                    family="independent",
                    timeframe="15m",
                    current_status="observe",
                    active_parameter_set_id="ps_keep_old",
                )
            )
            self.assertTrue(
                db_upsert_active_decision(
                    session,
                    family="directional",
                    timeframe="1h",
                    current_status="observe",
                    active_parameter_set_id="ps_pause_old",
                )
            )

        inputs = _managed_publication_inputs(
            round_id="20260828_010200_1234abcd"
        )
        inputs["ft_decisions"] = [
            {
                "family": "independent",
                "timeframe": "15m",
                "decision": "keep_active",
                "confidence": "high",
                "reasons": ["stable"],
            },
            {
                "family": "directional",
                "timeframe": "1H",
                "decision": "pause",
                "confidence": "low",
                "reasons": ["risk"],
            },
        ]
        with patch.object(
            recommendation_registry,
            "try_governance_db",
            return_value=(self.engine, True),
        ):
            recommendation_registry.publish_managed_decision_round(
                **inputs,  # type: ignore[arg-type]
            )

        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            rows = connection.execute(
                text(
                    "SELECT family, timeframe, current_status, "
                    "active_parameter_set_id FROM governance.active_decisions "
                    "ORDER BY family, timeframe"
                )
            ).mappings()
            by_combo = {
                f"{row['family']}_{row['timeframe']}": dict(row)
                for row in rows
            }
        self.assertEqual(
            by_combo["independent_15m"]["active_parameter_set_id"],
            "ps_keep_old",
        )
        self.assertEqual(
            by_combo["directional_1h"]["active_parameter_set_id"],
            "ps_pause_old",
        )
        self.assertEqual(
            by_combo["directional_1h"]["current_status"],
            "pause",
        )

    def test_promote_candidate_replaces_existing_active_parameter_set(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.decision_system import recommendation_registry
        from aats.data_platform.governance.recommendations_db import (
            db_upsert_active_decision,
        )

        for parameter_set_id in ("ps_promote_old", "ps_promote_new"):
            self._seed_parameter_set(
                parameter_set_id=parameter_set_id,
                family="independent",
                timeframe="15m",
            )
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            self.assertTrue(
                db_upsert_active_decision(
                    session,
                    family="independent",
                    timeframe="15m",
                    current_status="observe",
                    active_parameter_set_id="ps_promote_old",
                )
            )

        inputs = _managed_publication_inputs(
            round_id="20260828_010300_1234abcd"
        )
        inputs["upgrade_candidates"] = [
            {
                "family": "independent",
                "timeframe": "15m",
                "decision": "promote_candidate",
                "parameter_set_id": "ps_promote_new",
                "source_round_id": "20260828_005900_abcdef12",
                "confidence": "high",
                "reason": "qualified",
            }
        ]
        with patch.object(
            recommendation_registry,
            "try_governance_db",
            return_value=(self.engine, True),
        ):
            recommendation_registry.publish_managed_decision_round(
                **inputs,  # type: ignore[arg-type]
            )

        with self.engine.connect() as connection:  # type: ignore[attr-defined]
            active_parameter_set_id = connection.execute(
                text(
                    "SELECT active_parameter_set_id "
                    "FROM governance.active_decisions "
                    "WHERE family = 'independent' AND timeframe = '15m'"
                )
            ).scalar_one()
        self.assertEqual(active_parameter_set_id, "ps_promote_new")
