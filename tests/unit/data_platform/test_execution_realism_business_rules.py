from __future__ import annotations

import math

import pytest

from aats.data_platform.execution_realism.aggregation import (
    _identify_top_execution_failure,
)
from aats.data_platform.execution_realism.execution_cost_model import (
    build_execution_cost_summary,
)
from aats.data_platform.execution_realism.fill_feasibility import (
    evaluate_fill_feasibility,
)
from aats.data_platform.execution_realism.slippage_estimator import estimate_slippage


def test_execution_failure_aggregation_accepts_blank_csv_numbers() -> None:
    rows = [
        {
            "feasibility_category": "insufficient_market_data",
            "slippage_data_quality": "no_data",
            "cost_adjusted_edge_bps": "",
            "estimated_slippage_bps": "",
        },
    ]

    assert _identify_top_execution_failure(rows) == "insufficient_data(1)"


def test_execution_failure_aggregation_ignores_non_finite_numbers() -> None:
    rows = [
        {
            "feasibility_category": "fully_fillable",
            "cost_adjusted_edge_bps": float("nan"),
            "estimated_slippage_bps": float("inf"),
        },
    ]

    assert _identify_top_execution_failure(rows) == "none"


def test_slippage_marks_non_finite_market_inputs_as_no_data() -> None:
    [result] = estimate_slippage(
        [
            {
                "candidate_side": "buy",
                "bar_close": math.nan,
                "bar_range_bps": 10,
                "volume_ratio": 0.01,
                "feasibility_category": "fully_fillable",
            },
        ],
    )

    assert result["slippage_data_quality"] == "no_data"
    assert result["estimated_slippage_bps"] is None


@pytest.mark.parametrize("volume_ratio", (None, "", True, math.nan, math.inf, -0.01))
def test_slippage_does_not_understate_missing_or_invalid_volume_impact(
    volume_ratio: object,
) -> None:
    [result] = estimate_slippage(
        [
            {
                "candidate_side": "buy",
                "bar_close": 100,
                "bar_range_bps": 10,
                "volume_ratio": volume_ratio,
                "feasibility_category": "fully_fillable",
            },
        ],
    )

    assert result["slippage_data_quality"] == "no_data"
    assert result["estimated_slippage_bps"] is None


def test_execution_cost_summary_excludes_non_finite_metrics() -> None:
    summary = build_execution_cost_summary(
        [
            {
                "candidate_action": "open",
                "feasibility_category": "fully_fillable",
                "estimated_slippage_bps": math.nan,
                "estimated_total_execution_cost_bps": math.inf,
                "estimated_fee_bps": 5,
                "cost_adjusted_edge_bps": "",
            },
        ],
    )

    assert summary["slippage"] == {}
    assert summary["total_execution_cost"] == {}
    assert summary["cost_adjusted_edge"] == {}
    assert summary["fee"]["mean"] == 5


def test_slippage_rejects_non_finite_edge_instead_of_emitting_nan() -> None:
    with pytest.raises(ValueError, match="expected_net_edge_bps"):
        estimate_slippage(
            [
                {
                    "candidate_side": "buy",
                    "bar_close": 100,
                    "bar_range_bps": 10,
                    "volume_ratio": 0.01,
                    "feasibility_category": "fully_fillable",
                    "expected_net_edge_bps": math.nan,
                },
            ],
        )


@pytest.mark.parametrize("candidate_qty", (None, "", True, 0, -1, math.nan, math.inf))
def test_fill_feasibility_rejects_invalid_candidate_quantity(candidate_qty: object) -> None:
    with pytest.raises(ValueError, match="candidate_qty"):
        evaluate_fill_feasibility(
            [
                {
                    "candidate_qty": candidate_qty,
                    "bar_volume": 100,
                    "alignment_status": "matched",
                },
            ],
        )
