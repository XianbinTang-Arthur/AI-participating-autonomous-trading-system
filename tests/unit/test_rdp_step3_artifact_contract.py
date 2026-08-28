from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import rdp_run_step3_research as step3


def _calibration_results(status: str = "succeeded") -> list[dict[str, object]]:
    return [
        {"round_key": key, "status": status}
        for key in step3._EXPANDED_ROUND_KEYS
    ]


def _candidate_payload() -> dict[str, object]:
    candidates = {
        key: {"entry_threshold": 0.35}
        for key in step3._EXPECTED_STEP3_COMBO_KEYS
    }
    return {
        "candidates": candidates,
        "pending_validation": [],
    }


def _clean_constraints() -> dict[str, object]:
    return {"all_passed": True, "violations": [], "auto_fixes": []}


def _trusted_step2(status: str = "succeeded") -> dict[str, object]:
    return {"_validated_provenance": {"status": status}}


def test_step3_success_requires_the_complete_trusted_contract() -> None:
    assert step3._determine_step3_round_status(
        calibration_results=_calibration_results(),
        skip_calibration=False,
        skip_merge=False,
        step2_baseline=_trusted_step2(),
        constraint_result=_clean_constraints(),
        candidate_payload=_candidate_payload(),
    ) == "succeeded"


@pytest.mark.parametrize(
    "overrides",
    [
        {"calibration_results": _calibration_results("partial_success")},
        {"skip_calibration": True},
        {"skip_merge": True},
        {"step2_baseline": _trusted_step2("partial_success")},
        {
            "candidate_payload": {
                **_candidate_payload(),
                "pending_validation": ["entry_threshold in independent_15m"],
            }
        },
        {
            "candidate_payload": {
                "candidates": {
                    "independent_15m": {"entry_threshold": 0.35}
                },
                "pending_validation": [],
            }
        },
        {
            "constraint_result": {
                "all_passed": False,
                "violations": [{"rule": "close <= entry"}],
                "auto_fixes": [{"param": "close_threshold"}],
            }
        },
        {
            "calibration_results": [
                *_calibration_results(),
                _calibration_results()[0],
            ]
        },
    ],
)
def test_step3_incomplete_or_untrusted_contract_is_partial(
    overrides: dict[str, object],
) -> None:
    inputs: dict[str, object] = {
        "calibration_results": _calibration_results(),
        "skip_calibration": False,
        "skip_merge": False,
        "step2_baseline": _trusted_step2(),
        "constraint_result": _clean_constraints(),
        "candidate_payload": _candidate_payload(),
    }
    inputs.update(overrides)
    assert step3._determine_step3_round_status(**inputs) == "partial_success"  # type: ignore[arg-type]


def test_step3_all_failed_calibration_is_failed() -> None:
    assert step3._determine_step3_round_status(
        calibration_results=_calibration_results("failed"),
        skip_calibration=False,
        skip_merge=False,
        step2_baseline=_trusted_step2(),
        constraint_result=_clean_constraints(),
        candidate_payload=_candidate_payload(),
    ) == "failed"


def _write_step2_round(
    root: Path,
    *,
    round_id: str = "20260827_100000_1234abcd",
    symbol: str = "BTC-USDT-SWAP",
    dataset_version: str = "v1.0",
    status: str = "succeeded",
) -> Path:
    if root.name != "step2_rounds" or root.parent.name != "research":
        root = root / "artifacts" / "research" / "step2_rounds"
    round_dir = root / round_id
    round_dir.mkdir(parents=True)
    combo_keys = ["directional_15m", "directional_1h", "independent_1h"]
    window = {"start": "2026-08-01", "end": "2026-08-27"}
    candidate = {
        "schema_version": "aats.step2_candidates.v1",
        "round_id": round_id,
        "dataset_version": dataset_version,
        "scope": {
            "symbol": symbol,
            "step": "step2_candidates",
            "combo_keys": combo_keys,
            "combo_count": len(combo_keys),
        },
        "candidates": {
            key: {"signal_edge_scale_bps": 12.0} for key in combo_keys
        },
        "pending_validation": [],
    }
    candidate_bytes = json.dumps(candidate, sort_keys=True).encode("utf-8")
    (round_dir / "parameter_candidates.json").write_bytes(candidate_bytes)
    calibrations = []
    child_index = 1
    for round_key, (family, timeframe, batch_keys) in (
        step3._EXPECTED_STEP2_CALIBRATION_TOPOLOGY.items()
    ):
        batches = []
        for batch_key in batch_keys:
            batch_run_id = f"20260827_100000_{child_index:08x}"
            child_index += 1
            batch_dir = round_dir / "batches" / batch_run_id
            batch_dir.mkdir(parents=True)
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
        calibrations.append({
            "round_key": round_key,
            "family": family,
            "timeframe": timeframe,
            "status": "succeeded",
            "batches": batches,
        })

    project_root = round_dir.parents[3]
    scans = []
    for scan_index, (scan_key, (family, timeframe)) in enumerate(
        step3._EXPECTED_STEP2_SCAN_TOPOLOGY.items(),
        start=1,
    ):
        scan_run_id = f"00000000-0000-4000-8000-{scan_index:012x}"
        scan_dir = (
            project_root / "artifacts" / "research" / "experiments" / scan_run_id
        )
        scan_dir.mkdir(parents=True)
        comparison_bytes = json.dumps(
            {
                "experiment_count": 1,
                "comparison": [{"label": f"{scan_key}_baseline"}],
            },
            sort_keys=True,
        ).encode("utf-8")
        (scan_dir / "comparison_summary.json").write_bytes(comparison_bytes)
        scans.append({
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

    manifest = {
        "schema_version": "aats.step2_round.v1",
        "phase": "step2",
        "round_id": round_id,
        "symbol": symbol,
        "status": status,
        "dataset_version": dataset_version,
        "started_at": "2026-08-27T10:00:00+00:00",
        "finished_at": "2026-08-27T10:01:00+00:00",
        "scope": {
            "symbol": symbol,
            "families": ["directional", "independent"],
            "timeframes": ["15m", "1h"],
            "combo_keys": combo_keys,
            "combo_count": len(combo_keys),
            "window": window,
        },
        "input_refs": {"dataset_version": dataset_version, "window": window},
        "artifact_sha256": {
            "parameter_candidates.json": hashlib.sha256(candidate_bytes).hexdigest()
        },
        "artifact_size_bytes": {
            "parameter_candidates.json": len(candidate_bytes)
        },
        "calibrations": calibrations,
        "scans": scans,
    }
    (round_dir / "round_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return round_dir


def test_step2_baseline_loader_binds_symbol_dataset_window_and_digest(
    tmp_path: Path,
) -> None:
    round_dir = _write_step2_round(tmp_path)
    loaded = step3._load_step2_baseline(
        round_dir,
        expected_symbol="BTC-USDT-SWAP",
        expected_dataset_version="v1.0",
        expected_window={"start": "2026-08-01", "end": "2026-08-27"},
    )
    provenance = loaded["_validated_provenance"]
    assert provenance["round_id"] == round_dir.name
    assert provenance["status"] == "succeeded"
    assert provenance["candidate_sha256"]

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(round_dir, expected_symbol="ETH-USDT-SWAP")
    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(
            round_dir,
            expected_dataset_version="v2.0",
        )


def test_explicit_missing_step2_round_never_falls_back(tmp_path: Path) -> None:
    root = tmp_path / "step2_rounds"
    _write_step2_round(root)
    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(
            root / "20260827_110000_deadbeef",
            step2_artifact_root=root,
        )


def test_step2_baseline_loader_rejects_duplicate_result_identity(
    tmp_path: Path,
) -> None:
    round_dir = _write_step2_round(tmp_path)
    manifest_path = round_dir / "round_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["calibrations"].append(dict(manifest["calibrations"][0]))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(round_dir)


@pytest.mark.parametrize("pending_validation", [None, {}, [123], [""], [" duplicate", " duplicate"]])
def test_step2_baseline_loader_rejects_invalid_pending_contract(
    tmp_path: Path,
    pending_validation: object,
) -> None:
    round_dir = _write_step2_round(tmp_path)
    candidate_path = round_dir / "parameter_candidates.json"
    manifest_path = round_dir / "round_manifest.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["pending_validation"] = pending_validation
    candidate_bytes = json.dumps(candidate, sort_keys=True).encode("utf-8")
    candidate_path.write_bytes(candidate_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"][candidate_path.name] = hashlib.sha256(
        candidate_bytes
    ).hexdigest()
    manifest["artifact_size_bytes"][candidate_path.name] = len(candidate_bytes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(round_dir)


def test_step2_auto_selection_rejects_any_nonstandard_round_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "step2_rounds"
    _write_step2_round(root)
    (root / "scratch").mkdir()

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(step2_artifact_root=root)


def test_step2_baseline_loader_rejects_batch_summary_digest_drift(
    tmp_path: Path,
) -> None:
    round_dir = _write_step2_round(tmp_path)
    manifest = json.loads(
        (round_dir / "round_manifest.json").read_text(encoding="utf-8")
    )
    batch_dir = Path(manifest["calibrations"][0]["batches"][0]["batch_dir"])
    (batch_dir / "batch_summary.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(round_dir)


def test_step2_baseline_loader_rejects_scan_comparison_digest_drift(
    tmp_path: Path,
) -> None:
    round_dir = _write_step2_round(tmp_path)
    manifest = json.loads(
        (round_dir / "round_manifest.json").read_text(encoding="utf-8")
    )
    scan_dir = Path(manifest["scans"][0]["scan_dir"])
    (scan_dir / "comparison_summary.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(round_dir)


@pytest.mark.parametrize(
    "invalid_case",
    [
        "experiments_type",
        "experiment_count",
        "experiment_identity",
        "experiment_params",
        "failures_type",
        "sidecar_count_type",
    ],
)
def test_step2_baseline_loader_rejects_semantically_invalid_batch_summary(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    round_dir = _write_step2_round(tmp_path)
    manifest_path = round_dir / "round_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch = manifest["calibrations"][0]["batches"][0]
    summary_path = Path(batch["batch_dir"]) / "batch_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if invalid_case == "experiments_type":
        summary["experiments"] = {}
    elif invalid_case == "experiment_count":
        summary["succeeded"] = 2
    elif invalid_case == "experiment_identity":
        summary["experiments"][0]["experiment_id"] = "not-a-uuid"
    elif invalid_case == "experiment_params":
        summary["experiments"][0]["params"] = []
    elif invalid_case == "failures_type":
        summary["failures"] = {}
    else:
        batch["total_experiments"] = True
    summary_bytes = json.dumps(summary, sort_keys=True).encode("utf-8")
    summary_path.write_bytes(summary_bytes)
    batch["summary_sha256"] = hashlib.sha256(summary_bytes).hexdigest()
    batch["summary_size_bytes"] = len(summary_bytes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(round_dir)


@pytest.mark.parametrize(
    "invalid_case",
    [
        "legacy_schema",
        "experiment_count",
        "comparison_item",
        "duplicate_identity",
        "sidecar_count",
        "sidecar_count_type",
    ],
)
def test_step2_baseline_loader_rejects_semantically_invalid_scan_comparison(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    round_dir = _write_step2_round(tmp_path)
    manifest_path = round_dir / "round_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scan = manifest["scans"][0]
    comparison_path = Path(scan["scan_dir"]) / "comparison_summary.json"
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if invalid_case == "legacy_schema":
        comparison = {"rows": comparison["comparison"]}
    elif invalid_case == "experiment_count":
        comparison["experiment_count"] = 2
    elif invalid_case == "comparison_item":
        comparison["comparison"] = ["not-an-object"]
    elif invalid_case == "duplicate_identity":
        comparison["experiment_count"] = 2
        comparison["comparison"] = comparison["comparison"] * 2
        scan["total_combinations"] = 2
        scan["completed_count"] = 2
    elif invalid_case == "sidecar_count":
        scan["total_combinations"] = 2
        scan["completed_count"] = 2
    else:
        scan["total_combinations"] = True
        scan["completed_count"] = True
    comparison_bytes = json.dumps(comparison, sort_keys=True).encode("utf-8")
    comparison_path.write_bytes(comparison_bytes)
    scan["comparison_sha256"] = hashlib.sha256(comparison_bytes).hexdigest()
    scan["comparison_size_bytes"] = len(comparison_bytes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(round_dir)


def test_step2_auto_selection_rejects_calendar_invalid_round_id(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts" / "research" / "step2_rounds"
    _write_step2_round(root)
    (root / "20260230_120000_deadbeef").mkdir(parents=True)

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(step2_artifact_root=root)


def test_step2_baseline_loader_rejects_candidate_symlink(tmp_path: Path) -> None:
    round_dir = _write_step2_round(tmp_path)
    candidate_path = round_dir / "parameter_candidates.json"
    target_path = round_dir.parent / "redirected_candidates.json"
    target_path.write_bytes(candidate_path.read_bytes())
    candidate_path.unlink()
    try:
        candidate_path.symlink_to(target_path)
    except OSError:
        pytest.skip("Windows host does not permit symlink creation")

    with pytest.raises(ValueError, match="step2_baseline_contract_invalid"):
        step3._load_step2_baseline(round_dir)


def test_step2_pending_confidence_is_not_promoted_during_merge() -> None:
    merged = step3._merge_recommendations(
        {
            "candidates": {
                "independent_15m": {"signal_edge_scale_bps": 12.0}
            },
            "pending_validation": [
                "signal_edge_scale_bps in independent_15m"
            ],
        },
        {},
    )
    record = merged["independent_15m"]["signal_edge_scale_bps"]
    assert record["confidence"] == "low"
    assert "待验证" in record["reason"]
