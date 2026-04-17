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
    round_dir = root / "artifacts/research/step2_rounds" / round_id
    # round_manifest.json 必须存在，否则 snapshot 会被 is_snapshot_incomplete() 标记，
    # build_strategy_tuning_review 会按无 step2 数据处理（防止半成品目录驱动自动调优）。
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": round_id,
            "overall_status": "completed",
            "started_at": "2026-04-16T09:00:00Z",
            "finished_at": "2026-04-16T09:05:00Z",
        },
    )
    _write_json(
        round_dir / "scan_comparison_summary.json",
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


def test_strategy_tuning_review_refuses_incomplete_step2_snapshot(tmp_path: Path) -> None:
    """回归：缺 round_manifest.json 的 Step2 目录不得驱动自动调优输出 recommendation。

    曾经的 bug：scan_comparison_summary.json 存在就被当成最新 Step2 round 喂入
    调优链，函数即使不生成 proposal，也会产出 step2_round_id 与一个基于空 rows
    的占位 global_recommendation（"inspect_signal_generation"），误导 operator 以
    为自动调优已经基于最新数据分析过。
    """
    round_dir = tmp_path / "artifacts/research/step2_rounds/20260416_090000_nomanif"
    # 故意不写 round_manifest.json
    _write_json(
        round_dir / "scan_comparison_summary.json",
        {
            "comparison": [
                {
                    "family": "independent",
                    "timeframe": "15m",
                    "label": "combo_incomplete",
                    "opening_count": 0,
                    "mean_cost_bps": 5.6,
                    "mean_expected_edge_bps": 1.2,
                    "execution_compatible_ratio": 0.0,
                    "positive_edge_ratio": 0.8,
                    "top_blocking_reason": "net_edge_below_safe_minimum",
                },
            ],
        },
    )

    with patch(
        "aats.data_platform.operations.strategy_tuning_review.collect_phase4_evidence",
        return_value={"latest_round": {"combos": {}}},
    ):
        result = build_strategy_tuning_review(tmp_path, save_results=False)

    assert result["step2_round_id"] is None, (
        "缺 round_manifest.json 的 Step2 目录不能作为最新轮次暴露给运营者"
    )
    assert result["step2_incomplete_reason"] == "manifest_missing_on_disk"
    assert result["global_recommendation"] == "insufficient_data", (
        "没有可信 Step2 rows 时必须降级为 insufficient_data，"
        "不能用 combo 默认 focus 伪装出一个 recommendation"
    )
    assert result["proposal_count"] == 0


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
    # P0-3 后 JSON 导出默认关闭 → overrides_path 可以是空字符串；
    # 真正有价值的断言是下面 get_combo_tuning_overrides 能从 cache 读出来。
    assert isinstance(review["overrides_path"], str)
    assert get_combo_tuning_overrides(tmp_path, "independent", "15m") == {
        "min_safe_net_edge_bps": 1.5,
    }
