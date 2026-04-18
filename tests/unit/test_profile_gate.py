"""Profile gate (Sharpe/MaxDD/Activity) unit tests.

场景覆盖 v3 §1.4 + §6.2 的 3 指标 + 本次事件(activity_ratio 拦截 clamp=13.0)。
"""

from __future__ import annotations

import pytest

from aats.data_platform.gates.profile_gate import (
    ACTIVITY_RATIO_MIN,
    MAXDD_RATIO_MAX,
    SHARPE_RATIO_MIN,
    check_profile_gate,
    compute_metrics_from_replay,
)


def test_gate_pass() -> None:
    result = check_profile_gate(
        {"sharpe_ratio": 1.0, "maxdd_ratio": 1.0, "activity_ratio": 1.0},
    )
    assert result.allow_apply
    assert result.failures == ()


def test_gate_fail_sharpe() -> None:
    result = check_profile_gate(
        {"sharpe_ratio": SHARPE_RATIO_MIN - 0.01, "maxdd_ratio": 1.0, "activity_ratio": 1.0},
    )
    assert not result.allow_apply
    assert any("sharpe_ratio" in f for f in result.failures)


def test_gate_fail_maxdd() -> None:
    result = check_profile_gate(
        {"sharpe_ratio": 1.0, "maxdd_ratio": MAXDD_RATIO_MAX + 0.01, "activity_ratio": 1.0},
    )
    assert not result.allow_apply
    assert any("maxdd_ratio" in f for f in result.failures)


def test_gate_fail_activity_blocks_edge13_incident() -> None:
    """Regression: seed 把 signal_edge=13 → activity 接近 0,
    Sharpe/MaxDD "看起来不错",但 activity_ratio 应拦住。"""
    result = check_profile_gate(
        {"sharpe_ratio": 1.5, "maxdd_ratio": 0.8, "activity_ratio": 0.1},
    )
    assert not result.allow_apply
    assert any("activity_ratio" in f for f in result.failures)


def test_gate_missing_metric_raises() -> None:
    with pytest.raises(ValueError):
        check_profile_gate({"sharpe_ratio": 1.0})


def test_compute_metrics_basic() -> None:
    m = compute_metrics_from_replay(
        current_stats={"sharpe": 1.0, "maxdd": -0.10, "trades_per_year": 100},
        candidate_stats={"sharpe": 1.2, "maxdd": -0.08, "trades_per_year": 80},
    )
    assert m["sharpe_ratio"] == pytest.approx(1.2)
    assert m["maxdd_ratio"] == pytest.approx(0.8)
    assert m["activity_ratio"] == pytest.approx(0.8)


def test_compute_metrics_zero_denominator() -> None:
    m = compute_metrics_from_replay(
        current_stats={"sharpe": 0.0, "maxdd": 0.0, "trades_per_year": 0.0},
        candidate_stats={"sharpe": 1.0, "maxdd": -0.1, "trades_per_year": 100},
    )
    # inf 是可容忍的;Gate 会把这类当高于阈值,要求上游过滤。
    assert m["sharpe_ratio"] == float("inf")


def test_gate_boundary_exact_min() -> None:
    """刚好等于阈值时通过(>= / <= 边界)。"""
    result = check_profile_gate({
        "sharpe_ratio": SHARPE_RATIO_MIN,
        "maxdd_ratio": MAXDD_RATIO_MAX,
        "activity_ratio": ACTIVITY_RATIO_MIN,
    })
    assert result.allow_apply
