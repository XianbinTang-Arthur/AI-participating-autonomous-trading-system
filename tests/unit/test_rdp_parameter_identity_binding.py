from __future__ import annotations

import json
import hashlib
import math
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aats.data_platform.decision_system.active_parameter_apply import (
    apply_approved_recommendation,
)
from aats.data_platform.decision_system.candidate_selector import (
    select_parameter_upgrade_candidates,
)
from aats.data_platform.governance._exceptions import DBConflictError
from aats.data_platform.governance.decision_rounds_db import (
    db_upsert_decision_round_snapshot,
)
from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)
from aats.data_platform.governance.parameter_registry import add_parameter_set
from aats.data_platform.governance.parameter_sets_db import (
    db_update_parameter_set_status,
    db_upsert_parameter_set,
)


class _Result:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def fetchone(self) -> object | None:
        return self._row


class _CaptureSession:
    def __init__(self, row: object | None) -> None:
        self.row = row
        self.statement = ""
        self.params: dict[str, object] = {}

    def execute(self, statement: object, params: dict[str, object]) -> _Result:
        self.statement = str(statement)
        self.params = params
        return _Result(self.row)


class _UpdateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _UpdateCaptureSession:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.statement = ""
        self.params: dict[str, object] = {}

    def execute(self, statement: object, params: dict[str, object]) -> _UpdateResult:
        self.statement = str(statement)
        self.params = params
        return _UpdateResult(self.rowcount)


def _parameter_set(*, values: dict[str, object]) -> dict[str, object]:
    return {
        "parameter_set_id": "ps_identity_1",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "source_round_id": "round_source_1",
        "source_phase": "phase2_step2",
        "dataset_version": "v1.0",
        "confidence": "high",
        "status": "candidate",
        "values": values,
    }


def test_parameter_values_fingerprint_is_canonical_and_domain_separated() -> None:
    first = parameter_values_fingerprint(
        {"阈值": 1.25, "nested": {"enabled": True, "weights": [2, 1]}}
    )
    second = parameter_values_fingerprint(
        {"nested": {"weights": [2, 1], "enabled": True}, "阈值": 1.25}
    )

    assert first == second
    assert len(first) == 64
    assert first == first.lower()
    assert first != parameter_values_fingerprint({"阈值": 1.26})
    assert parameter_values_fingerprint({"zero": -0.0}) == (
        parameter_values_fingerprint({"zero": 0.0})
    )


def test_parameter_values_fingerprint_v1_golden_digest() -> None:
    assert parameter_values_fingerprint(
        {
            "nested": {"enabled": True, "weights": [2, 1]},
            "zero": -0.0,
            "阈值": 1.25,
        }
    ) == "c125f9dbef3cb80e58989d57212936cdf052ab764e84be584d3fd2529ac68ec1"


@pytest.mark.parametrize(
    "values",
    [None, [], {1: "bad-key"}, {"x": math.nan}, {"x": math.inf}, {"x": object()}],
)
def test_parameter_values_fingerprint_rejects_non_json_or_nonfinite_values(
    values: object,
) -> None:
    with pytest.raises(ValueError, match="parameter_values_invalid"):
        parameter_values_fingerprint(values)


def test_candidate_binds_exact_parameter_values_fingerprint() -> None:
    values = {"entry_threshold": 0.35}
    parameter_set = _parameter_set(values=values)

    with (
        patch(
            "aats.data_platform.decision_system.candidate_selector._evaluate_phase2_score",
            return_value={
                "dimension": "phase2",
                "score": 0.0,
                "max_score": 3.0,
                "promotion_evidence_qualified": False,
                "details": [],
            },
        ),
        patch(
            "aats.data_platform.decision_system.candidate_selector._evaluate_phase3_score",
            return_value={"dimension": "phase3", "score": 0.0, "max_score": 2.0, "details": []},
        ),
        patch(
            "aats.data_platform.decision_system.candidate_selector._evaluate_phase4_score",
            return_value={"dimension": "phase4", "score": 0.0, "max_score": 2.0, "details": []},
        ),
        patch(
            "aats.data_platform.decision_system.candidate_selector._evaluate_governance_score",
            return_value={"dimension": "phase5", "score": 0.0, "max_score": 2.0, "details": []},
        ),
    ):
        candidate = select_parameter_upgrade_candidates([parameter_set], {})[0]

    assert candidate["parameter_values_fingerprint"] == parameter_values_fingerprint(values)


def test_parameter_set_db_writer_accepts_only_identity_equivalent_retry() -> None:
    accepted = _CaptureSession(SimpleNamespace(parameter_set_id="ps_identity_1"))
    db_upsert_parameter_set(
        accepted,  # type: ignore[arg-type]
        parameter_set_id="ps_identity_1",
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        source_round_id="round_source_1",
        source_phase="phase2_step2",
        values={"entry_threshold": 0.35},
        confidence="high",
        status="draft",
    )
    assert (
        "values::text IS NOT DISTINCT FROM EXCLUDED.values::text"
        in accepted.statement
    )
    assert "typed_json_identity_sha256 = COALESCE" in accepted.statement
    assert "status          = EXCLUDED.status" not in accepted.statement
    assert "RETURNING parameter_set_id" in accepted.statement

    rejected = _CaptureSession(None)
    with pytest.raises(
        DBConflictError,
        match="parameter_set_immutable_identity_conflict",
    ):
        db_upsert_parameter_set(
            rejected,  # type: ignore[arg-type]
            parameter_set_id="ps_identity_1",
            family="independent",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            source_round_id="round_source_1",
            source_phase="phase2_step2",
            values={"entry_threshold": 9.99},
            confidence="high",
        )


@pytest.mark.parametrize("audit_field", ["frozen_at", "deprecated_at"])
def test_parameter_set_db_writer_rejects_initial_lifecycle_audit_fields(
    audit_field: str,
) -> None:
    session = _CaptureSession(SimpleNamespace(parameter_set_id="ps_identity_1"))

    with pytest.raises(ValueError, match="生命周期审计字段"):
        db_upsert_parameter_set(
            session,  # type: ignore[arg-type]
            parameter_set_id="ps_identity_1",
            family="independent",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            source_round_id="round_source_1",
            source_phase="phase2_step2",
            values={"entry_threshold": 0.35},
            confidence="high",
            status="candidate",
            **{audit_field: "2026-08-27T12:00:00+00:00"},
        )

    assert session.statement == ""


def test_generic_parameter_status_writer_rejects_released_lifecycle_rewrite() -> None:
    session = _UpdateCaptureSession(1)

    with pytest.raises(ValueError, match="parameter_set_transition_not_allowed"):
        db_update_parameter_set_status(
            session,  # type: ignore[arg-type]
            "ps_identity_1",
            status="deprecated",
            expected_current_status="released",
        )
    assert session.statement == ""


def test_deprecate_cas_conflict_leaves_registry_unchanged() -> None:
    from aats.data_platform.governance.parameter_registry import (
        deprecate_parameter_set,
    )

    original = _parameter_set(values={"entry_threshold": 0.35})
    registry = {"parameter_sets": [original.copy()]}

    with patch(
        "aats.data_platform.governance.parameter_registry._db_update_status",
        return_value=False,
    ) as update:
        changed = deprecate_parameter_set(
            registry,
            "ps_identity_1",
            notes="new round",
        )

    assert changed is False
    assert registry["parameter_sets"][0] == original
    update.assert_called_once()
    assert update.call_args.kwargs["expected_current_status"] == "candidate"


def test_registry_helpers_reject_released_lifecycle_rewrites() -> None:
    from aats.data_platform.governance.parameter_registry import (
        deprecate_parameter_set,
        freeze_parameter_set,
    )

    released = {**_parameter_set(values={"entry_threshold": 0.35}), "status": "released"}
    registry = {"parameter_sets": [released.copy()]}

    with patch(
        "aats.data_platform.governance.parameter_registry._db_update_status"
    ) as update:
        assert deprecate_parameter_set(registry, "ps_identity_1") is False
        assert freeze_parameter_set(registry, "ps_identity_1") is False

    assert registry["parameter_sets"][0] == released
    update.assert_not_called()


_NEW_STEP3_ROUND = "20260827_120000_a1b2c3d4"
_OLD_STEP3_ROUND = "20260827_110000_0a1b2c3d"


@contextmanager
def _no_import_lock(_project_root: Path):
    yield


@contextmanager
def _offline_auto_import(auto_import: object):
    with (
        patch(
            "aats.data_platform.governance.parameter_registry.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.governance.parameter_registry."
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch.object(
            auto_import,
            "_parameter_candidate_import_lock",
            side_effect=_no_import_lock,
        ),
    ):
        yield


def _write_step3_candidates(
    project_root: Path,
    *,
    round_id: str = _NEW_STEP3_ROUND,
    symbol: str = "BTC-USDT-SWAP",
    dataset_version: str = "v1.0",
    status: str = "succeeded",
    started_at: str = "2026-08-27T12:00:00+00:00",
    finished_at: str = "2026-08-27T12:01:00+00:00",
    candidates: dict[str, dict[str, object]] | None = None,
) -> Path:
    candidate_path = (
        project_root
        / f"artifacts/research/step3_rounds/{round_id}"
        / "parameter_candidates_merged.json"
    )
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_map = (
        dict(candidates)
        if candidates is not None
        else {
            "independent_15m": {"entry_threshold": 0.35},
            "independent_1h": {"entry_threshold": 0.36},
            "directional_15m": {"close_threshold": 0.19},
            "directional_1h": {"close_threshold": 0.2},
        }
    )
    if status == "succeeded":
        candidate_map.setdefault("independent_15m", {"entry_threshold": 0.35})
        candidate_map.setdefault("independent_1h", {"entry_threshold": 0.36})
        candidate_map.setdefault("directional_15m", {"close_threshold": 0.19})
        candidate_map.setdefault("directional_1h", {"close_threshold": 0.2})
    combo_keys = sorted(candidate_map)
    window = {"start": "2026-08-01", "end": "2026-08-27"}
    payload = {
        "schema_version": "aats.step3_candidates.v1",
        "round_id": round_id,
        "dataset_version": dataset_version,
        "scope": {
            "symbol": symbol,
            "step": "step3_merged",
            "combo_keys": combo_keys,
            "combo_count": len(combo_keys),
        },
        "candidates": candidate_map,
        "pending_validation": [],
        "constraint_check": {
            "all_passed": True,
            "violation_count": 0,
            "auto_fix_count": 0,
        },
    }
    candidate_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    candidate_path.write_bytes(candidate_bytes)
    step2_round_id = "20260827_100000_1234abcd"
    step2_path = (
        project_root
        / "artifacts/research/step2_rounds"
        / step2_round_id
        / "parameter_candidates.json"
    )
    step2_path.parent.mkdir(parents=True, exist_ok=True)
    step2_combo_keys = [
        "directional_15m",
        "directional_1h",
        "independent_1h",
    ]
    step2_bytes = json.dumps(
        {
            "schema_version": "aats.step2_candidates.v1",
            "round_id": step2_round_id,
            "dataset_version": dataset_version,
            "scope": {
                "symbol": symbol,
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
    step2_calibration_topology = {
        "independent_1h": (
            "independent", "1H",
            ("scale_calibration", "cost_sensitivity", "confirm_ticks"),
        ),
        "directional_15m": (
            "directional", "15m",
            (
                "scale_calibration", "cost_sensitivity", "confirm_ticks",
                "trend_weight", "return_clamp",
            ),
        ),
        "directional_1h": (
            "directional", "1H",
            (
                "scale_calibration", "cost_sensitivity", "confirm_ticks",
                "trend_weight", "return_clamp",
            ),
        ),
    }

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
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "dataset_version": dataset_version,
                    "window": f"{window['start']} ~ {window['end']}",
                    "total_experiments": 1,
                    "succeeded": 1,
                    "failed": 0,
                    "experiments": [
                        {
                            "label": f"{batch_key}_baseline",
                            "experiment_id": (
                                f"00000000-0000-4000-8000-{child_index:012x}"
                            ),
                            "status": "succeeded",
                            "params": {"test_parameter": child_index},
                        }
                    ],
                    "failures": [],
                }
                summary_bytes = json.dumps(summary, sort_keys=True).encode("utf-8")
                (batch_dir / "batch_summary.json").write_bytes(summary_bytes)
                batches.append({
                    "key": batch_key,
                    "batch_run_id": batch_run_id,
                    "batch_dir": str(batch_dir.resolve()),
                    "status": "succeeded",
                    "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
                    "summary_size_bytes": len(summary_bytes),
                    "total_experiments": 1,
                    "succeeded": 1,
                    "failed": 0,
                })
            rows.append({
                "round_key": round_key,
                "family": family,
                "timeframe": timeframe,
                "status": "succeeded",
                "batches": batches,
            })
        return rows

    step2_calibrations = _calibration_artifacts(
        step2_path.parent,
        step2_calibration_topology,
    )
    step2_scan_topology = {
        "independent_15m": ("independent", "15m"),
        "independent_1h": ("independent", "1H"),
        "directional_15m": ("directional", "15m"),
        "directional_1h": ("directional", "1H"),
    }
    step2_scans: list[dict[str, object]] = []
    for scan_index, (scan_key, (family, timeframe)) in enumerate(
        step2_scan_topology.items(),
        start=1,
    ):
        scan_run_id = f"00000000-0000-4000-8000-{scan_index:012x}"
        scan_dir = (
            project_root / "artifacts/research/experiments" / scan_run_id
        )
        scan_dir.mkdir(parents=True, exist_ok=True)
        comparison_bytes = json.dumps(
            {
                "experiment_count": 1,
                "comparison": [{"label": f"{scan_key}_baseline"}],
            },
            sort_keys=True,
        ).encode("utf-8")
        (scan_dir / "comparison_summary.json").write_bytes(comparison_bytes)
        step2_scans.append({
            "scan_key": scan_key,
            "family": family,
            "timeframe": timeframe,
            "status": "succeeded",
            "scan_run_id": scan_run_id,
            "scan_dir": str(scan_dir.resolve()),
            "comparison_sha256": hashlib.sha256(comparison_bytes).hexdigest(),
            "comparison_size_bytes": len(comparison_bytes),
            "window": window,
            "dataset_version": dataset_version,
            "grid_sha256": hashlib.sha256(scan_key.encode()).hexdigest(),
            "total_combinations": 1,
            "completed_count": 1,
            "failed_count": 0,
        })
    step2_manifest = {
        "schema_version": "aats.step2_round.v1",
        "round_id": step2_round_id,
        "phase": "step2",
        "status": "succeeded",
        "started_at": "2026-08-27T10:00:00+00:00",
        "finished_at": "2026-08-27T10:01:00+00:00",
        "symbol": symbol,
        "dataset_version": dataset_version,
        "scope": {
            "symbol": symbol,
            "combo_keys": step2_combo_keys,
            "combo_count": len(step2_combo_keys),
            "window": window,
        },
        "input_refs": {
            "dataset_version": dataset_version,
            "window": window,
        },
        "artifact_sha256": {
            step2_path.name: hashlib.sha256(step2_bytes).hexdigest()
        },
        "artifact_size_bytes": {step2_path.name: len(step2_bytes)},
        "calibrations": step2_calibrations,
        "scans": step2_scans,
    }
    step2_path.with_name("round_manifest.json").write_text(
        json.dumps(step2_manifest),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "aats.step3_round.v1",
        "round_id": round_id,
        "phase": "step3",
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "symbol": symbol,
        "dataset_version": dataset_version,
        "scope": {
            "symbol": symbol,
            "families": sorted(key.rsplit("_", 1)[0] for key in combo_keys),
            "timeframes": sorted(key.rsplit("_", 1)[1] for key in combo_keys),
            "combo_keys": combo_keys,
            "combo_count": len(combo_keys),
            "window": window,
        },
        "input_refs": {
            "dataset_version": dataset_version,
            "window": window,
            "step2": {
                "round_id": step2_round_id,
                "status": "succeeded",
                "symbol": symbol,
                "dataset_version": dataset_version,
                "started_at": "2026-08-27T10:00:00+00:00",
                "finished_at": "2026-08-27T10:01:00+00:00",
                "candidate_sha256": hashlib.sha256(step2_bytes).hexdigest(),
                "window": window,
            },
        },
        "artifact_sha256": {
            candidate_path.name: hashlib.sha256(candidate_bytes).hexdigest()
        },
        "artifact_size_bytes": {candidate_path.name: len(candidate_bytes)},
        "calibrations": (
            _calibration_artifacts(
                candidate_path.parent,
                {
                    "independent_15m_expanded": (
                        "independent", "15m",
                        (
                            "entry_threshold", "close_threshold", "de_risk_edge",
                            "failed_thesis_edge", "timing", "cost_buffer",
                        ),
                    ),
                    "independent_1h_expanded": (
                        "independent", "1H",
                        (
                            "entry_threshold", "close_threshold", "de_risk_edge",
                            "failed_thesis_edge", "timing", "cost_buffer",
                        ),
                    ),
                },
            )
            if status == "succeeded"
            else []
        ),
        "scans": [],
    }
    (candidate_path.parent / "round_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return candidate_path


def _expected_step3_sets(candidate_path: Path) -> list[dict[str, object]]:
    from aats.data_platform.governance import auto_import_candidates as auto_import
    from aats.data_platform.governance.parameter_registry import (
        import_from_parameter_candidates,
    )

    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    parameter_sets = import_from_parameter_candidates(
        candidate_path,
        source_round_id=str(payload["round_id"]),
        source_phase="step3_merged",
        dataset_version=str(payload["dataset_version"]),
        symbol=str(payload["scope"]["symbol"]),
        initial_status="candidate",
        candidate_data=payload,
    )
    for parameter_set in parameter_sets:
        parameter_set["parameter_set_id"] = auto_import._stable_step3_parameter_set_id(
            parameter_set
        )
    return parameter_sets


def _managed_step3_snapshot_fixtures(
    project_root: Path,
    candidate_path: Path,
) -> dict[str, dict[str, object]]:
    from aats.data_platform.governance import snapshot_db

    round_dir = candidate_path.parent.resolve()
    manifest_path = round_dir / "round_manifest.json"
    candidate_bytes = candidate_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    candidate_payload = json.loads(candidate_bytes.decode("utf-8"))
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    step2_round_id = manifest["input_refs"]["step2"]["round_id"]
    step2_round_dir = (
        project_root
        / "artifacts/research/step2_rounds"
        / step2_round_id
    ).resolve()
    step2_manifest = json.loads(
        (step2_round_dir / "round_manifest.json").read_text(encoding="utf-8")
    )
    step2_snapshot: dict[str, object] = {
        "round_id": step2_round_id,
        "phase": snapshot_db.ROUND_PHASE_STEP2,
        "status": "succeeded",
        "round_path": str(step2_round_dir),
        "started_at": step2_manifest["started_at"],
        "finished_at": step2_manifest["finished_at"],
        "replay_only": False,
        "manifest": step2_manifest,
        "summary": {},
        "conclusion": {},
        "artifacts": {},
        "data_source": "db",
    }
    step3_snapshot: dict[str, object] = {
        "round_id": candidate_payload["round_id"],
        "phase": snapshot_db.ROUND_PHASE_STEP3,
        "status": manifest["status"],
        "round_path": str(round_dir),
        "started_at": manifest["started_at"],
        "finished_at": manifest["finished_at"],
        "replay_only": False,
        "manifest": manifest,
        "summary": {"parameter_candidates_merged": candidate_payload},
        "conclusion": {},
        "artifacts": {
            "round_dir": str(round_dir),
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "manifest_size_bytes": len(manifest_bytes),
            "manifest_utf8": manifest_bytes.decode("utf-8"),
            "candidate_path": str(candidate_path.resolve()),
            "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "candidate_size_bytes": len(candidate_bytes),
            "candidate_utf8": candidate_bytes.decode("utf-8"),
            "step2_round_id": step2_round_id,
            "step2_candidate_sha256": manifest["input_refs"]["step2"][
                "candidate_sha256"
            ],
            "step2_snapshot_sha256": (
                snapshot_db.research_round_snapshot_fingerprint(step2_snapshot)
            ),
        },
        "data_source": "db",
    }
    return {
        str(step2_round_id): step2_snapshot,
        str(candidate_payload["round_id"]): step3_snapshot,
    }


def test_managed_step3_candidate_requires_exact_db_snapshot(tmp_path: Path) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    snapshots = _managed_step3_snapshot_fixtures(tmp_path, candidate_path)

    with (
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
        patch.object(
            auto_import,
            "load_research_round_snapshot",
            side_effect=lambda *, round_id, **_kwargs: snapshots.get(round_id),
        ),
    ):
        artifact = auto_import.load_validated_formal_step3_candidate(
            tmp_path,
            candidate_path,
        )

    assert artifact is not None
    assert artifact.candidate_sha256 == hashlib.sha256(
        candidate_path.read_bytes()
    ).hexdigest()


def test_managed_step3_candidate_rejects_missing_snapshot(tmp_path: Path) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    with (
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
        patch.object(
            auto_import,
            "load_research_round_snapshot",
            return_value=None,
        ),
    ):
        assert auto_import.load_validated_formal_step3_candidate(
            tmp_path,
            candidate_path,
        ) is None


def test_managed_step3_candidate_rejects_coherent_file_rewrite(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    snapshots = _managed_step3_snapshot_fixtures(tmp_path, candidate_path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["candidates"]["independent_15m"]["entry_threshold"] = 9.99
    candidate_bytes = json.dumps(candidate, sort_keys=True).encode("utf-8")
    candidate_path.write_bytes(candidate_bytes)
    manifest_path = candidate_path.with_name("round_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"][candidate_path.name] = hashlib.sha256(
        candidate_bytes
    ).hexdigest()
    manifest["artifact_size_bytes"][candidate_path.name] = len(candidate_bytes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with (
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
        patch.object(
            auto_import,
            "load_research_round_snapshot",
            side_effect=lambda *, round_id, **_kwargs: snapshots.get(round_id),
        ),
    ):
        assert auto_import.load_validated_formal_step3_candidate(
            tmp_path,
            candidate_path,
        ) is None


def test_managed_step3_candidate_rejects_numeric_type_drift_in_db_anchor(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(
        tmp_path,
        candidates={
            "independent_15m": {"entry_threshold": 1.0},
        },
    )
    snapshots = _managed_step3_snapshot_fixtures(tmp_path, candidate_path)
    step3_snapshot = snapshots[candidate_path.parent.name]
    anchored_candidate = step3_snapshot["summary"][  # type: ignore[index]
        "parameter_candidates_merged"
    ]
    anchored_candidate["candidates"]["independent_15m"][  # type: ignore[index]
        "entry_threshold"
    ] = 1

    with (
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
        patch.object(
            auto_import,
            "load_research_round_snapshot",
            side_effect=lambda *, round_id, **_kwargs: snapshots.get(round_id),
        ),
    ):
        assert auto_import.load_validated_formal_step3_candidate(
            tmp_path,
            candidate_path,
        ) is None


def test_managed_step3_candidate_rejects_manifest_numeric_type_drift(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    snapshots = _managed_step3_snapshot_fixtures(tmp_path, candidate_path)
    step3_snapshot = snapshots[candidate_path.parent.name]
    anchored_manifest = step3_snapshot["manifest"]  # type: ignore[assignment]
    anchored_manifest["scope"]["combo_count"] = 4.0  # type: ignore[index]

    with (
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
        patch.object(
            auto_import,
            "load_research_round_snapshot",
            side_effect=lambda *, round_id, **_kwargs: snapshots.get(round_id),
        ),
    ):
        assert auto_import.load_validated_formal_step3_candidate(
            tmp_path,
            candidate_path,
        ) is None


def test_managed_step3_candidate_rejects_wrong_step2_snapshot(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    snapshots = _managed_step3_snapshot_fixtures(tmp_path, candidate_path)
    step2_round_id = next(
        key for key in snapshots if key != candidate_path.parent.name
    )
    snapshots[step2_round_id] = {
        **snapshots[step2_round_id],
        "summary": {"drift": True},
    }

    with (
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
        patch.object(
            auto_import,
            "load_research_round_snapshot",
            side_effect=lambda *, round_id, **_kwargs: snapshots.get(round_id),
        ),
    ):
        assert auto_import.load_validated_formal_step3_candidate(
            tmp_path,
            candidate_path,
        ) is None


def test_managed_step3_publication_failure_blocks_snapshot_boundary(
    tmp_path: Path,
) -> None:
    from scripts import rdp_run_step3_research as step3_script

    candidate_path = _write_step3_candidates(tmp_path)
    snapshots = _managed_step3_snapshot_fixtures(tmp_path, candidate_path)
    manifest_path = candidate_path.with_name("round_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    step2_ref = manifest["input_refs"]["step2"]
    with (
        patch.object(
            step3_script,
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
        patch.object(
            step3_script,
            "load_research_round_snapshot",
            return_value=snapshots[step2_ref["round_id"]],
        ),
        patch.object(
            step3_script,
            "save_research_round_snapshot",
            return_value=False,
        ) as save,
    ):
        published = step3_script._publish_managed_step3_snapshot(
            project_root=tmp_path,
            round_dir=candidate_path.parent,
            manifest_path=manifest_path,
            candidate_path=candidate_path,
            candidate_payload=json.loads(candidate_path.read_text(encoding="utf-8")),
            round_id=candidate_path.parent.name,
            round_status="succeeded",
            started_at=manifest["started_at"],
            finished_at=manifest["finished_at"],
            step2_provenance=step2_ref,
            conclusion_path=candidate_path.with_name(
                "phase2_step3_research_conclusion.md"
            ),
        )

    assert published is False
    save.assert_called_once()


def test_managed_step3_publication_rejects_numeric_type_drift_before_db_anchor(
    tmp_path: Path,
) -> None:
    from scripts import rdp_run_step3_research as step3_script

    candidate_path = _write_step3_candidates(
        tmp_path,
        candidates={
            "independent_15m": {"entry_threshold": 1.0},
        },
    )
    snapshots = _managed_step3_snapshot_fixtures(tmp_path, candidate_path)
    manifest_path = candidate_path.with_name("round_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    step2_ref = manifest["input_refs"]["step2"]
    stale_candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    stale_candidate_payload["candidates"]["independent_15m"][
        "entry_threshold"
    ] = 1

    with (
        patch.object(
            step3_script,
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
        patch.object(
            step3_script,
            "load_research_round_snapshot",
            return_value=snapshots[step2_ref["round_id"]],
        ),
        patch.object(
            step3_script,
            "save_research_round_snapshot",
            return_value=True,
        ) as save,
    ):
        published = step3_script._publish_managed_step3_snapshot(
            project_root=tmp_path,
            round_dir=candidate_path.parent,
            manifest_path=manifest_path,
            candidate_path=candidate_path,
            candidate_payload=stale_candidate_payload,
            round_id=candidate_path.parent.name,
            round_status="succeeded",
            started_at=manifest["started_at"],
            finished_at=manifest["finished_at"],
            step2_provenance=step2_ref,
            conclusion_path=candidate_path.with_name(
                "phase2_step3_research_conclusion.md"
            ),
        )

    assert published is False
    save.assert_not_called()


def test_formal_step3_candidate_lineage_binds_raw_and_resolved_values(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance.parameter_candidate_lineage import (
        load_parameter_candidate_lineage,
    )

    candidate_path = _write_step3_candidates(tmp_path)
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    lineage = load_parameter_candidate_lineage(candidate_path, project_root=tmp_path)

    assert lineage["status"] == "bound"
    assert lineage["source_step3_round_id"] == _NEW_STEP3_ROUND
    assert lineage["source_step3_candidate_sha256"] == hashlib.sha256(
        candidate_path.read_bytes()
    ).hexdigest()
    assert lineage["combos"]["independent_15m"][
        "parameter_values_fingerprint"
    ] == parameter_values_fingerprint(payload["candidates"]["independent_15m"])
    assert lineage["combos"]["independent_15m"][
        "resolved_parameter_values_fingerprint"
    ] != lineage["combos"]["independent_15m"][
        "parameter_values_fingerprint"
    ]


def test_formal_step3_candidate_lineage_rejects_digest_drift(tmp_path: Path) -> None:
    from aats.data_platform.governance.parameter_candidate_lineage import (
        load_parameter_candidate_lineage,
    )

    candidate_path = _write_step3_candidates(tmp_path)
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    payload["candidates"]["independent_15m"]["entry_threshold"] = 0.99
    candidate_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="parameter_candidate_formal_validation_failed",
    ):
        load_parameter_candidate_lineage(candidate_path, project_root=tmp_path)


def test_formal_step3_candidate_lineage_rejects_validation_time_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import parameter_candidate_lineage as lineage

    candidate_path = _write_step3_candidates(tmp_path)
    from aats.data_platform.governance import auto_import_candidates as auto_import

    original_reader = auto_import.require_regular_round_file

    def _changed_canonical_read(path: Path, *, parent: Path) -> bytes:
        payload = original_reader(path, parent=parent)
        if path.name == candidate_path.name:
            return payload + b"\n"
        return payload

    monkeypatch.setattr(
        auto_import,
        "require_regular_round_file",
        _changed_canonical_read,
    )

    with pytest.raises(
        ValueError,
        match="parameter_candidate_formal_validation_failed",
    ):
        lineage.load_parameter_candidate_lineage(
            candidate_path,
            project_root=tmp_path,
        )


def test_formal_step3_candidate_lineage_rejects_other_project_root(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance.parameter_candidate_lineage import (
        load_parameter_candidate_lineage,
    )

    candidate_path = _write_step3_candidates(tmp_path)

    with pytest.raises(
        ValueError,
        match="parameter_candidate_formal_validation_failed",
    ):
        load_parameter_candidate_lineage(candidate_path)


def test_formal_step3_candidate_lineage_rejects_step2_child_digest_drift(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance.parameter_candidate_lineage import (
        load_parameter_candidate_lineage,
    )

    candidate_path = _write_step3_candidates(tmp_path)
    step2_batches = sorted(
        (tmp_path / "artifacts/research/step2_rounds").glob(
            "*/batches/*/batch_summary.json"
        )
    )
    assert step2_batches
    step2_batches[0].write_bytes(step2_batches[0].read_bytes() + b"\n")

    with pytest.raises(
        ValueError,
        match="parameter_candidate_formal_validation_failed",
    ):
        load_parameter_candidate_lineage(candidate_path, project_root=tmp_path)


def test_auto_import_recovers_partial_round_before_deprecating_old(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
        candidates={"independent_15m": {"entry_threshold": 0.1}},
    )
    expected = _expected_step3_sets(candidate_path)
    old = {
        **_parameter_set(values={"entry_threshold": 0.1}),
        "parameter_set_id": "ps_old_candidate",
        "source_round_id": _OLD_STEP3_ROUND,
        "source_phase": "step3_merged",
    }
    registry_path = (
        tmp_path / "artifacts/governance/current_parameter_registry.json"
    )
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps({"version": 1, "parameter_sets": [expected[0], old]}),
        encoding="utf-8",
    )

    events: list[str] = []
    original_add = auto_import.add_parameter_set
    original_deprecate = auto_import.deprecate_parameter_set

    def _record_add(registry: dict[str, object], parameter_set: dict[str, object]) -> None:
        events.append("add")
        original_add(registry, parameter_set)  # type: ignore[arg-type]

    def _record_deprecate(
        registry: dict[str, object],
        parameter_set_id: str,
        *,
        notes: str | None = None,
        replacement_parameter_set: dict[str, object] | None = None,
    ) -> bool:
        events.append("deprecate")
        return original_deprecate(  # type: ignore[arg-type]
            registry,
            parameter_set_id,
            notes=notes,
            replacement_parameter_set=replacement_parameter_set,
        )

    with (
        patch(
            "aats.data_platform.governance.parameter_registry.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.governance.parameter_registry."
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch.object(
            auto_import,
            "_parameter_candidate_import_lock",
            side_effect=_no_import_lock,
        ),
        patch.object(auto_import, "add_parameter_set", side_effect=_record_add),
        patch.object(
            auto_import,
            "deprecate_parameter_set",
            side_effect=_record_deprecate,
        ),
    ):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "recovered_partial_import"
    assert result["imported_count"] == 3
    assert result["deprecated_count"] == 1
    assert events == ["add", "add", "add", "deprecate"]

    persisted = json.loads(registry_path.read_text(encoding="utf-8"))
    current_round = [
        parameter_set
        for parameter_set in persisted["parameter_sets"]
        if parameter_set.get("source_round_id") == _NEW_STEP3_ROUND
    ]
    assert len(current_round) == 4
    assert next(
        parameter_set
        for parameter_set in persisted["parameter_sets"]
        if parameter_set["parameter_set_id"] == "ps_old_candidate"
    )["status"] == "deprecated"


def test_auto_import_fails_closed_on_same_round_content_conflict(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    expected = _expected_step3_sets(candidate_path)
    conflicting = {**expected[0], "values": {"entry_threshold": 9.99}}
    registry_path = (
        tmp_path / "artifacts/governance/current_parameter_registry.json"
    )
    registry_path.parent.mkdir(parents=True)
    original = {"version": 1, "parameter_sets": [conflicting]}
    registry_path.write_text(json.dumps(original), encoding="utf-8")

    with (
        patch(
            "aats.data_platform.governance.parameter_registry.try_governance_db",
            return_value=(None, False),
        ),
        patch.object(
            auto_import,
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch.object(
            auto_import,
            "_parameter_candidate_import_lock",
            side_effect=_no_import_lock,
        ),
    ):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "round_content_conflict"
    assert json.loads(registry_path.read_text(encoding="utf-8")) == original


def test_auto_import_reconciles_old_candidate_after_full_round_exists(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
        candidates={"independent_15m": {"entry_threshold": 0.1}},
    )
    expected = _expected_step3_sets(candidate_path)
    old = {
        **_parameter_set(values={"entry_threshold": 0.1}),
        "parameter_set_id": "ps_old_candidate",
        "source_round_id": _OLD_STEP3_ROUND,
        "source_phase": "step3_merged",
    }
    registry_path = tmp_path / "artifacts/governance/current_parameter_registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps({"parameter_sets": [*expected, old]}),
        encoding="utf-8",
    )

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "reconciled_import"
    assert result["imported_count"] == 0
    assert result["deprecated_count"] == 1


def test_auto_import_refreshes_managed_mirror_after_status_cas_conflict(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(tmp_path)
    _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
        candidates={"independent_15m": {"entry_threshold": 0.1}},
    )
    expected = _expected_step3_sets(candidate_path)
    old_candidate = {
        **_parameter_set(values={"entry_threshold": 0.1}),
        "parameter_set_id": "ps_old_candidate",
        "source_round_id": _OLD_STEP3_ROUND,
        "source_phase": "step3_merged",
    }
    initial = {"parameter_sets": [*expected, old_candidate]}
    canonical = {
        "parameter_sets": [
            *expected,
            {**old_candidate, "status": "released"},
        ]
    }

    with (
        patch.object(auto_import, "load_registry", side_effect=[initial, canonical]),
        patch.object(
            auto_import,
            "_parameter_candidate_import_lock",
            side_effect=_no_import_lock,
        ),
        patch.object(auto_import, "deprecate_parameter_set", return_value=False),
            patch.object(
                auto_import,
                "has_explicit_governance_db_configuration",
                return_value=True,
            ),
            patch.object(
                auto_import,
                "_managed_step3_snapshot_matches",
                return_value=True,
            ),
            patch.object(auto_import, "save_registry") as save,
    ):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "concurrent_transition_preserved"
    assert result["status_conflict_count"] == 1
    save.assert_called_once()
    assert save.call_args.args[0] is canonical


def test_auto_import_never_deprecates_a_different_symbol(tmp_path: Path) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        symbol="BTC-USDT-SWAP",
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
        candidates={"independent_15m": {"entry_threshold": 0.1}},
    )
    _write_step3_candidates(tmp_path, symbol="ETH-USDT-SWAP")
    old_btc = {
        **_parameter_set(values={"entry_threshold": 0.1}),
        "parameter_set_id": "ps_old_btc",
        "source_round_id": _OLD_STEP3_ROUND,
        "source_phase": "step3_merged",
    }
    registry_path = tmp_path / "artifacts/governance/current_parameter_registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps({"parameter_sets": [old_btc]}),
        encoding="utf-8",
    )

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "imported"
    assert result["deprecated_count"] == 0
    persisted = json.loads(registry_path.read_text(encoding="utf-8"))
    stored_old = next(
        item for item in persisted["parameter_sets"]
        if item["parameter_set_id"] == "ps_old_btc"
    )
    assert stored_old["status"] == "candidate"
    assert {
        item["symbol"] for item in persisted["parameter_sets"]
        if item["source_round_id"] == _NEW_STEP3_ROUND
    } == {"ETH-USDT-SWAP"}


@pytest.mark.parametrize(
    ("round_status", "requested_status"),
    [("partial_success", "candidate"), ("succeeded", "draft")],
)
def test_auto_import_draft_or_partial_round_cannot_remove_candidate(
    tmp_path: Path,
    round_status: str,
    requested_status: str,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
        candidates={"independent_15m": {"entry_threshold": 0.1}},
    )
    _write_step3_candidates(tmp_path, status=round_status)
    old = {
        **_parameter_set(values={"entry_threshold": 0.1}),
        "parameter_set_id": "ps_old_candidate",
        "source_round_id": _OLD_STEP3_ROUND,
        "source_phase": "step3_merged",
    }
    registry_path = tmp_path / "artifacts/governance/current_parameter_registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps({"parameter_sets": [old]}),
        encoding="utf-8",
    )

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(
            tmp_path,
            initial_status=requested_status,
        )

    assert result["status"] == "imported"
    assert result["round_status"] == round_status
    assert result["effective_initial_status"] == "draft"
    assert result["deprecated_count"] == 0
    persisted = json.loads(registry_path.read_text(encoding="utf-8"))
    assert next(
        item for item in persisted["parameter_sets"]
        if item["parameter_set_id"] == "ps_old_candidate"
    )["status"] == "candidate"
    assert {
        item["status"] for item in persisted["parameter_sets"]
        if item["source_round_id"] == _NEW_STEP3_ROUND
    } == {"draft"}


def test_auto_import_old_round_replay_defers_supersession(tmp_path: Path) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    older_path = _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
        candidates={"independent_15m": {"entry_threshold": 0.1}},
    )
    newer_path = _write_step3_candidates(
        tmp_path,
        candidates={"independent_15m": {"entry_threshold": 0.35}},
    )
    newer = _expected_step3_sets(newer_path)[0]
    registry_path = tmp_path / "artifacts/governance/current_parameter_registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps({"parameter_sets": [newer]}),
        encoding="utf-8",
    )

    with (
        _offline_auto_import(auto_import),
        patch.object(
            auto_import,
            "find_latest_step3_candidates",
            return_value=older_path,
        ),
    ):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "supersession_deferred"
    assert result["deprecation_skipped_count"] == 1
    persisted = json.loads(registry_path.read_text(encoding="utf-8"))
    assert next(
        item for item in persisted["parameter_sets"]
        if item["parameter_set_id"] == newer["parameter_set_id"]
    )["status"] == "candidate"


def test_auto_import_exact_candidate_never_switches_to_global_latest(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    exact_older = _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
    )
    _write_step3_candidates(tmp_path)

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(
            tmp_path,
            candidates_file=exact_older,
        )

    assert result["status"] == "imported"
    assert result["source_round_id"] == _OLD_STEP3_ROUND
    assert Path(result["source_file"]) == exact_older.resolve()


@pytest.mark.parametrize(
    "relative_path",
    [
        "outside.json",
        "artifacts/research/step3_rounds/not-a-round/parameter_candidates_merged.json",
        f"artifacts/research/step3_rounds/{_NEW_STEP3_ROUND}/wrong.json",
    ],
)
def test_auto_import_rejects_unbound_exact_candidate_path(
    tmp_path: Path,
    relative_path: str,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    supplied = tmp_path / relative_path
    supplied.parent.mkdir(parents=True, exist_ok=True)
    supplied.write_text("{}", encoding="utf-8")

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(
            tmp_path,
            candidates_file=supplied,
        )

    assert result["status"] == "round_metadata_invalid"
    assert result["imported_count"] == 0


def test_auto_import_preserves_dataset_version_and_symbol(tmp_path: Path) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    _write_step3_candidates(
        tmp_path,
        symbol="ETH-USDT-SWAP",
        dataset_version="v2.0",
        candidates={"independent_15m": {"entry_threshold": 0.35}},
    )

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "imported"
    registry_path = tmp_path / "artifacts/governance/current_parameter_registry.json"
    stored = json.loads(registry_path.read_text(encoding="utf-8"))["parameter_sets"]
    assert len(stored) == 4
    assert {item["symbol"] for item in stored} == {"ETH-USDT-SWAP"}
    assert {item["dataset_version"] for item in stored} == {"v2.0"}


def test_latest_step3_selection_uses_bound_started_at_within_same_second(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    lexically_larger_but_earlier = _write_step3_candidates(
        tmp_path,
        round_id="20260827_120000_ffffffff",
        started_at="2026-08-27T12:00:00.100000+00:00",
        finished_at="2026-08-27T12:01:00+00:00",
    )
    actually_later = _write_step3_candidates(
        tmp_path,
        round_id="20260827_120000_00000000",
        started_at="2026-08-27T12:00:00.900000+00:00",
        finished_at="2026-08-27T12:00:30+00:00",
    )

    assert auto_import.find_latest_step3_candidates(tmp_path) == actually_later
    assert actually_later != lexically_larger_but_earlier


@pytest.mark.parametrize(
    "failure_mode",
    [
        "digest_mismatch",
        "missing_candidates",
        "failed_round",
        "duplicate_step3_topology",
        "duplicate_step2_topology",
        "step2_pending_type_invalid",
    ],
)
def test_auto_import_rejects_unbound_or_malformed_candidate_without_writes(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    candidate_path = _write_step3_candidates(
        tmp_path,
        status="failed" if failure_mode == "failed_round" else "succeeded",
    )
    if failure_mode == "digest_mismatch":
        candidate_path.write_text("{}", encoding="utf-8")
    elif failure_mode == "missing_candidates":
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        payload.pop("candidates")
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        candidate_path.write_bytes(payload_bytes)
        manifest_path = candidate_path.parent / "round_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_sha256"][candidate_path.name] = hashlib.sha256(
            payload_bytes
        ).hexdigest()
        manifest["artifact_size_bytes"][candidate_path.name] = len(payload_bytes)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif failure_mode == "duplicate_step3_topology":
        manifest_path = candidate_path.parent / "round_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["calibrations"].append(dict(manifest["calibrations"][0]))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif failure_mode == "duplicate_step2_topology":
        step2_manifest_path = (
            tmp_path
            / "artifacts/research/step2_rounds/20260827_100000_1234abcd"
            / "round_manifest.json"
        )
        step2_manifest = json.loads(
            step2_manifest_path.read_text(encoding="utf-8")
        )
        step2_manifest["scans"].append(dict(step2_manifest["scans"][0]))
        step2_manifest_path.write_text(
            json.dumps(step2_manifest),
            encoding="utf-8",
        )
    elif failure_mode == "step2_pending_type_invalid":
        step2_path = (
            tmp_path
            / "artifacts/research/step2_rounds/20260827_100000_1234abcd"
            / "parameter_candidates.json"
        )
        step2_payload = json.loads(step2_path.read_text(encoding="utf-8"))
        step2_payload["pending_validation"] = [123]
        step2_bytes = json.dumps(step2_payload, sort_keys=True).encode("utf-8")
        step2_path.write_bytes(step2_bytes)
        step2_digest = hashlib.sha256(step2_bytes).hexdigest()
        step2_manifest_path = step2_path.with_name("round_manifest.json")
        step2_manifest = json.loads(
            step2_manifest_path.read_text(encoding="utf-8")
        )
        step2_manifest["artifact_sha256"][step2_path.name] = step2_digest
        step2_manifest["artifact_size_bytes"][step2_path.name] = len(step2_bytes)
        step2_manifest_path.write_text(
            json.dumps(step2_manifest),
            encoding="utf-8",
        )
        step3_manifest_path = candidate_path.with_name("round_manifest.json")
        step3_manifest = json.loads(
            step3_manifest_path.read_text(encoding="utf-8")
        )
        step3_manifest["input_refs"]["step2"][
            "candidate_sha256"
        ] = step2_digest
        step3_manifest_path.write_text(
            json.dumps(step3_manifest),
            encoding="utf-8",
        )

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "round_metadata_invalid"
    assert not (tmp_path / "artifacts/governance/current_parameter_registry.json").exists()


def test_auto_import_does_not_fall_back_past_latest_incomplete_round(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
    )
    latest = _write_step3_candidates(tmp_path)
    (latest.parent / "round_manifest.json").unlink()

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "round_metadata_invalid"
    assert result["source_round_id"] == _NEW_STEP3_ROUND


def test_auto_import_does_not_fall_back_past_latest_round_without_candidate(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    _write_step3_candidates(
        tmp_path,
        round_id=_OLD_STEP3_ROUND,
        started_at="2026-08-27T11:00:00+00:00",
        finished_at="2026-08-27T11:01:00+00:00",
    )
    latest_dir = (
        tmp_path / "artifacts/research/step3_rounds" / _NEW_STEP3_ROUND
    )
    latest_dir.mkdir(parents=True)
    (latest_dir / "round_manifest.json").write_text("{}", encoding="utf-8")

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "round_metadata_invalid"
    assert result["source_round_id"] == _NEW_STEP3_ROUND


def test_auto_import_rejects_nonstandard_round_directory_without_fallback(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    _write_step3_candidates(tmp_path, round_id=_OLD_STEP3_ROUND)
    untrusted = tmp_path / "artifacts/research/step3_rounds/latest_manual"
    untrusted.mkdir(parents=True)

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "round_metadata_invalid"
    assert result["source_file"].endswith(
        "latest_manual\\parameter_candidates_merged.json"
    )


def test_latest_step3_selection_rejects_equal_started_at_tie(
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance import auto_import_candidates as auto_import

    started_at = "2026-08-27T12:00:00.500000+00:00"
    _write_step3_candidates(
        tmp_path,
        round_id="20260827_120000_aaaaaaaa",
        started_at=started_at,
    )
    _write_step3_candidates(
        tmp_path,
        round_id="20260827_120000_bbbbbbbb",
        started_at=started_at,
    )

    with _offline_auto_import(auto_import):
        result = auto_import.auto_import_latest_candidates(tmp_path)

    assert result["status"] == "round_metadata_invalid"
    assert result["source_file"].endswith(
        "ambiguous_parameter_candidates_merged.json"
    )


def test_decision_round_snapshot_writer_is_insert_once() -> None:
    accepted = _CaptureSession(SimpleNamespace(round_id="round_identity_1"))
    db_upsert_decision_round_snapshot(
        accepted,  # type: ignore[arg-type]
        round_id="round_identity_1",
        started_at="2026-08-27T12:00:00+00:00",
        finished_at="2026-08-27T12:01:00+00:00",
        parameter_upgrade_candidates=[
            {
                "parameter_set_id": "ps_identity_1",
                "parameter_values_fingerprint": "a" * 64,
            }
        ],
        manifest={"status": "succeeded"},
    )
    assert (
        "parameter_upgrade_candidates_json::text IS NOT DISTINCT FROM"
        in accepted.statement
    )
    assert "typed_json_identity_sha256 = COALESCE" in accepted.statement
    assert "parameter_upgrade_candidates_json = EXCLUDED" not in accepted.statement
    assert "RETURNING round_id" in accepted.statement

    rejected = _CaptureSession(None)
    with pytest.raises(
        DBConflictError,
        match="decision_round_snapshot_immutable_identity_conflict",
    ):
        db_upsert_decision_round_snapshot(
            rejected,  # type: ignore[arg-type]
            round_id="round_identity_1",
            parameter_upgrade_candidates=[
                {
                    "parameter_set_id": "ps_identity_1",
                    "parameter_values_fingerprint": "b" * 64,
                }
            ],
        )


def test_file_registry_rejects_same_id_replacement_before_mutation() -> None:
    original = _parameter_set(values={"entry_threshold": 0.35})
    registry = {"parameter_sets": [original]}
    replacement = _parameter_set(values={"entry_threshold": 9.99})

    with (
        patch(
            "aats.data_platform.governance.parameter_registry.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.governance.parameter_registry."
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        pytest.raises(
            DBConflictError,
            match="parameter_set_immutable_identity_conflict",
        ),
    ):
        add_parameter_set(registry, replacement)

    assert registry == {"parameter_sets": [original]}


def _recommendation() -> dict[str, object]:
    return {
        "recommendation_id": "rec_identity_1",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_identity_1",
        "source_round_id": "round_source_1",
        "confidence": "high",
        "reason": "immutable recommendation rationale",
        "evidence_bundle_ref": "20260827_120000_deadbeef",
        "status": "approved",
    }


def _qualification_verdict(values: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        to_dict=lambda: {
            "parameter_values_fingerprint": parameter_values_fingerprint(values)
        }
    )


def test_dry_run_rejects_values_different_from_qualified_candidate() -> None:
    rec = _recommendation()
    qualified_values = {"entry_threshold": 0.35}
    registry_values = {"entry_threshold": 9.99}
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=True),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard."
            "require_apply_promotion_qualification",
            return_value=_qualification_verdict(qualified_values),
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={
                "parameter_sets": [
                    {
                        **_parameter_set(values=registry_values),
                        "status": "candidate",
                    }
                ]
            },
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_identity_1",
            dry_run=True,
        )

    assert result["ok"] is False
    assert result["code"] == "parameter_set_evidence_fingerprint_mismatch"


@pytest.mark.parametrize(
    ("locked_values", "locked_status", "locked_rec_changes", "expected_code"),
    [
        (
            {"entry_threshold": 9.99},
            "candidate",
            {},
            "parameter_set_evidence_fingerprint_mismatch",
        ),
        (
            {"entry_threshold": 0.35},
            "deprecated",
            {},
            "parameter_set_state_changed",
        ),
        (
            {"entry_threshold": 0.35},
            "candidate",
            {"reason": "drifted after authorization"},
            "recommendation_state_changed",
        ),
    ],
)
def test_apply_rechecks_locked_parameter_identity_before_capital_writes(
    locked_values: dict[str, object],
    locked_status: str,
    locked_rec_changes: dict[str, object],
    expected_code: str,
) -> None:
    rec = _recommendation()
    qualified_values = {"entry_threshold": 0.35}

    @contextmanager
    def _session():
        yield object()

    capital_write = MagicMock()
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=True),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard."
            "require_apply_promotion_qualification",
            return_value=_qualification_verdict(qualified_values),
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={
                "parameter_sets": [
                    _parameter_set(values=qualified_values)
                ]
            },
        ),
        patch("aats.data_platform.db.get_session", side_effect=_session),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_pending_rollback_release_id",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_known_bad_release_id_for_parameter_set",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_recommendation_for_update",
            return_value={**rec, **locked_rec_changes},
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_parameter_set_for_update",
            return_value={
                **_parameter_set(values=locked_values),
                "status": locked_status,
            },
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
            capital_write,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_identity_1",
        )

    assert result["ok"] is False
    assert result["code"] == expected_code
    capital_write.assert_not_called()


def test_apply_revalidates_qualification_after_locks_before_capital_writes() -> None:
    from aats.data_platform.decision_system.promotion_guard import (
        PromotionQualificationBlockedError,
    )

    rec = _recommendation()
    values = {"entry_threshold": 0.35}

    class _ApplyReadSession:
        def execute(
            self,
            _statement: object,
            _params: dict[str, object],
        ) -> _Result:
            return _Result(None)

    @contextmanager
    def _session():
        yield _ApplyReadSession()

    blocked_verdict = SimpleNamespace(
        reason_code="promotion_authorization_expired",
        detail="authorization expired while waiting for lock",
        to_dict=lambda: {
            "required": True,
            "eligible": False,
            "reason_code": "promotion_authorization_expired",
            "detail": "authorization expired while waiting for lock",
        },
    )
    qualification = MagicMock(
        side_effect=[
            _qualification_verdict(values),
            PromotionQualificationBlockedError("rec_identity_1", blocked_verdict),
        ]
    )
    active_write = MagicMock()
    history_write = MagicMock()

    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=True),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "has_explicit_governance_db_configuration",
            return_value=False,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [rec]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.decision_system.promotion_guard."
            "require_apply_promotion_qualification",
            qualification,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={"parameter_sets": [_parameter_set(values=values)]},
        ),
        patch("aats.data_platform.db.get_session", side_effect=_session),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_pending_rollback_release_id",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_known_bad_release_id_for_parameter_set",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_recommendation_for_update",
            return_value=rec,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_parameter_set_for_update",
            return_value=_parameter_set(values=values),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
            active_write,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history",
            history_write,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_identity_1",
        )

    assert result["ok"] is False
    assert result["code"] == "promotion_qualification_changed_at_lock_in"
    assert qualification.call_count == 2
    active_write.assert_not_called()
    history_write.assert_not_called()
