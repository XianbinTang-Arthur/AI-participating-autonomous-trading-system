from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from aats.data_platform.production_workflow.release_cycle import (
    _select_release_candidates,
    run_release_cycle,
)


def test_select_release_candidates_dedupes_by_combo_and_skips_existing_release() -> None:
    registry = {
        "recommendations": [
            {
                "recommendation_id": "rec_old",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T08:00:00+00:00",
                "target_parameter_set_id": "ps_old",
            },
            {
                "recommendation_id": "rec_new",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T09:00:00+00:00",
                "target_parameter_set_id": "ps_new",
            },
            {
                "recommendation_id": "rec_keep",
                "family": "directional",
                "timeframe": "1H",
                "recommendation_type": "keep_active",
                "status": "approved",
                "approved_at": "2026-04-16T09:00:00+00:00",
                "target_parameter_set_id": None,
            },
            {
                "recommendation_id": "rec_missing_ps",
                "family": "directional",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T09:00:00+00:00",
                "target_parameter_set_id": None,
            },
            {
                "recommendation_id": "rec_existing_release",
                "family": "directional",
                "timeframe": "1H",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T09:00:00+00:00",
                "target_parameter_set_id": "ps_existing",
            },
        ],
    }
    release_history = {
        "releases": [
            {
                "release_id": "rel_1",
                "recommendation_id": "rec_existing_release",
                "apply_result": "success",
            }
        ]
    }

    result = _select_release_candidates(registry, release_history)

    assert result["reviewed_count"] == 5
    assert [item["recommendation_id"] for item in result["eligible"]] == ["rec_new"]
    skipped_ids = {item["recommendation_id"] for item in result["skipped"]}
    assert skipped_ids == {"rec_old", "rec_keep", "rec_missing_ps", "rec_existing_release"}


def test_select_release_candidates_retries_after_blocked_by_gate() -> None:
    """被 gate 拦下或失败的 recommendation 必须可以重新进入下一轮 release cycle，
    否则会出现 UI 显示为 pending 但 release_cycle 永远跳过的死锁。"""
    registry = {
        "recommendations": [
            {
                "recommendation_id": "rec_retry_after_gate",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T09:00:00+00:00",
                "target_parameter_set_id": "ps_retry",
            },
            {
                "recommendation_id": "rec_retry_after_failure",
                "family": "directional",
                "timeframe": "1H",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T10:00:00+00:00",
                "target_parameter_set_id": "ps_retry_fail",
            },
        ],
    }
    release_history = {
        "releases": [
            {
                "release_id": "rel_gate_blocked",
                "recommendation_id": "rec_retry_after_gate",
                "apply_result": "blocked_by_gate",
            },
            {
                "release_id": "rel_failed",
                "recommendation_id": "rec_retry_after_failure",
                "apply_result": "failed",
            },
        ],
    }

    result = _select_release_candidates(registry, release_history)

    eligible_ids = {item["recommendation_id"] for item in result["eligible"]}
    assert eligible_ids == {"rec_retry_after_gate", "rec_retry_after_failure"}
    skipped_ids = {item["recommendation_id"] for item in result["skipped"]}
    assert skipped_ids == set()


def test_run_release_cycle_dry_run_has_no_release_side_effects(tmp_path: Path) -> None:
    registry = {
        "recommendations": [
            {
                "recommendation_id": "rec_1",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T09:00:00+00:00",
                "target_parameter_set_id": "ps_1",
            }
        ]
    }

    with (
        patch(
            "aats.data_platform.production_workflow.release_cycle.load_recommendation_registry",
            return_value=registry,
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle.load_release_history",
            return_value={"releases": []},
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle.get_current_environment",
            return_value="dev",
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle.create_parameter_release",
        ) as create_release_mock,
    ):
        result = run_release_cycle(tmp_path, dry_run=True, save_results=False)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["results"][0]["outcome"] == "dry_run"
    create_release_mock.assert_not_called()


def test_run_release_cycle_treats_blocked_by_gate_as_non_failure(tmp_path: Path) -> None:
    registry = {
        "recommendations": [
            {
                "recommendation_id": "rec_1",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T09:00:00+00:00",
                "target_parameter_set_id": "ps_1",
            }
        ]
    }
    release_result = {
        "ok": True,
        "message": "gate blocked apply",
        "release": {
            "release_id": "rel_1",
            "apply_result": "blocked_by_gate",
            "gate_status": "block",
        },
    }

    with (
        patch(
            "aats.data_platform.production_workflow.release_cycle.load_recommendation_registry",
            return_value=registry,
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle.load_release_history",
            return_value={"releases": []},
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle.get_current_environment",
            return_value="dev",
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle.create_parameter_release",
            return_value=release_result,
        ),
    ):
        result = run_release_cycle(tmp_path, dry_run=False, save_results=False)

    assert result["ok"] is True
    assert result["blocked_count"] == 1
    assert result["failed_count"] == 0
    assert result["results"][0]["outcome"] == "blocked_by_gate"
