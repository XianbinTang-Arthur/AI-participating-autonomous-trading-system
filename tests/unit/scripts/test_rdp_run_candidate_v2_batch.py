from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.rdp_run_candidate_v2_batch import (
    _validate_execution_summary,
    experiment_id_for_plan,
    load_and_validate_plan,
    main,
)


def _write_plan(tmp_path: Path) -> tuple[Path, Path, dict]:
    root = tmp_path / "research_factory"
    experiment = root / "experiments" / "old"
    experiment.mkdir(parents=True)
    candidate = experiment / "candidate_artifact.json"
    spec = experiment / "experiment_spec.json"
    candidate.write_text("{}\n", encoding="utf-8")
    spec.write_text("{}\n", encoding="utf-8")
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {
        "plan_id": "v2replay_1234567890abcdef",
        "status": "planned_not_run",
        "selection_protocol_version": "train_valid_selection_test_holdout_v2",
        "source_candidate_ref": "experiments/old/candidate_artifact.json",
        "source_experiment_spec_ref": "experiments/old/experiment_spec.json",
        "source_candidate_sha256": sha(candidate),
        "source_experiment_spec_sha256": sha(spec),
        "source_experiment_id": "old",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1h",
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-02-01T00:00:00+00:00",
    }
    plan_path = root / "plans" / "plan.json"
    plan_path.parent.mkdir()
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    return root, plan_path, payload


def test_plan_validation_checks_source_hashes(tmp_path: Path) -> None:
    root, plan_path, payload = _write_plan(tmp_path)
    loaded = load_and_validate_plan(plan_path, artifact_root=root)
    assert loaded["plan_id"] == payload["plan_id"]
    (root / payload["source_candidate_ref"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="candidate_sha256_mismatch"):
        load_and_validate_plan(plan_path, artifact_root=root)


def test_phase_is_part_of_experiment_identity() -> None:
    plan = {"source_experiment_id": "old", "plan_id": "v2replay_abc"}
    assert experiment_id_for_plan(plan, phase="development") == "old_v2_dev_abc"
    assert experiment_id_for_plan(plan, phase="evidence-complete") == "old_v2_evidence_abc"


def test_execution_summary_is_bound_to_exact_plan_and_symbol(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "model_version": "l2_event_replay_v1",
                "plan_id": "v2replay_other",
                "symbol": "BTC-USDT-SWAP",
                "benchmark_segment": "valid",
                "timeframe": "1h",
            }
        ),
        encoding="utf-8",
    )
    plan = {
        "plan_id": "v2replay_expected",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1h",
    }
    with pytest.raises(ValueError, match="plan_id_mismatch"):
        _validate_execution_summary(summary_path, plan=plan)


def test_empty_plan_root_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="v2_replay_plans_required"):
        main(["--plan-root", str(tmp_path), "--dry-run"])
