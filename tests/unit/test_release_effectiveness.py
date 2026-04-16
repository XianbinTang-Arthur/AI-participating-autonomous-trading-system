from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from aats.data_platform.metrics.release_effectiveness import (
    evaluate_release_effectiveness,
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


def test_rollback_recommended_status_falls_back_to_negative_without_artifact(
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
    assert operations["score"] == "negative"
    assert "rollback recommended" in operations["detail"]
    assert evaluation["conclusion"] == "rollback_triggered"
