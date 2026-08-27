from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aats.data_platform.production_workflow.post_apply_evidence import (
    POST_APPLY_EVIDENCE_CONTRACT_VERSION,
    make_source_provenance,
)
from aats.data_platform.production_workflow.release_registry import (
    validate_release_bound_evidence,
)


def _release() -> tuple[dict, datetime]:
    applied_at = datetime.now(timezone.utc) - timedelta(hours=1)
    return (
        {
            "release_id": "rel_provenance_1",
            "family": "independent",
            "timeframe": "15m",
            "applied_at": applied_at.isoformat(),
            "observation_window_hours": 24,
        },
        applied_at,
    )


def _rollback_evidence(*, source: dict | None = None) -> dict:
    evidence = {
        "release_id": "rel_provenance_1",
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "rollback_recommended": True,
        "severity": "high",
        "fired_trigger_count": 1,
        "triggers": [
            {
                "trigger": "attribution_regression",
                "fired": True,
                "severity": "high",
                "evidence_status": "valid",
                "detail": "regression",
            }
        ],
    }
    if source is not None:
        evidence["evidence_contract_version"] = (
            POST_APPLY_EVIDENCE_CONTRACT_VERSION
        )
        evidence["source_provenance"] = [source]
        evidence["triggers"][0]["source_provenance"] = source
    return evidence


def _observation_evidence() -> dict:
    return {
        "release_id": "rel_provenance_1",
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "observation_window_hours": 24,
        "status": "rollback_recommended",
        "recommendation": "rollback_recommended",
        "regression_count": 1,
        "checklist": [
            {
                "name": "quality_monitor",
                "status": "regression",
                "severity": "high",
            }
        ],
    }


def test_legacy_true_rollback_evidence_requires_reconciliation() -> None:
    release, _ = _release()

    error = validate_release_bound_evidence(
        release,
        _rollback_evidence(),
        evidence_kind="rollback_recommendation",
    )

    assert error is not None
    assert error["reason"] == "release_evidence_provenance_invalid"


def test_legacy_risk_observation_requires_reconciliation() -> None:
    release, _ = _release()
    evidence = _observation_evidence()
    evidence["started_at"] = release["applied_at"]

    error = validate_release_bound_evidence(
        release,
        evidence,
        evidence_kind="observation",
    )

    assert error is not None
    assert error["reason"] == "release_evidence_provenance_invalid"


def test_versioned_post_apply_risk_source_is_accepted() -> None:
    release, applied_at = _release()
    source_time = applied_at + timedelta(minutes=10)
    source = make_source_provenance(
        source_kind="research_round",
        source_id="phase3_round_1",
        source_timestamp=source_time,
        source_phase="phase3",
        source_family="independent",
        source_timeframe="15m",
        source_payload={"round_id": "phase3_round_1", "failure_pct": 90.0},
    )

    error = validate_release_bound_evidence(
        release,
        _rollback_evidence(source=source),
        evidence_kind="rollback_recommendation",
    )

    assert error is None


@pytest.mark.parametrize(
    "change",
    [
        {"source_phase": "phase4"},
        {"source_fingerprint": "not-a-sha256"},
    ],
)
def test_wrong_source_contract_details_are_rejected(change: dict) -> None:
    release, applied_at = _release()
    source = make_source_provenance(
        source_kind="research_round",
        source_id="phase3_round_1",
        source_timestamp=applied_at + timedelta(minutes=10),
        source_phase="phase3",
        source_family="independent",
        source_timeframe="15m",
        source_payload={"round_id": "phase3_round_1"},
    )
    source.update(change)

    error = validate_release_bound_evidence(
        release,
        _rollback_evidence(source=source),
        evidence_kind="rollback_recommendation",
    )

    assert error is not None
    assert error["reason"] == "release_evidence_provenance_invalid"


@pytest.mark.parametrize(
    "change",
    [
        {"severity": "none"},
        {"fired_trigger_count": 2},
    ],
)
def test_provenance_shaped_but_semantically_impossible_risk_is_rejected(
    change: dict,
) -> None:
    release, applied_at = _release()
    source = make_source_provenance(
        source_kind="research_round",
        source_id="phase3_round_1",
        source_timestamp=applied_at + timedelta(minutes=10),
        source_phase="phase3",
        source_family="independent",
        source_timeframe="15m",
        source_payload={"round_id": "phase3_round_1"},
    )
    evidence = _rollback_evidence(source=source)
    evidence.update(change)

    error = validate_release_bound_evidence(
        release,
        evidence,
        evidence_kind="rollback_recommendation",
    )

    assert error is not None
    assert error["reason"] == "release_evidence_provenance_invalid"


def test_pre_apply_source_timestamp_is_rejected() -> None:
    release, applied_at = _release()
    source = make_source_provenance(
        source_kind="research_round",
        source_id="phase3_round_old",
        source_timestamp=applied_at - timedelta(seconds=1),
        source_phase="phase3",
        source_family="independent",
        source_timeframe="15m",
        source_payload={"round_id": "phase3_round_old"},
    )

    error = validate_release_bound_evidence(
        release,
        _rollback_evidence(source=source),
        evidence_kind="rollback_recommendation",
    )

    assert error is not None
    assert "source timestamp" in error["error"]


def test_cross_combo_source_cannot_authorize_target_release() -> None:
    release, applied_at = _release()
    source = make_source_provenance(
        source_kind="research_round",
        source_id="phase3_round_other_combo",
        source_timestamp=applied_at + timedelta(minutes=10),
        source_phase="phase3",
        source_family="trend",
        source_timeframe="1h",
        source_payload={"round_id": "phase3_round_other_combo"},
    )

    error = validate_release_bound_evidence(
        release,
        _rollback_evidence(source=source),
        evidence_kind="rollback_recommendation",
    )

    assert error is not None
    assert "source scope" in error["error"]


def test_source_after_evaluation_is_rejected() -> None:
    release, applied_at = _release()
    source = make_source_provenance(
        source_kind="research_round",
        source_id="phase3_round_after_wrapper",
        source_timestamp=datetime.now(timezone.utc) + timedelta(minutes=1),
        source_phase="phase3",
        source_family="independent",
        source_timeframe="15m",
        source_payload={"round_id": "phase3_round_after_wrapper"},
    )
    evidence = _rollback_evidence(source=source)
    evidence["evaluated_at"] = (applied_at + timedelta(minutes=10)).isoformat()

    error = validate_release_bound_evidence(
        release,
        evidence,
        evidence_kind="rollback_recommendation",
    )

    assert error is not None
    assert "source timestamp" in error["error"]


@pytest.mark.parametrize("kind", ["rollback_recommendation", "observation"])
def test_risk_wrapper_without_a_firing_item_is_rejected(kind: str) -> None:
    release, applied_at = _release()
    source = make_source_provenance(
        source_kind="research_round",
        source_id="phase3_round_1",
        source_timestamp=applied_at + timedelta(minutes=10),
        source_phase="phase3",
        source_family="independent",
        source_timeframe="15m",
        source_payload={"round_id": "phase3_round_1"},
    )
    if kind == "rollback_recommendation":
        evidence = _rollback_evidence(source=source)
        evidence["triggers"] = []
    else:
        evidence = _observation_evidence()
        evidence["started_at"] = release["applied_at"]
        evidence["evidence_contract_version"] = (
            POST_APPLY_EVIDENCE_CONTRACT_VERSION
        )
        evidence["source_provenance"] = [source]
        evidence["checklist"] = []

    error = validate_release_bound_evidence(
        release,
        evidence,
        evidence_kind=kind,
    )

    assert error is not None
    assert error["reason"] == "release_evidence_provenance_invalid"
