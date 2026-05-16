import pytest

from aats.data_platform.research_factory.allocation.policy import (
    ALLOCATION_ARMS,
    DEFAULT_EPSILON_FLOOR,
    AllocationDecision,
    ResearchAllocationInput,
    choose_next_research_action,
)
from aats.data_platform.research_factory.specs import MetricsSnapshot


def complete_metrics_snapshot(**overrides: float | None) -> MetricsSnapshot:
    values: dict[str, float | None] = {
        "ic": 0.1,
        "rank_ic": 0.2,
        "icir": 0.3,
        "rank_icir": 0.4,
        "annualized_return": 0.05,
        "net_annualized_return": 0.03,
        "information_ratio": 0.7,
        "sharpe": 0.8,
        "max_drawdown": -0.1,
        "turnover": 0.2,
        "fee_bps_mean": 5.0,
        "slippage_bps_mean": 2.0,
        "funding_bps_mean": 0.5,
        "fillable_ratio": 0.9,
        "partial_fill_ratio": 0.05,
        "cost_adjusted_edge_bps_mean": 1.2,
    }
    values.update(overrides)
    missing_reasons = {
        metric_name: f"{metric_name} not measured"
        for metric_name, value in values.items()
        if value is None
    }
    return MetricsSnapshot(**values, missing_reasons=missing_reasons)


def test_high_mdd_direction_is_downweighted() -> None:
    decision = choose_next_research_action(
        ResearchAllocationInput(
            metrics_by_arm={
                "factor": complete_metrics_snapshot(max_drawdown=-0.05),
                "model": complete_metrics_snapshot(max_drawdown=-0.50),
            },
            sample_counts={"factor": 2, "model": 2},
        )
    )

    assert decision.arm == "factor"
    assert decision.scores["factor"] > decision.scores["model"]
    assert any("model: max_drawdown=0.500000" in reason for reason in decision.reason_trace)


def test_missing_critical_metrics_downweights_arm() -> None:
    decision = choose_next_research_action(
        ResearchAllocationInput(
            metrics_by_arm={
                "factor": complete_metrics_snapshot(net_annualized_return=None),
                "model": complete_metrics_snapshot(net_annualized_return=0.03),
            },
            sample_counts={"factor": 1, "model": 1},
        )
    )

    assert decision.arm == "model"
    assert decision.scores["factor"] < decision.scores["model"]
    assert any("factor: missing_critical_metrics=net_annualized_return" in reason for reason in decision.reason_trace)


def test_low_sample_arm_receives_epsilon_floor_and_can_be_selected() -> None:
    decision = choose_next_research_action(
        ResearchAllocationInput(
            metrics_by_arm={
                "factor": complete_metrics_snapshot(
                    ic=-0.2,
                    rank_ic=-0.2,
                    net_annualized_return=-0.1,
                    information_ratio=-0.5,
                    max_drawdown=-0.6,
                    turnover=1.0,
                )
            },
            sample_counts={"factor": 20, "validation": 0},
            epsilon_floor=DEFAULT_EPSILON_FLOOR,
        )
    )

    assert decision.arm == "model"
    assert decision.scores["model"] == pytest.approx(DEFAULT_EPSILON_FLOOR)
    assert decision.scores["factor"] < 0
    assert any("model: no metrics; using exploration-only reward" in reason for reason in decision.reason_trace)


def test_decision_contains_reason_trace_for_all_arms() -> None:
    decision = choose_next_research_action(
        ResearchAllocationInput(
            metrics_by_arm={"factor": complete_metrics_snapshot()},
            sample_counts={"factor": 3},
        )
    )

    assert isinstance(decision, AllocationDecision)
    for arm in ALLOCATION_ARMS:
        assert arm in decision.scores
        assert any(reason.startswith(f"{arm}:") for reason in decision.reason_trace)
    assert decision.reason_trace[-1].startswith("selected=")


def test_allocation_input_rejects_unknown_arm() -> None:
    with pytest.raises(ValueError, match="allocation arm"):
        ResearchAllocationInput(metrics_by_arm={"llm": None})
