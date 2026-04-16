from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from aats.data_platform.operations.strategy_tuning_registry import (
    get_combo_tuning_overrides,
    load_strategy_tuning_registry,
    review_strategy_tuning_proposal,
)
from aats.data_platform.operations.strategy_tuning_review import (
    build_strategy_tuning_review,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_step2_round(root: Path, round_id: str, comparison: list[dict]) -> None:
    _write_json(
        root / "artifacts/research/step2_rounds" / round_id / "scan_comparison_summary.json",
        {
            "round_id": round_id,
            "experiment_count": len(comparison),
            "comparison": comparison,
        },
    )


def test_strategy_tuning_review_generates_pending_review_proposal(tmp_path: Path) -> None:
    _seed_step2_round(
        tmp_path,
        "20260416_090000_aaaa1111",
        [
            {
                "family": "independent",
                "timeframe": "15m",
                "label": "combo_1",
                "opening_count": 0,
                "mean_cost_bps": 5.6,
                "mean_expected_edge_bps": 1.2,
                "execution_compatible_ratio": 0.0,
                "positive_edge_ratio": 0.8,
                "top_blocking_reason": "net_edge_below_safe_minimum",
            },
            {
                "family": "independent",
                "timeframe": "15m",
                "label": "combo_2",
                "opening_count": 0,
                "mean_cost_bps": 5.7,
                "mean_expected_edge_bps": 1.1,
                "execution_compatible_ratio": 0.0,
                "positive_edge_ratio": 0.7,
                "top_blocking_reason": "net_edge_below_safe_minimum",
            },
        ],
    )
    phase4 = {
        "latest_round": {
            "round_id": "phase4_1",
            "combos": {
                "independent_15m": {
                    "cost_summary": {
                        "cost_adjusted_edge_mean": 4.2,
                        "full_fill_ratio": 1.0,
                    }
                }
            },
        }
    }

    with patch(
        "aats.data_platform.operations.strategy_tuning_review.collect_phase4_evidence",
        return_value=phase4,
    ):
        result = build_strategy_tuning_review(tmp_path, save_results=True)

    assert result["proposal_count"] == 1
    proposal = result["proposals"][0]
    assert proposal["parameter"] == "min_safe_net_edge_bps"
    assert proposal["status"] == "pending_review"
    assert proposal["review_required"] is True
    assert result["recommend_cost_gate_reassessment"] is False

    registry = load_strategy_tuning_registry(tmp_path)
    assert len(registry["proposals"]) == 1
    assert registry["proposals"][0]["status"] == "pending_review"


def test_strategy_tuning_review_supersedes_old_pending_proposal(tmp_path: Path) -> None:
    phase4 = {
        "latest_round": {
            "round_id": "phase4_1",
            "combos": {
                "independent_15m": {
                    "cost_summary": {
                        "cost_adjusted_edge_mean": 4.2,
                        "full_fill_ratio": 1.0,
                    }
                }
            },
        }
    }

    _seed_step2_round(
        tmp_path,
        "20260416_090000_aaaa1111",
        [
            {
                "family": "independent",
                "timeframe": "15m",
                "label": "combo_1",
                "opening_count": 0,
                "mean_cost_bps": 5.6,
                "mean_expected_edge_bps": 1.2,
                "execution_compatible_ratio": 0.0,
                "positive_edge_ratio": 0.8,
                "top_blocking_reason": "net_edge_below_safe_minimum",
            },
            {
                "family": "independent",
                "timeframe": "15m",
                "label": "combo_2",
                "opening_count": 0,
                "mean_cost_bps": 5.7,
                "mean_expected_edge_bps": 1.1,
                "execution_compatible_ratio": 0.0,
                "positive_edge_ratio": 0.7,
                "top_blocking_reason": "net_edge_below_safe_minimum",
            },
        ],
    )
    with patch(
        "aats.data_platform.operations.strategy_tuning_review.collect_phase4_evidence",
        return_value=phase4,
    ):
        first = build_strategy_tuning_review(tmp_path, save_results=True)

    _seed_step2_round(
        tmp_path,
        "20260416_100000_bbbb2222",
        [
            {
                "family": "independent",
                "timeframe": "15m",
                "label": "combo_1",
                "opening_count": 20,
                "mean_cost_bps": 5.5,
                "mean_expected_edge_bps": 2.2,
                "execution_compatible_ratio": 0.4,
                "positive_edge_ratio": 0.8,
                "top_blocking_reason": "score_not_stable",
            },
            {
                "family": "independent",
                "timeframe": "15m",
                "label": "combo_2",
                "opening_count": 18,
                "mean_cost_bps": 5.6,
                "mean_expected_edge_bps": 2.1,
                "execution_compatible_ratio": 0.3,
                "positive_edge_ratio": 0.75,
                "top_blocking_reason": "score_not_stable",
            },
        ],
    )
    with patch(
        "aats.data_platform.operations.strategy_tuning_review.collect_phase4_evidence",
        return_value=phase4,
    ):
        second = build_strategy_tuning_review(tmp_path, save_results=True)

    registry = load_strategy_tuning_registry(tmp_path)
    statuses = {item["proposal_id"]: item["status"] for item in registry["proposals"]}
    assert len(registry["proposals"]) == 2
    assert statuses[first["proposals"][0]["proposal_id"]] == "superseded"
    assert statuses[second["proposals"][0]["proposal_id"]] == "pending_review"
    assert second["proposals"][0]["parameter"] == "score_stability_threshold"


def test_strategy_tuning_proposal_review_changes_status(tmp_path: Path) -> None:
    _seed_step2_round(
        tmp_path,
        "20260416_090000_aaaa1111",
        [
            {
                "family": "independent",
                "timeframe": "15m",
                "label": "combo_1",
                "opening_count": 0,
                "mean_cost_bps": 5.6,
                "mean_expected_edge_bps": 1.2,
                "execution_compatible_ratio": 0.0,
                "positive_edge_ratio": 0.8,
                "top_blocking_reason": "net_edge_below_safe_minimum",
            }
        ],
    )
    phase4 = {
        "latest_round": {
            "round_id": "phase4_1",
            "combos": {
                "independent_15m": {
                    "cost_summary": {
                        "cost_adjusted_edge_mean": 4.2,
                        "full_fill_ratio": 1.0,
                    }
                }
            },
        }
    }

    with patch(
        "aats.data_platform.operations.strategy_tuning_review.collect_phase4_evidence",
        return_value=phase4,
    ):
        result = build_strategy_tuning_review(tmp_path, save_results=True)

    proposal_id = result["proposals"][0]["proposal_id"]
    review = review_strategy_tuning_proposal(
        tmp_path,
        proposal_id=proposal_id,
        action="approve",
        reviewer="operator",
        notes="同意进入下一轮研究复核",
    )

    assert review["ok"] is True
    assert review["proposal"]["status"] == "approved"
    assert review["proposal"]["reviewed_by"] == "operator"
    assert review["overrides_path"]
    assert get_combo_tuning_overrides(tmp_path, "independent", "15m") == {
        "min_safe_net_edge_bps": 1.5,
    }
