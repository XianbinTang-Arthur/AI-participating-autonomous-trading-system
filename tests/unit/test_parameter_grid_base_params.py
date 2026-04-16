from __future__ import annotations

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
