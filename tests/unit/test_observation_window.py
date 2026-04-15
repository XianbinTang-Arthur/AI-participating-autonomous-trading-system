from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aats.data_platform.production_workflow.observation_window import (
    run_observation,
)


def _ok_check(name: str) -> dict[str, str]:
    return {"name": name, "status": "ok", "detail": "ok"}


def test_run_observation_dry_run_does_not_update_release_history() -> None:
    created_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    release = {
        "release_id": "rel_obs_1",
        "family": "independent",
        "timeframe": "15m",
        "created_at": created_at,
        "observation_status": "observing",
    }
    history = {"releases": [release]}

    with (
        patch(
            "aats.data_platform.production_workflow.release_registry.load_release_history",
            return_value=history,
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.find_release",
            return_value=release,
        ),
        patch(
            "aats.data_platform.production_workflow.observation_window._check_quality_monitor_regression",
            return_value=_ok_check("quality_monitor"),
        ),
        patch(
            "aats.data_platform.production_workflow.observation_window._check_decision_regression",
            return_value=_ok_check("decision_system"),
        ),
        patch(
            "aats.data_platform.production_workflow.observation_window._check_attribution_regression",
            return_value=_ok_check("attribution"),
        ),
        patch(
            "aats.data_platform.production_workflow.observation_window._check_execution_regression",
            return_value=_ok_check("execution"),
        ),
        patch(
            "aats.data_platform.production_workflow.observation_window._save_observation",
        ) as save_observation_mock,
        patch(
            "aats.data_platform.production_workflow.release_registry.update_release_status",
        ) as update_release_status_mock,
        patch(
            "aats.data_platform.production_workflow.release_registry.save_release_history",
        ) as save_release_history_mock,
    ):
        result = run_observation(
            Path("."),
            release_id="rel_obs_1",
            family="independent",
            timeframe="15m",
            save_result=False,
        )

    assert result["status"] == "observing"
    save_observation_mock.assert_not_called()
    update_release_status_mock.assert_not_called()
    save_release_history_mock.assert_not_called()
