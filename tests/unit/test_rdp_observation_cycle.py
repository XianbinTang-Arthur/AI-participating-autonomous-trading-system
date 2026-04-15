from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from aats.data_platform.production_workflow.observation_cycle import (
    run_release_observation_cycle,
)


def test_run_release_observation_cycle_processes_observing_releases() -> None:
    history = {
        "releases": [
            {
                "release_id": "rel_observe_1",
                "family": "independent",
                "timeframe": "15m",
                "apply_result": "success",
                "observation_status": "observing",
                "observation_window_hours": 24,
            },
            {
                "release_id": "rel_done_1",
                "family": "independent",
                "timeframe": "1h",
                "apply_result": "success",
                "observation_status": "completed",
                "observation_window_hours": 24,
            },
        ],
    }
    with (
        patch(
            "aats.data_platform.production_workflow.release_registry.load_release_history",
            return_value=history,
        ),
        patch(
            "aats.data_platform.production_workflow.observation_window.run_observation",
            return_value={"status": "observing", "recommendation": "review"},
        ) as observation_mock,
        patch(
            "aats.data_platform.production_workflow.rollback_policy.evaluate_rollback_recommendation",
            return_value={"rollback_recommended": True, "severity": "medium"},
        ) as rollback_mock,
        patch(
            "aats.data_platform.metrics.release_effectiveness.evaluate_release_effectiveness",
            return_value={"conclusion": "mixed"},
        ) as effectiveness_mock,
        patch(
            "aats.data_platform.metrics.release_effectiveness.enforce_pending_rollbacks",
            return_value=[{"release_id": "rel_observe_1", "ok": True}],
        ),
    ):
        result = run_release_observation_cycle(Path("."), save_results=True)

    assert result["ok"] is True
    assert result["processed_count"] == 1
    assert result["rollback_recommended_count"] == 1
    assert result["auto_rollback_count"] == 1
    observation_mock.assert_called_once()
    rollback_mock.assert_called_once()
    effectiveness_mock.assert_called_once()
