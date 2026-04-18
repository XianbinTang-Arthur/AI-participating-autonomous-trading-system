"""Profile clamp single-source-of-truth unit tests (R1-04)."""

from __future__ import annotations

import pytest

from aats.data_platform.research.profile_clamps import (
    PROFILE_CLAMPS,
    clamp_value,
    clamp_violation_direction,
    get_profile_clamps,
    is_in_clamp,
)


def test_all_profiles_have_expected_keys() -> None:
    expected = {
        "strategy_entry_min_signal_edge_bps",
        "strategy_entry_alpha_min",
        "strategy_min_net_edge_bps",
    }
    for pid, clamps in PROFILE_CLAMPS.items():
        assert set(clamps.keys()) == expected, f"profile {pid} missing keys"


def test_clamp_ranges_valid() -> None:
    """每个 clamp 必须 lo < hi。"""
    for pid, clamps in PROFILE_CLAMPS.items():
        for key, rng in clamps.items():
            assert rng["lo"] < rng["hi"], f"{pid}.{key} lo>=hi"


def test_get_clamps_unknown_profile() -> None:
    with pytest.raises(KeyError):
        get_profile_clamps("nonexistent-profile")


def test_clamp_value_clamps_low() -> None:
    lo = PROFILE_CLAMPS["trend_normal"]["strategy_entry_min_signal_edge_bps"]["lo"]
    assert clamp_value(
        "trend_normal", "strategy_entry_min_signal_edge_bps", lo - 5.0,
    ) == lo


def test_clamp_value_clamps_high() -> None:
    hi = PROFILE_CLAMPS["trend_normal"]["strategy_entry_min_signal_edge_bps"]["hi"]
    assert clamp_value(
        "trend_normal", "strategy_entry_min_signal_edge_bps", hi + 5.0,
    ) == hi


def test_clamp_value_passes_through_mid() -> None:
    rng = PROFILE_CLAMPS["trend_normal"]["strategy_entry_min_signal_edge_bps"]
    mid = (rng["lo"] + rng["hi"]) / 2
    assert clamp_value(
        "trend_normal", "strategy_entry_min_signal_edge_bps", mid,
    ) == mid


def test_is_in_clamp() -> None:
    rng = PROFILE_CLAMPS["trend_normal"]["strategy_entry_min_signal_edge_bps"]
    assert is_in_clamp("trend_normal", "strategy_entry_min_signal_edge_bps", rng["lo"])
    assert is_in_clamp("trend_normal", "strategy_entry_min_signal_edge_bps", rng["hi"])
    assert not is_in_clamp(
        "trend_normal", "strategy_entry_min_signal_edge_bps", rng["hi"] + 0.01,
    )
    assert not is_in_clamp(
        "trend_normal", "strategy_entry_min_signal_edge_bps", rng["lo"] - 0.01,
    )


def test_violation_direction() -> None:
    rng = PROFILE_CLAMPS["trend_normal"]["strategy_entry_min_signal_edge_bps"]
    assert clamp_violation_direction(
        "trend_normal", "strategy_entry_min_signal_edge_bps", rng["hi"] + 1,
    ) == "above_upper"
    assert clamp_violation_direction(
        "trend_normal", "strategy_entry_min_signal_edge_bps", rng["lo"] - 1,
    ) == "below_lower"
    mid = (rng["lo"] + rng["hi"]) / 2
    assert clamp_violation_direction(
        "trend_normal", "strategy_entry_min_signal_edge_bps", mid,
    ) is None
