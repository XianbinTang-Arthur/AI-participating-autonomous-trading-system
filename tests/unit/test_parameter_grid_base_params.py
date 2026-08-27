from __future__ import annotations

import pytest

from aats.data_platform.replay.scan.parameter_grid import build_grid


def test_build_grid_merges_base_params_before_grid_overrides() -> None:
    combos = build_grid(
        {
            "min_confirm_ticks": [2],
            "score_stability_threshold": [4.5],
        },
        base_params={
            "min_safe_net_edge_bps": 2.5,
            "score_stability_threshold": 5.0,
        },
    )

    assert len(combos) == 1
    payload = combos[0].to_dict()
    assert payload["min_safe_net_edge_bps"] == 2.5
    assert payload["score_stability_threshold"] == 4.5


def test_build_grid_uses_directional_family_baseline_for_partial_overrides() -> None:
    combos = build_grid(
        {"min_confirm_ticks": [3]},
        family="directional",
    )

    assert len(combos) == 1
    assert combos[0].entry_threshold == 0.45
    assert combos[0].close_threshold == 0.20
    assert combos[0].scale_in_threshold == 0.55


def test_build_grid_independent_default_is_preserved() -> None:
    combo = build_grid({"min_confirm_ticks": [2]})[0]

    assert combo.entry_threshold == 0.30
    assert combo.close_threshold == 0.15
    assert combo.scale_in_threshold == 0.40


def test_build_grid_rejects_conflicting_flat_and_nested_cost_sources() -> None:
    with pytest.raises(
        ValueError,
        match="flat cost parameters conflict with nested cost_config",
    ):
        build_grid(
            {"cost_config": [{"taker_fee_bps": 3.0}]},
            base_params={"slippage_bps": 2.0},
        )


@pytest.mark.parametrize(
    ("cost_config", "reason"),
    [
        (None, "cost_config must be a string-keyed mapping"),
        ({"typo_fee_bps": 3.0}, "unknown_cost_config_keys"),
    ],
)
def test_build_grid_rejects_null_or_unknown_nested_cost_config(
    cost_config: object,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        build_grid({"cost_config": [cost_config]})


@pytest.mark.parametrize(
    ("grid", "message"),
    (
        ({"min_confirm_ticks": [True]}, "integer, not boolean"),
        ({"typo_parameter": [1]}, "unknown_replay_parameter_keys"),
        ({"entry_threshold": []}, "non_empty_lists"),
        (
            {
                "entry_threshold": [0.1],
                "close_threshold": [0.2],
            },
            "parameter_grid_has_no_valid_combinations",
        ),
    ),
)
def test_build_grid_fails_closed_for_invalid_schema_or_empty_result(
    grid: dict[str, list[object]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_grid(grid)
