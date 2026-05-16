import pytest

from aats.data_platform.execution_realism.execution_cost_model import build_execution_cost_summary


def test_execution_cost_summary_includes_research_factory_cost_stack() -> None:
    summary = build_execution_cost_summary(
        [
            {
                "candidate_action": "open",
                "feasibility_category": "fully_fillable",
                "estimated_slippage_bps": 1.5,
                "estimated_fee_bps": 5.0,
                "funding_adjustment_bps": 0.25,
                "estimated_total_execution_cost_bps": 6.5,
                "cost_adjusted_edge_bps": 2.0,
            },
            {
                "candidate_action": "close",
                "feasibility_category": "partially_fillable",
                "estimated_slippage_bps": 2.5,
                "estimated_fee_bps": 5.0,
                "funding_adjustment_bps": 0.75,
                "estimated_total_execution_cost_bps": 7.5,
                "cost_adjusted_edge_bps": 1.0,
            },
        ]
    )

    assert summary["full_fill_ratio"] == pytest.approx(0.5)
    assert summary["partial_fill_ratio"] == pytest.approx(0.5)
    assert summary["slippage"]["mean"] == pytest.approx(2.0)
    assert summary["fee"]["mean"] == pytest.approx(5.0)
    assert summary["funding"]["mean"] == pytest.approx(0.5)
    assert summary["turnover"]["mean"] == pytest.approx(1.0)
    assert summary["cost_adjusted_edge"]["mean"] == pytest.approx(1.5)
