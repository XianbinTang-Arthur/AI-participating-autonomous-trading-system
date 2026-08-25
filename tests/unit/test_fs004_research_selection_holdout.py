"""FS-004: train/valid selection and sealed test-holdout contracts."""

from datetime import UTC, datetime

from aats.data_platform.research_factory.metrics.gates import CandidateGateResult
from aats.data_platform.research_factory.real_data import (
    BENCHMARK_SEGMENT,
    DEVELOPMENT_SEGMENTS,
    HOLDOUT_SEGMENT,
    HOLDOUT_STATUS,
    REAL_DATA_CODE_VERSION,
    SELECTION_PROTOCOL_VERSION,
    _combine_development_gates,
)


def gate(*, passed: bool, failures: tuple[str, ...] = ()) -> CandidateGateResult:
    return CandidateGateResult(
        passed=passed,
        failures=failures,
        thresholds={
            "min_net_annualized_return": 0.0,
            "max_drawdown_limit": 0.2,
            "min_cost_adjusted_edge_bps_mean": 0.0,
            "critical_metrics": ("net_annualized_return",),
        },
        critical_metrics=("net_annualized_return",),
        evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def test_real_data_selection_protocol_has_explicit_development_and_holdout_roles() -> None:
    assert REAL_DATA_CODE_VERSION == "research_factory_real_data_runner_v2"
    assert SELECTION_PROTOCOL_VERSION == "train_valid_selection_test_holdout_v2"
    assert DEVELOPMENT_SEGMENTS == ("train", "valid")
    assert BENCHMARK_SEGMENT == "valid"
    assert HOLDOUT_SEGMENT == "test"
    assert HOLDOUT_STATUS == "sealed_not_evaluated"


def test_combined_development_gate_requires_both_train_and_valid() -> None:
    combined = _combine_development_gates(
        gate(passed=False, failures=("net edge is not positive",)),
        gate(passed=True),
        datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert combined.passed is False
    assert combined.failures == ("train: net edge is not positive",)
