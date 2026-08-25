from __future__ import annotations

import json
import shutil
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from aats.data_platform.research_factory.preregistered_campaign import (
    PREREGISTERED_CAMPAIGN_SCHEMA,
    load_preregistered_campaign,
    register_preregistered_campaign,
)
from scripts.rdp_run_candidate_v2_batch import load_and_validate_plan


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"preregistered_campaign_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, onexc=_remove_readonly)


def _remove_readonly(function: object, path: str, _error: object) -> None:
    Path(path).chmod(stat.S_IRWXU)
    if callable(function):
        function(path)


def _hypothesis(identifier: str, expression: str) -> dict[str, object]:
    return {
        "hypothesis_id": identifier,
        "mechanism": f"mechanism {identifier}",
        "hypothesis": f"Hypothesis {identifier} predicts positive next-hour returns.",
        "rationale": f"Rationale {identifier} is fixed before development results.",
        "falsification_condition": "Reject when either development segment or campaign gate fails.",
        "capacity_assumption": "No capacity claim before L2 evidence.",
        "holding_period_bars": 1,
        "factor_expression": expression,
    }


def _config() -> dict[str, object]:
    return {
        "schema_version": PREREGISTERED_CAMPAIGN_SCHEMA,
        "campaign_id": "campaign_test_001",
        "registered_at": "2026-08-25T19:00:00+00:00",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1h",
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-04-01T00:00:00+00:00",
        "dataset_version": "v1.0",
        "research_profile": "real_factor_research",
        "train_ratio": 0.6,
        "valid_ratio": 0.2,
        "test_ratio": 0.2,
        "fee_bps": 5.0,
        "slippage_bps": 2.0,
        "funding_bps": 0.5,
        "hypotheses": [
            _hypothesis("reversal", "-ZScore(Return(close, 4), 24)"),
            _hypothesis("momentum", "Return(close, 8) * Rank(volume, 24)"),
            _hypothesis("funding", "-ZScore(funding_rate, 24)"),
        ],
    }


def _write_config(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_registration_is_deterministic_idempotent_and_batch_compatible(
    tmp_path: Path,
) -> None:
    spec = load_preregistered_campaign(_write_config(tmp_path, _config()))
    artifact_root = tmp_path / "artifacts" / "research" / "research_factory"

    first = register_preregistered_campaign(spec, artifact_root=artifact_root)
    second = register_preregistered_campaign(spec, artifact_root=artifact_root)

    assert first == second
    assert first["plan_count"] == 3
    assert first["holdout_accessed"] is False
    plan_paths = sorted(
        (artifact_root / "preregistered_campaigns" / spec.campaign_id / "plans").glob(
            "*.json"
        )
    )
    assert len(plan_paths) == 3
    plans = [
        load_and_validate_plan(path, artifact_root=artifact_root)
        for path in plan_paths
    ]
    assert all(plan["plan_type"] == "preregistered_hypothesis" for plan in plans)
    assert all(plan["funding_bps"] == pytest.approx(0.5) for plan in plans)
    assert all(plan["_proposal_payload"]["hypothesis"] for plan in plans)


def test_registration_rejects_duplicate_factor_signature(tmp_path: Path) -> None:
    payload = _config()
    payload["hypotheses"][1]["factor_expression"] = payload["hypotheses"][0][
        "factor_expression"
    ]

    with pytest.raises(ValueError, match="duplicate_factor_signature"):
        load_preregistered_campaign(_write_config(tmp_path, payload))


def test_preregistered_plan_detects_bound_proposal_tampering(tmp_path: Path) -> None:
    spec = load_preregistered_campaign(_write_config(tmp_path, _config()))
    artifact_root = tmp_path / "artifacts" / "research" / "research_factory"
    register_preregistered_campaign(spec, artifact_root=artifact_root)
    campaign_root = artifact_root / "preregistered_campaigns" / spec.campaign_id
    proposal_path = next((campaign_root / "proposals").glob("*.json"))
    proposal_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    proposal_path.write_text("{}\n", encoding="utf-8")
    plan_path = next(
        path
        for path in (campaign_root / "plans").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["proposal_ref"].endswith(
            proposal_path.name
        )
    )

    with pytest.raises(ValueError, match="proposal_sha256_mismatch"):
        load_and_validate_plan(plan_path, artifact_root=artifact_root)


def test_campaign_config_rejects_unknown_keys(tmp_path: Path) -> None:
    payload = _config()
    payload["look_at_holdout"] = True

    with pytest.raises(ValueError, match="campaign_keys_mismatch"):
        load_preregistered_campaign(_write_config(tmp_path, payload))


def test_campaign_rejects_overlapping_holding_period_until_modeled(
    tmp_path: Path,
) -> None:
    payload = _config()
    payload["hypotheses"][0]["holding_period_bars"] = 4

    with pytest.raises(ValueError, match="holding_period_bars_must_be_one"):
        load_preregistered_campaign(_write_config(tmp_path, payload))


def test_campaign_binds_factor_input_missing_ratio_into_manifest_and_plans(
    tmp_path: Path,
) -> None:
    payload = _config()
    payload["max_factor_input_missing_ratio"] = 0.01
    spec = load_preregistered_campaign(_write_config(tmp_path, payload))
    artifact_root = tmp_path / "artifacts" / "research" / "research_factory"

    register_preregistered_campaign(spec, artifact_root=artifact_root)
    campaign_root = artifact_root / "preregistered_campaigns" / spec.campaign_id
    manifest = json.loads(
        (campaign_root / "campaign_manifest.json").read_text(encoding="utf-8")
    )
    plans = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (campaign_root / "plans").glob("*.json")
    ]

    assert manifest["max_factor_input_missing_ratio"] == pytest.approx(0.01)
    assert all(plan["max_factor_input_missing_ratio"] == pytest.approx(0.01) for plan in plans)
