from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aats.data_platform.research_factory.validation.candidate_replay import (
    audit_historical_candidate,
    build_candidate_v2_replay_plan,
)
from aats.data_platform.research_factory.validation.capital_eligibility import (
    CURRENT_SELECTION_PROTOCOL,
)


@pytest.fixture
def candidate_fixture() -> Iterator[tuple[Path, Path, Path]]:
    root = Path(".pytest_workspace_tmp") / f"candidate_replay_{uuid.uuid4().hex}"
    experiment = root / "research_factory" / "experiments" / "exp_legacy"
    experiment.mkdir(parents=True)
    candidate_path = experiment / "candidate_artifact.json"
    spec_path = experiment / "experiment_spec.json"
    candidate_path.write_text(
        json.dumps(
            {
                "candidate_id": "cand_legacy",
                "experiment_id": "exp_legacy",
                "payload": {
                    "benchmark_segment": "test",
                    "factor_expression": "Return(close, 1)",
                    "dataset_fingerprint": "rfds_" + "a" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    spec_path.write_text(
        json.dumps(
            {
                "dataset": {
                    "symbol": "BTC-USDT-SWAP",
                    "timeframe": "1h",
                    "window_start": "2026-01-01T00:00:00+00:00",
                    "window_end": "2026-04-01T00:00:00+00:00",
                    "dataset_version": "v1.0",
                },
                "features": [{"expression": "Return(close, 1)"}],
                "label": {"horizon_bars": 1, "fee_bps": 5.0, "slippage_bps": 2.0},
            }
        ),
        encoding="utf-8",
    )
    try:
        yield root / "research_factory", candidate_path, spec_path
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_historical_candidate_is_invalidated_and_gets_deterministic_v2_plan(
    candidate_fixture: tuple[Path, Path, Path],
) -> None:
    artifact_root, candidate_path, spec_path = candidate_fixture
    timestamp = datetime(2026, 8, 25, tzinfo=UTC)
    audit = audit_historical_candidate(
        candidate_path,
        artifact_root=artifact_root,
        evaluated_at=timestamp,
    )
    assert audit.capital_eligible is False
    assert "legacy_test_used_for_selection" in audit.reason_codes

    plan = build_candidate_v2_replay_plan(
        audit=audit,
        candidate_path=candidate_path,
        experiment_spec_path=spec_path,
        artifact_root=artifact_root,
        created_at=timestamp,
    )
    duplicate = build_candidate_v2_replay_plan(
        audit=audit,
        candidate_path=candidate_path,
        experiment_spec_path=spec_path,
        artifact_root=artifact_root,
        created_at=timestamp,
    )
    assert plan.plan_id == duplicate.plan_id
    assert plan.selection_protocol_version == CURRENT_SELECTION_PROTOCOL
    assert plan.status == "planned_not_run"
    assert plan.train_ratio == pytest.approx(0.6)
    assert plan.valid_ratio == pytest.approx(0.2)
    assert plan.test_ratio == pytest.approx(0.2)


def test_replay_plan_rejects_candidate_spec_factor_mismatch(
    candidate_fixture: tuple[Path, Path, Path],
) -> None:
    artifact_root, candidate_path, spec_path = candidate_fixture
    audit = audit_historical_candidate(candidate_path, artifact_root=artifact_root)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["features"][0]["expression"] = "Mean(close, 2)"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate_factor_expression_mismatch"):
        build_candidate_v2_replay_plan(
            audit=audit,
            candidate_path=candidate_path,
            experiment_spec_path=spec_path,
            artifact_root=artifact_root,
        )
