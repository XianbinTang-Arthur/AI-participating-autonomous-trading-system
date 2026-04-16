from __future__ import annotations

import json
from pathlib import Path

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


def test_observation_warns_when_latest_round_missing_combo_specific_attribution(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/research/attribution_rounds/20260416_010101_deadbeef/attribution_summary.json",
        {
            "strategy_failure_pct": 95,
            "risk_failure_pct": 0,
            "execution_failure_pct": 0,
        },
    )

    result = _check_attribution_regression(tmp_path, "independent", "15m")

    assert result["status"] == "warn"
    assert "independent_15m" in result["detail"]
    assert "20260416_010101_deadbeef" in result["detail"]


def test_observation_reads_combo_specific_execution_summary_from_manifestless_round(tmp_path: Path) -> None:
    _write_json(
        tmp_path
        / "artifacts/research/execution_rounds/20260416_010101_deadbeef/independent_15m/execution_summary.json",
        {
            "full_fill_ratio": 0.9,
            "mean_total_execution_cost_bps": 4.2,
            "positive_adjusted_edge_ratio": 0.81,
        },
    )

    result = _check_execution_regression(tmp_path, "independent", "15m")

    assert result["status"] == "ok"
    assert "independent_15m" in result["detail"]
    assert "cost=4.2bps" in result["detail"]


def test_rollback_policy_does_not_fallback_to_global_attribution_summary(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/research/attribution_rounds/20260416_010101_deadbeef/attribution_summary.json",
        {
            "strategy_failure_pct": 95,
            "risk_failure_pct": 0,
            "execution_failure_pct": 0,
        },
    )

    result = _evaluate_attribution_regression(tmp_path, "independent", "15m")

    assert result["fired"] is False
    assert "independent_15m" in result["detail"]
    assert "20260416_010101_deadbeef" in result["detail"]


def test_rollback_policy_does_not_fallback_to_global_execution_summary(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/research/execution_rounds/20260416_010101_deadbeef/execution_summary.json",
        {
            "full_fill_ratio": 0.1,
            "mean_total_execution_cost_bps": 18.5,
            "positive_adjusted_edge_ratio": 0.05,
        },
    )

    result = _evaluate_execution_regression(tmp_path, "independent", "15m")

    assert result["fired"] is False
    assert "independent_15m" in result["detail"]
    assert "20260416_010101_deadbeef" in result["detail"]
