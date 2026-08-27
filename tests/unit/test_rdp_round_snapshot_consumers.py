from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aats.data_platform.production_workflow.observation_window import (
    _check_attribution_regression,
    _check_execution_regression,
)
from aats.data_platform.production_workflow.rollback_policy import (
    _evaluate_attribution_regression,
    _evaluate_execution_regression,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_observation_rejects_manifestless_global_attribution(
    tmp_path: Path,
) -> None:
    _write_json(
        tmp_path / "artifacts/research/attribution_rounds/20260416_010101_deadbeef/attribution_summary.json",
        {
            "strategy_failure_pct": 95,
            "risk_failure_pct": 0,
            "execution_failure_pct": 0,
        },
    )

    result = _check_attribution_regression(
        tmp_path,
        "independent",
        "15m",
        not_before=datetime.min.replace(tzinfo=timezone.utc),
    )

    assert result["status"] == "unknown"
    assert result["detail"] == "missing attribution round snapshot"


def test_observation_rejects_manifestless_execution_summary(tmp_path: Path) -> None:
    _write_json(
        tmp_path
        / "artifacts/research/execution_rounds/20260416_010101_deadbeef/independent_15m/execution_summary.json",
        {
            "full_fill_ratio": 0.9,
            "mean_total_execution_cost_bps": 4.2,
            "positive_adjusted_edge_ratio": 0.81,
        },
    )

    result = _check_execution_regression(
        tmp_path,
        "independent",
        "15m",
        not_before=datetime.min.replace(tzinfo=timezone.utc),
    )

    assert result["status"] == "unknown"
    assert result["detail"] == "missing execution round snapshot"


def test_rollback_policy_does_not_fallback_to_global_attribution_summary(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/research/attribution_rounds/20260416_010101_deadbeef/attribution_summary.json",
        {
            "strategy_failure_pct": 95,
            "risk_failure_pct": 0,
            "execution_failure_pct": 0,
        },
    )

    result = _evaluate_attribution_regression(
        tmp_path,
        "independent",
        "15m",
        not_before=datetime.min.replace(tzinfo=timezone.utc),
    )

    assert result["fired"] is False
    assert result["detail"] == "missing attribution round snapshot"


def test_rollback_policy_does_not_fallback_to_global_execution_summary(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/research/execution_rounds/20260416_010101_deadbeef/execution_summary.json",
        {
            "full_fill_ratio": 0.1,
            "mean_total_execution_cost_bps": 18.5,
            "positive_adjusted_edge_ratio": 0.05,
        },
    )

    result = _evaluate_execution_regression(
        tmp_path,
        "independent",
        "15m",
        not_before=datetime.min.replace(tzinfo=timezone.utc),
    )

    assert result["fired"] is False
    assert result["detail"] == "missing execution round snapshot"


def test_malformed_attribution_metrics_are_insufficient_not_exception(
    tmp_path: Path,
) -> None:
    with patch(
        "aats.data_platform.production_workflow.rollback_policy."
        "_load_combo_round_summary",
        return_value=(
            {"round_id": "round_1"},
            {},
            {
                "strategy_failure_pct": "bad",
                "risk_failure_pct": 0,
                "execution_failure_pct": 0,
            },
        ),
    ):
        result = _evaluate_attribution_regression(
            tmp_path,
            "independent",
            "15m",
            not_before=datetime.min.replace(tzinfo=timezone.utc),
        )

    assert result["fired"] is False
    assert result["evidence_status"] == "insufficient"


def test_malformed_execution_metrics_are_insufficient_not_exception(
    tmp_path: Path,
) -> None:
    with patch(
        "aats.data_platform.production_workflow.rollback_policy."
        "_load_combo_round_summary",
        return_value=(
            {"round_id": "round_1"},
            {},
            {
                "full_fill_ratio": "bad",
                "mean_total_execution_cost_bps": 1.0,
                "positive_adjusted_edge_ratio": 0.8,
            },
        ),
    ):
        result = _evaluate_execution_regression(
            tmp_path,
            "independent",
            "15m",
            not_before=datetime.min.replace(tzinfo=timezone.utc),
        )

    assert result["fired"] is False
    assert result["evidence_status"] == "insufficient"


def test_observation_malformed_metrics_are_unknown_not_exception(
    tmp_path: Path,
) -> None:
    with patch(
        "aats.data_platform.production_workflow.observation_window."
        "_load_combo_round_summary",
        return_value=(
            {"round_id": "round_1"},
            {},
            {
                "full_fill_ratio": float("nan"),
                "mean_total_execution_cost_bps": 1.0,
                "positive_adjusted_edge_ratio": 0.8,
            },
        ),
    ):
        result = _check_execution_regression(
            tmp_path,
            "independent",
            "15m",
            not_before=datetime.min.replace(tzinfo=timezone.utc),
        )

    assert result["status"] == "unknown"
    assert result["severity"] == "none"


def test_malformed_snapshot_containers_are_insufficient_not_exception(
    tmp_path: Path,
) -> None:
    malformed_snapshot = {
        "round_id": "round_bad",
        "status": "succeeded",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "summary": {"combos": "bad"},
    }
    not_before = datetime.min.replace(tzinfo=timezone.utc)
    with (
        patch(
            "aats.data_platform.production_workflow.observation_window."
            "load_latest_research_round_snapshot",
            return_value=malformed_snapshot,
        ),
        patch(
            "aats.data_platform.production_workflow.rollback_policy."
            "load_latest_research_round_snapshot",
            return_value=malformed_snapshot,
        ),
    ):
        observation = _check_attribution_regression(
            tmp_path,
            "independent",
            "15m",
            not_before=not_before,
        )
        rollback = _evaluate_attribution_regression(
            tmp_path,
            "independent",
            "15m",
            not_before=not_before,
        )

    assert observation["status"] == "unknown"
    assert rollback["evidence_status"] == "insufficient"
