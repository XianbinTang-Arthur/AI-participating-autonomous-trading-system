import pytest

from aats.data_platform.research_factory.benchmarks.baseline import run_factor_baseline
from aats.data_platform.research_factory.specs import MetricsSnapshot


DATASET = object()


def test_run_factor_baseline_reports_perfect_positive_ic() -> None:
    metrics = run_factor_baseline(
        DATASET,
        factor_values=[1.0, 2.0, 3.0, 4.0],
        label_values=[0.01, 0.02, 0.03, 0.04],
        cost_config={"periods_per_year": 1.0},
    )

    assert isinstance(metrics, MetricsSnapshot)
    assert metrics.ic == pytest.approx(1.0)
    assert metrics.rank_ic == pytest.approx(1.0)
    assert metrics.net_annualized_return == pytest.approx(0.025)


def test_run_factor_baseline_reports_perfect_negative_ic() -> None:
    metrics = run_factor_baseline(
        DATASET,
        factor_values=[4.0, 3.0, 2.0, 1.0],
        label_values=[0.01, 0.02, 0.03, 0.04],
        cost_config={"periods_per_year": 1.0},
    )

    assert metrics.ic == pytest.approx(-1.0)
    assert metrics.rank_ic == pytest.approx(-1.0)


def test_run_factor_baseline_subtracts_fee_slippage_and_funding_from_net_return() -> None:
    metrics = run_factor_baseline(
        DATASET,
        factor_values=[1.0, 1.0, 1.0],
        label_values=[0.01, 0.02, -0.01],
        cost_config={
            "fee_bps": 10.0,
            "slippage_bps": 5.0,
            "funding_bps": 2.0,
            "periods_per_year": 1.0,
        },
    )

    expected_gross_mean = (0.01 + 0.02 - 0.01) / 3
    expected_trade_cost_mean = (15.0 / 10_000.0) / 3
    expected_funding_cost_mean = 2.0 / 10_000.0
    expected_net_mean = expected_gross_mean - expected_trade_cost_mean - expected_funding_cost_mean
    assert metrics.annualized_return == pytest.approx(expected_gross_mean)
    assert metrics.net_annualized_return == pytest.approx(expected_net_mean)
    assert metrics.fee_bps_mean == pytest.approx(10.0)
    assert metrics.slippage_bps_mean == pytest.approx(5.0)
    assert metrics.funding_bps_mean == pytest.approx(2.0)
    assert metrics.cost_adjusted_edge_bps_mean == pytest.approx(expected_net_mean * 10_000.0)


def test_run_factor_baseline_uses_long_flat_signal_and_turnover_proxy() -> None:
    metrics = run_factor_baseline(
        DATASET,
        factor_values=[1.0, -1.0, 2.0, 0.0],
        label_values=[0.01, 0.02, 0.03, 0.04],
        cost_config={"periods_per_year": 1.0},
    )

    assert metrics.annualized_return == pytest.approx((0.01 + 0.0 + 0.03 + 0.0) / 4)
    assert metrics.turnover == pytest.approx(1.0)


def test_run_factor_baseline_charges_exit_turnover_cost() -> None:
    metrics = run_factor_baseline(
        DATASET,
        factor_values=[1.0, 0.0],
        label_values=[0.01, 0.02],
        cost_config={
            "fee_bps": 10.0,
            "slippage_bps": 5.0,
            "funding_bps": 0.0,
            "periods_per_year": 1.0,
        },
    )

    expected_net_mean = ((0.01 - 15.0 / 10_000.0) + (0.0 - 15.0 / 10_000.0)) / 2
    assert metrics.net_annualized_return == pytest.approx(expected_net_mean)
    assert metrics.turnover == pytest.approx(1.0)


def test_run_factor_baseline_all_null_factor_returns_failed_metrics_without_candidate() -> None:
    metrics = run_factor_baseline(
        DATASET,
        factor_values=[None, None],
        label_values=[0.01, 0.02],
        cost_config={"periods_per_year": 1.0},
    )

    assert metrics.ic is None
    assert metrics.net_annualized_return is None
    assert metrics.missing_reasons["ic"] == "no valid factor/label pairs; candidate not generated"
    assert metrics.missing_reasons["candidate_generated"] == "false"


def test_run_factor_baseline_rejects_mismatched_input_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        run_factor_baseline(
            DATASET,
            factor_values=[1.0],
            label_values=[0.01, 0.02],
            cost_config={},
        )


def test_run_factor_baseline_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        run_factor_baseline(
            DATASET,
            factor_values=[float("inf")],
            label_values=[0.01],
            cost_config={},
        )


def test_run_factor_baseline_rejects_non_finite_costs() -> None:
    with pytest.raises(ValueError, match="finite"):
        run_factor_baseline(
            DATASET,
            factor_values=[1.0],
            label_values=[0.01],
            cost_config={"fee_bps": float("nan")},
        )
