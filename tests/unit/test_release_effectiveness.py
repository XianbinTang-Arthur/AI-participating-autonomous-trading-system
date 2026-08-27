from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aats.data_platform.metrics.release_effectiveness import (
    evaluate_release_effectiveness,
)
from aats.data_platform.production_workflow.post_apply_evidence import (
    POST_APPLY_EVIDENCE_CONTRACT_VERSION,
    make_source_provenance,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _disable_governance_db() -> ExitStack:
    stack = ExitStack()
    for target in (
        "aats.data_platform.metrics.release_effectiveness.try_governance_db",
        "aats.data_platform.production_workflow.release_registry.try_governance_db",
        "aats.data_platform.production_workflow.observation_window.try_governance_db",
        "aats.data_platform.production_workflow.rollback_policy.try_governance_db",
    ):
        stack.enter_context(patch(target, return_value=(None, False)))
    return stack


def _operations_dimension(evaluation: dict) -> dict:
    return next(
        dimension
        for dimension in evaluation.get("dimensions", [])
        if dimension.get("dimension") == "operations"
    )


def test_rolled_back_release_is_classified_as_rollback_triggered(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/production_workflow/parameter_release_history.json",
        {
            "generated_at": "2026-04-16T10:00:00Z",
            "releases": [
                {
                    "release_id": "rel_rolled_back",
                    "created_at": "2026-04-16T09:00:00Z",
                    "family": "independent",
                    "timeframe": "15m",
                    "combo_key": "independent_15m",
                    "parameter_set_id": "ps_live_1",
                    "previous_parameter_set_id": "ps_live_0",
                    "apply_operation_id": "apply_rel_rolled_back",
                    "applied_at": "2026-04-16T09:05:00Z",
                    "apply_result": "success",
                    "observation_status": "rolled_back",
                },
            ],
        },
    )

    with _disable_governance_db():
        evaluation = evaluate_release_effectiveness(
            tmp_path,
            "rel_rolled_back",
            save_result=False,
        )

    operations = _operations_dimension(evaluation)
    assert operations["score"] == "negative"
    assert "rollback executed" in operations["detail"]
    assert evaluation["conclusion"] == "rollback_triggered"


def test_rollback_recommended_status_without_canonical_artifact_is_unknown(
    tmp_path: Path,
) -> None:
    _write_json(
        tmp_path / "artifacts/production_workflow/parameter_release_history.json",
        {
            "generated_at": "2026-04-16T10:00:00Z",
            "releases": [
                {
                    "release_id": "rel_rb_status_only",
                    "created_at": "2026-04-16T09:00:00Z",
                    "family": "independent",
                    "timeframe": "1h",
                    "combo_key": "independent_1h",
                    "parameter_set_id": "ps_live_2",
                    "previous_parameter_set_id": "ps_live_1",
                    "apply_operation_id": "apply_rel_rb_status_only",
                    "applied_at": "2026-04-16T09:05:00Z",
                    "apply_result": "success",
                    "observation_status": "rollback_recommended",
                },
            ],
        },
    )

    with _disable_governance_db():
        evaluation = evaluate_release_effectiveness(
            tmp_path,
            "rel_rb_status_only",
            save_result=False,
        )

    operations = _operations_dimension(evaluation)
    assert operations["score"] == "unknown"
    assert operations["detail"] == "observation_status=unavailable"
    assert evaluation["conclusion"] != "rollback_triggered"


def _write_release_with_evidence(
    root: Path,
    *,
    release_id: str,
    observation: dict,
    rollback: dict,
) -> None:
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(hours=2)
    applied_at = now - timedelta(hours=1)
    release = {
        "release_id": release_id,
        "created_at": created_at.isoformat(),
        "applied_at": applied_at.isoformat(),
        "apply_operation_id": f"apply_{release_id}",
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
        "parameter_set_id": "ps_live",
        "previous_parameter_set_id": "ps_previous",
        "apply_result": "success",
        "observation_status": "observing",
        "observation_window_hours": 24,
    }
    observation = dict(observation)
    if observation.get("started_at") == "__APPLIED_AT__":
        observation["started_at"] = applied_at.isoformat()
    _write_json(
        root / "artifacts/production_workflow/parameter_release_history.json",
        {"releases": [release]},
    )
    _write_json(
        root
        / "artifacts/production_workflow/observations"
        / release_id
        / "observation_summary.json",
        observation,
    )
    _write_json(
        root
        / "artifacts/production_workflow/rollback_recommendations"
        / release_id
        / "rollback_recommendation.json",
        rollback,
    )


def test_valid_rollback_risk_wins_over_invalid_observation(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    release_id = "rel_valid_rb_wins"
    common = {
        "release_id": release_id,
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
    }
    source = make_source_provenance(
        source_kind="research_round",
        source_id="phase3_valid_risk",
        source_timestamp=now - timedelta(minutes=30),
        source_phase="phase3",
        source_family="independent",
        source_timeframe="15m",
        source_payload={"round_id": "phase3_valid_risk", "failure_pct": 90},
    )
    _write_release_with_evidence(
        tmp_path,
        release_id=release_id,
        observation={
            **common,
            "status": "observing",
            "recommendation": "review",
            "observation_window_hours": 24,
            "window_active": True,
            "started_at": (now - timedelta(hours=3)).isoformat(),
            "evaluated_at": (now - timedelta(hours=3)).isoformat(),
        },
        rollback={
            **common,
            "rollback_recommended": True,
            "severity": "high",
            "evaluated_at": now.isoformat(),
            "evidence_contract_version": POST_APPLY_EVIDENCE_CONTRACT_VERSION,
            "source_provenance": [source],
            "fired_trigger_count": 1,
            "triggers": [
                {
                    "trigger": "attribution_regression",
                    "fired": True,
                    "severity": "high",
                    "evidence_status": "valid",
                    "source_provenance": source,
                }
            ],
        },
    )

    with _disable_governance_db():
        evaluation = evaluate_release_effectiveness(
            tmp_path,
            release_id,
            save_result=False,
        )

    assert evaluation["conclusion"] == "rollback_triggered"
    assert evaluation["evidence_reconciliation_required"] is True


def test_valid_observation_risk_wins_over_invalid_rollback(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    release_id = "rel_valid_obs_wins"
    common = {
        "release_id": release_id,
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
    }
    source = make_source_provenance(
        source_kind="governance_snapshot",
        source_id="quality_monitor:valid_risk",
        source_timestamp=now - timedelta(minutes=30),
        source_payload={"health": "unhealthy", "critical_failures": 1},
    )
    _write_release_with_evidence(
        tmp_path,
        release_id=release_id,
        observation={
            **common,
            "status": "rollback_recommended",
            "recommendation": "rollback_recommended",
            "observation_window_hours": 24,
            "window_active": True,
            "started_at": "__APPLIED_AT__",
            "evaluated_at": now.isoformat(),
            "evidence_contract_version": POST_APPLY_EVIDENCE_CONTRACT_VERSION,
            "source_provenance": [source],
            "regression_count": 1,
            "checklist": [
                {
                    "name": "quality_monitor",
                    "status": "regression",
                    "severity": "high",
                    "source_provenance": source,
                }
            ],
        },
        rollback={
            **common,
            "rollback_recommended": False,
            "severity": "none",
            "evaluated_at": (now - timedelta(hours=3)).isoformat(),
        },
    )

    with _disable_governance_db():
        evaluation = evaluate_release_effectiveness(
            tmp_path,
            release_id,
            save_result=False,
        )

    assert evaluation["conclusion"] == "rollback_triggered"
    assert evaluation["evidence_reconciliation_required"] is True
