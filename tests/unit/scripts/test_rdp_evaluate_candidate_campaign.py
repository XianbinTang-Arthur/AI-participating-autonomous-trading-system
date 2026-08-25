from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.rdp_evaluate_candidate_campaign import (
    _planned_hypothesis_fingerprint,
    evaluate_campaign,
    write_campaign,
)
from scripts.rdp_run_candidate_v2_batch import experiment_id_for_plan


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"candidate_campaign_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _series_fingerprint(values: list[float]) -> str:
    encoded = json.dumps(values, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _make_plan(
    artifact_root: Path,
    *,
    suffix: str,
    source_experiment_id: str,
    factor_expression: str,
) -> dict:
    source = artifact_root / "experiments" / source_experiment_id
    candidate = source / "candidate_artifact.json"
    spec = source / "experiment_spec.json"
    _write_json(candidate, {"source": source_experiment_id})
    _write_json(spec, {"source": source_experiment_id})
    plan = {
        "plan_id": f"v2replay_{suffix}",
        "selection_protocol_version": "train_valid_selection_test_holdout_v2",
        "status": "planned_not_run",
        "source_experiment_id": source_experiment_id,
        "source_candidate_ref": candidate.relative_to(artifact_root).as_posix(),
        "source_experiment_spec_ref": spec.relative_to(artifact_root).as_posix(),
        "source_candidate_sha256": _sha256(candidate),
        "source_experiment_spec_sha256": _sha256(spec),
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1h",
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-04-01T00:00:00+00:00",
        "factor_expression": factor_expression,
        "label_horizon_bars": 1,
        "dataset_version": "v1.0",
        "train_ratio": 0.6,
        "valid_ratio": 0.2,
        "test_ratio": 0.2,
        "fee_bps": 1.0,
        "slippage_bps": 1.0,
        "research_profile": "real_factor_evidence_complete",
    }
    _write_json(artifact_root / "replay_plans" / f"{suffix}.json", plan)
    return plan


def _make_experiment(experiment_root: Path, plan: dict, *, returns: list[float]) -> None:
    experiment_id = experiment_id_for_plan(plan, phase="development")
    candidate_id = f"cand_{experiment_id}"
    experiment = experiment_root / experiment_id
    dataset_fingerprint = "rfds_" + "a" * 64
    _write_json(experiment / "experiment_manifest.json", {"status": "succeeded"})
    _write_json(
        experiment / "candidate_artifact.json",
        {
            "candidate_id": candidate_id,
            "experiment_id": experiment_id,
            "payload": {
                "selection_protocol_version": "train_valid_selection_test_holdout_v2",
                "dataset_fingerprint": dataset_fingerprint,
                "benchmark_segment": "valid",
                "holdout_status": "sealed_not_evaluated",
            },
        },
    )
    _write_json(
        experiment / "development_return_series.json",
        {
            "schema_version": "research_development_return_series_v1",
            "selection_protocol_version": "train_valid_selection_test_holdout_v2",
            "experiment_id": experiment_id,
            "dataset_fingerprint": dataset_fingerprint,
            "benchmark_segment": "valid",
            "segments": {
                "train": {
                    "role": "development_stability",
                    "row_count": len(returns),
                    "sample_count": len(returns),
                    "net_returns": returns,
                    "series_fingerprint": _series_fingerprint(returns),
                },
                "valid": {
                    "role": "candidate_selection",
                    "row_count": len(returns),
                    "sample_count": len(returns),
                    "net_returns": returns,
                    "series_fingerprint": _series_fingerprint(returns),
                },
            },
            "cost_assumptions": {
                "fee_bps": 1.0,
                "slippage_bps": 1.0,
                "funding_bps": 0.0,
                "periods_per_year": 8760.0,
            },
            "holdout": {
                "segment": "test",
                "status": "sealed_not_evaluated",
                "content_fingerprint": "segment_" + "b" * 64,
                "values_exposed": False,
            },
        },
    )


def _campaign_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    artifact_root = tmp_path / "research_factory"
    experiment_root = artifact_root / "v2_experiments"
    returns = [0.003 + ((index % 5) - 2) * 0.0001 for index in range(120)]
    plan_a = _make_plan(
        artifact_root,
        suffix="a",
        source_experiment_id="source_a",
        factor_expression="Return(close, 1)",
    )
    plan_b = _make_plan(
        artifact_root,
        suffix="b",
        source_experiment_id="source_b",
        factor_expression="Return(close, 1)",
    )
    plan_c = _make_plan(
        artifact_root,
        suffix="c",
        source_experiment_id="source_c",
        factor_expression="Std(Return(close, 1), 24)",
    )
    _make_plan(
        artifact_root,
        suffix="missing",
        source_experiment_id="source_missing",
        factor_expression="Mean(funding_rate, 8)",
    )
    for plan in (plan_a, plan_b, plan_c):
        _make_experiment(experiment_root, plan, returns=returns)
    return artifact_root, artifact_root / "replay_plans", experiment_root


def test_campaign_counts_all_trials_and_collapses_duplicate_hypotheses(
    tmp_path: Path,
) -> None:
    artifact_root, plan_root, experiment_root = _campaign_fixture(tmp_path)

    summary, evidence = evaluate_campaign(
        plan_root=plan_root,
        artifact_root=artifact_root,
        experiment_root=experiment_root,
        replications=200,
        seed=7,
    )

    assert summary["trial_count"] == 4
    assert summary["return_series_available_count"] == 3
    assert summary["unique_hypothesis_count"] == 2
    assert summary["planned_unique_hypothesis_count"] == 3
    assert summary["duplicate_trial_count"] == 1
    assert summary["planned_duplicate_trial_count"] == 1
    assert len(summary["raw_p_values"]) == 4
    assert 1.0 in summary["raw_p_values"].values()
    assert len(evidence) == 2
    assert all(item["trial_count"] == 4 for item in evidence.values())
    assert all(item["capital_eligible"] is False for item in evidence.values())
    statuses = [entry["status"] for entry in summary["entries"]]
    assert statuses.count("duplicate_hypothesis") == 1
    assert statuses.count("experiment_unavailable") == 1


def test_campaign_rejects_tampered_return_series(tmp_path: Path) -> None:
    artifact_root, plan_root, experiment_root = _campaign_fixture(tmp_path)
    return_path = next(experiment_root.glob("*/development_return_series.json"))
    payload = json.loads(return_path.read_text(encoding="utf-8"))
    payload["segments"]["valid"]["net_returns"][0] = 0.9
    _write_json(return_path, payload)

    with pytest.raises(ValueError, match="fingerprint_mismatch"):
        evaluate_campaign(
            plan_root=plan_root,
            artifact_root=artifact_root,
            experiment_root=experiment_root,
            replications=200,
        )


def test_campaign_outputs_are_immutable(tmp_path: Path) -> None:
    artifact_root, plan_root, experiment_root = _campaign_fixture(tmp_path)
    summary, evidence = evaluate_campaign(
        plan_root=plan_root,
        artifact_root=artifact_root,
        experiment_root=experiment_root,
        replications=200,
    )
    output_root = tmp_path / "campaign"

    digest = write_campaign(summary, evidence, output_root=output_root)

    assert len(digest) == 64
    assert (output_root / "campaign_evidence.json").is_file()
    with pytest.raises(FileExistsError, match="campaign_output_exists"):
        write_campaign(summary, evidence, output_root=output_root)


def test_planned_hypothesis_fingerprint_binds_funding_cost() -> None:
    plan = {
        "factor_expression": "Return(close, 1)",
        "dataset_version": "v1.0",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1h",
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-04-01T00:00:00+00:00",
        "label_horizon_bars": 1,
        "train_ratio": 0.6,
        "valid_ratio": 0.2,
        "test_ratio": 0.2,
        "fee_bps": 5.0,
        "slippage_bps": 2.0,
        "funding_bps": 0.5,
    }
    changed = dict(plan, funding_bps=-0.5)

    assert _planned_hypothesis_fingerprint(plan) != _planned_hypothesis_fingerprint(
        changed
    )
