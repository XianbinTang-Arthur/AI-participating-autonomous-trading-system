from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import rdp_run_step2_research as step2


def _calibrations(status: str = "succeeded") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    child_index = 1
    for key in step2._EXPECTED_STEP2_CALIBRATION_KEYS:
        family, timeframe, batch_keys = step2._EXPECTED_STEP2_CALIBRATION_TOPOLOGY[key]
        batches = []
        for batch_key in batch_keys:
            batches.append({
                "_key": batch_key,
                "status": status,
                "batch_run_id": f"20260827_100000_{child_index:08x}",
                "batch_dir": f"/formal/batches/{child_index}",
                "summary_sha256": f"{child_index:064x}",
                "summary_size_bytes": 1,
                "total_experiments": 1,
                "succeeded": 1 if status == "succeeded" else 0,
                "failed": 0 if status == "succeeded" else 1,
            })
            child_index += 1
        rows.append({
            "round_key": key,
            "family": family,
            "timeframe": timeframe,
            "status": status,
            "batch_results": batches,
        })
    return rows


def _scans(status: str = "succeeded") -> list[dict[str, object]]:
    return [
        {
            "scan_key": key,
            "family": step2._EXPECTED_STEP2_SCAN_TOPOLOGY[key][0],
            "timeframe": step2._EXPECTED_STEP2_SCAN_TOPOLOGY[key][1],
            "status": status,
            "scan_run_id": f"00000000-0000-4000-8000-{index:012x}",
            "scan_dir": f"/formal/scans/{index}",
            "comparison_sha256": f"{index:064x}",
            "comparison_size_bytes": 1,
            "window": {"start": "2026-08-01", "end": "2026-08-27"},
            "dataset_version": "v1.0",
            "grid_sha256": f"{index + 16:064x}",
            "total_combinations": 1,
            "completed_count": 1 if status == "succeeded" else 0,
            "failed_count": 0 if status == "succeeded" else 1,
        }
        for index, key in enumerate(step2._EXPECTED_STEP2_SCAN_KEYS, start=1)
    ]


def _candidates() -> dict[str, object]:
    return {
        "candidates": {
            key: {"signal_edge_scale_bps": 12.0}
            for key in step2._EXPECTED_STEP2_COMBO_KEYS
        },
        "pending_validation": [],
    }


def _status(
    *,
    calibrations: list[dict[str, object]] | None = None,
    scans: list[dict[str, object]] | None = None,
    candidates: dict[str, object] | None = None,
    start: str | None = "2026-08-01",
    end: str | None = "2026-08-27",
) -> str:
    return step2._determine_step2_round_status(
        calibration_results=calibrations if calibrations is not None else _calibrations(),
        scan_results=scans if scans is not None else _scans(),
        parameter_candidates_payload=candidates if candidates is not None else _candidates(),
        start=start,
        end=end,
    )


def test_step2_success_requires_exact_unique_evidence_topology() -> None:
    assert _status() == "succeeded"


@pytest.mark.parametrize(
    ("calibrations", "scans"),
    [
        ([*_calibrations(), _calibrations()[0]], _scans()),
        (
            [
                *_calibrations()[:-1],
                {"round_key": "unknown_1h", "status": "succeeded"},
            ],
            _scans(),
        ),
        (
            [*_calibrations()[:-1], dict(_calibrations()[0])],
            _scans(),
        ),
        (_calibrations(), [*_scans(), _scans()[0]]),
        (_calibrations(), _scans()[:-1]),
    ],
)
def test_step2_duplicate_unknown_or_missing_result_identity_is_partial(
    calibrations: list[dict[str, object]],
    scans: list[dict[str, object]],
) -> None:
    assert _status(calibrations=calibrations, scans=scans) == "partial_success"


def test_step2_pending_or_missing_window_is_partial() -> None:
    pending = _candidates()
    pending["pending_validation"] = ["signal_edge_scale_bps in independent_1h"]
    assert _status(candidates=pending) == "partial_success"
    assert _status(start=None) == "partial_success"


@pytest.mark.parametrize(
    "pending_validation",
    [None, {}, [123], [""], [" valid-but-still-pending "]],
)
def test_step2_pending_contract_must_be_an_explicit_empty_list(
    pending_validation: object,
) -> None:
    candidates = _candidates()
    candidates["pending_validation"] = pending_validation
    assert _status(candidates=candidates) == "partial_success"


def test_step2_all_executed_results_failed_is_failed() -> None:
    assert _status(
        calibrations=_calibrations("failed"),
        scans=_scans("failed"),
    ) == "failed"


def test_step2_manifest_preserves_child_sidecar_counts(tmp_path: Path) -> None:
    manifest_path = tmp_path / "round_manifest.json"

    step2._write_manifest(
        _calibrations(),
        _scans(),
        "20260828_100000_1234abcd",
        "2026-08-28T10:00:00+00:00",
        "2026-08-28T10:05:00+00:00",
        manifest_path,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_batch = manifest["calibrations"][0]["batches"][0]
    assert (
        first_batch["total_experiments"],
        first_batch["succeeded"],
        first_batch["failed"],
    ) == (1, 1, 0)
    first_scan = manifest["scans"][0]
    assert (
        first_scan["total_combinations"],
        first_scan["completed_count"],
        first_scan["failed_count"],
    ) == (1, 1, 0)


@pytest.mark.parametrize(
    "failure_item",
    [
        "not-an-object",
        {"label": "failed", "params": []},
        {"label": "failed"},
    ],
)
def test_calibration_summary_rejects_invalid_failure_identity(
    failure_item: object,
) -> None:
    summary = {
        "total_experiments": 2,
        "succeeded": 1,
        "failed": 1,
        "experiments": [
            {
                "label": "succeeded",
                "experiment_id": "00000000-0000-4000-8000-000000000001",
                "status": "succeeded",
                "params": {"min_confirm_ticks": 2},
            }
        ],
        "failures": [failure_item],
    }

    with pytest.raises(ValueError, match="research_calibration_failure_invalid"):
        step2.validate_calibration_batch_summary(
            summary,
            expected_counts=(2, 1, 1),
            expected_status="partial_success",
        )


def _write_batch_child_result(
    cmd: list[str],
    *,
    invalid_summary: bool = False,
) -> SimpleNamespace:
    artifact_root = Path(cmd[cmd.index("--artifact-root") + 1]).resolve()
    result_path = Path(cmd[cmd.index("--result-json") + 1])
    batch_run_id = "20260828_100000_1234abcd"
    batch_dir = artifact_root / batch_run_id
    batch_dir.mkdir(parents=True)
    summary = {
        "batch_run_id": batch_run_id,
        "batch_name": "independent_test_1h",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1H",
        "dataset_version": "v1.0",
        "window": "2026-08-01 ~ 2026-08-27",
        "total_experiments": 1,
        "succeeded": 1,
        "failed": 0,
        "experiments": [
            {
                "label": "baseline",
                "experiment_id": "00000000-0000-4000-8000-000000000001",
                "status": "succeeded",
                "params": {"min_confirm_ticks": 2},
            }
        ],
        "failures": [],
    }
    if invalid_summary:
        summary["experiments"][0]["params"] = {"min_confirm_ticks": 99}
    summary_path = batch_dir / "batch_summary.json"
    summary_bytes = json.dumps(summary, sort_keys=True).encode("utf-8")
    summary_path.write_bytes(summary_bytes)
    result_payload = {
        "schema_version": "aats.calibration_batch_result.v1",
        "batch_run_id": batch_run_id,
        "batch_dir": str(batch_dir),
        "summary_path": str(summary_path),
        "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        "summary_size_bytes": len(summary_bytes),
        "status": "succeeded",
        "batch_name": "independent_test_1h",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1H",
        "dataset_version": "v1.0",
        "window": {"start": "2026-08-01", "end": "2026-08-27"},
        "total_experiments": 1,
        "succeeded": 1,
        "failed": 0,
    }
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps(result_payload), encoding="utf-8")
    return SimpleNamespace(returncode=0)


def test_step2_batch_producer_accepts_complete_semantic_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(
        json.dumps(
            {
                "batch_name": "independent_test_1h",
                "family": "independent",
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "1H",
                "dataset_version": "v1.0",
                "start": "2026-08-01",
                "end": "2026-08-27",
                "experiments": [
                    {
                        "label": "baseline",
                        "params": {"min_confirm_ticks": 2},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        step2.subprocess,
        "run",
        lambda cmd: _write_batch_child_result(cmd),
    )

    result = step2._run_batch(
        str(spec_path),
        tmp_path / "round" / "batches",
        start="2026-08-01",
        end="2026-08-27",
        dataset_version="v1.0",
        expected_family="independent",
        expected_timeframe="1H",
    )

    assert result["status"] == "succeeded"
    assert result["total_experiments"] == result["succeeded"] == 1
    assert result["failed"] == 0


def test_step2_batch_producer_rejects_invalid_parameter_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(
        json.dumps(
            {
                "batch_name": "independent_test_1h",
                "family": "independent",
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "1H",
                "dataset_version": "v1.0",
                "start": "2026-08-01",
                "end": "2026-08-27",
                "experiments": [
                    {
                        "label": "baseline",
                        "params": {"min_confirm_ticks": 2},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        step2.subprocess,
        "run",
        lambda cmd: _write_batch_child_result(cmd, invalid_summary=True),
    )

    result = step2._run_batch(
        str(spec_path),
        tmp_path / "round" / "batches",
        start="2026-08-01",
        end="2026-08-27",
        dataset_version="v1.0",
        expected_family="independent",
        expected_timeframe="1H",
    )

    assert result["status"] == "failed"
    assert result["error"] == "calibration_batch_summary_semantics_invalid"


def _write_scan_child_result(
    cmd: list[str],
    *,
    legacy_comparison: bool = False,
) -> SimpleNamespace:
    result_path = Path(cmd[cmd.index("--result-json") + 1])
    scan_run_id = "00000000-0000-4000-8000-000000000001"
    scan_dir = Path("artifacts/research/experiments") / scan_run_id
    scan_dir.mkdir(parents=True)
    if legacy_comparison:
        comparison = {"rows": [{"label": "legacy"}]}
    else:
        comparison = {
            "experiment_count": 1,
            "comparison": [{"label": "min_confirm_ticks=2"}],
        }
    comparison_path = scan_dir / "comparison_summary.json"
    comparison_bytes = json.dumps(comparison, sort_keys=True).encode("utf-8")
    comparison_path.write_bytes(comparison_bytes)
    grid = {"min_confirm_ticks": [2]}
    canonical_grid = json.dumps(
        grid,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    result_payload = {
        "schema_version": "aats.parameter_scan_result.v1",
        "scan_run_id": scan_run_id,
        "scan_dir": str(scan_dir.resolve()),
        "comparison_path": str(comparison_path.resolve()),
        "comparison_sha256": hashlib.sha256(comparison_bytes).hexdigest(),
        "comparison_size_bytes": len(comparison_bytes),
        "status": "succeeded",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "dataset_version": "v1.0",
        "window": {"start": "2026-08-01", "end": "2026-08-27"},
        "grid_sha256": hashlib.sha256(canonical_grid).hexdigest(),
        "total_combinations": 1,
        "completed_count": 1,
        "failed_count": 0,
    }
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps(result_payload), encoding="utf-8")
    return SimpleNamespace(returncode=0)


@pytest.mark.parametrize("legacy_comparison", [False, True])
def test_step2_scan_producer_requires_canonical_count_bound_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_comparison: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        step2.subprocess,
        "run",
        lambda cmd: _write_scan_child_result(
            cmd,
            legacy_comparison=legacy_comparison,
        ),
    )

    result = step2._run_scan(
        "independent_15m",
        {
            "family": "independent",
            "timeframe": "15m",
            "start": "2026-08-01",
            "end": "2026-08-27",
            "dataset_version": "v1.0",
            "grid": {"min_confirm_ticks": [2]},
        },
        result_root=tmp_path / "round" / "scan_results",
    )

    if legacy_comparison:
        assert result["status"] == "failed"
        assert result["error"] == "parameter_scan_artifact_identity_invalid"
    else:
        assert result["status"] == "succeeded"
        assert result["completed_count"] == result["total_combinations"] == 1
        assert result["failed_count"] == 0
