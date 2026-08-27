from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aats.data_platform.production_workflow.rollback_policy import (
    evaluate_rollback_recommendation,
)


@pytest.mark.parametrize("apply_result", ["pending", "failed", "blocked_by_gate", None])
def test_rollback_evaluation_rejects_non_success_release_without_writes(
    apply_result: str | None,
) -> None:
    release = {
        "release_id": "rel_not_applied",
        "family": "independent",
        "timeframe": "15m",
        "apply_result": apply_result,
    }
    save_result = MagicMock()
    attribution = MagicMock()
    with (
        patch(
            "aats.data_platform.production_workflow.release_registry."
            "load_release_history",
            return_value={"releases": [release]},
        ),
        patch(
            "aats.data_platform.production_workflow.rollback_policy."
            "_save_rollback_recommendation",
            save_result,
        ),
        patch(
            "aats.data_platform.production_workflow.rollback_policy."
            "_evaluate_attribution_regression",
            attribution,
        ),
    ):
        result = evaluate_rollback_recommendation(
            Path("."),
            release_id="rel_not_applied",
            family="independent",
            timeframe="15m",
        )

    assert result["ok"] is False
    assert result["reason"] == "release_not_applied"
    attribution.assert_not_called()
    save_result.assert_not_called()


def test_rollback_evaluation_rejects_wrong_combo_without_writes() -> None:
    release = {
        "release_id": "rel_wrong_combo",
        "family": "independent",
        "timeframe": "15m",
        "apply_result": "success",
    }
    save_result = MagicMock()
    execution = MagicMock()
    with (
        patch(
            "aats.data_platform.production_workflow.release_registry."
            "load_release_history",
            return_value={"releases": [release]},
        ),
        patch(
            "aats.data_platform.production_workflow.rollback_policy."
            "_save_rollback_recommendation",
            save_result,
        ),
        patch(
            "aats.data_platform.production_workflow.rollback_policy."
            "_evaluate_execution_regression",
            execution,
        ),
    ):
        result = evaluate_rollback_recommendation(
            Path("."),
            release_id="rel_wrong_combo",
            family="directional",
            timeframe="1h",
        )

    assert result["ok"] is False
    assert result["reason"] == "release_identity_mismatch"
    execution.assert_not_called()
    save_result.assert_not_called()
