from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aats.data_platform.production_workflow.release_cycle import (
    _select_release_candidates,
    run_release_cycle,
)


def _qualified_verdicts(registry: dict) -> dict[str, SimpleNamespace]:
    return {
        str(item["recommendation_id"]): SimpleNamespace(
            eligible=True,
            reason_code="qualified",
            to_dict=lambda: {"eligible": True, "reason_code": "qualified"},
        )
        for item in registry.get("recommendations", [])
        if item.get("recommendation_id")
    }


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

    result = _select_release_candidates(
        registry,
        release_history,
        qualification_verdicts=_qualified_verdicts(registry),
    )

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

    result = _select_release_candidates(
        registry,
        release_history,
        qualification_verdicts=_qualified_verdicts(registry),
    )

    eligible_ids = {item["recommendation_id"] for item in result["eligible"]}
    assert eligible_ids == {"rec_retry_after_gate", "rec_retry_after_failure"}
    skipped_ids = {item["recommendation_id"] for item in result["skipped"]}
    assert skipped_ids == set()


def test_select_release_candidates_quarantines_pending_or_unknown_outcome() -> None:
    registry = {
        "recommendations": [
            {
                "recommendation_id": recommendation_id,
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-04-16T09:00:00+00:00",
                "target_parameter_set_id": f"ps_{index}",
            }
            for index, recommendation_id in enumerate(
                ("rec_pending", "rec_unknown"),
                start=1,
            )
        ]
    }
    release_history = {
        "releases": [
            {
                "release_id": "rel_pending",
                "recommendation_id": "rec_pending",
                "apply_result": "pending",
            },
            {
                "release_id": "rel_unknown",
                "recommendation_id": "rec_unknown",
                "apply_result": None,
            },
        ]
    }

    result = _select_release_candidates(
        registry,
        release_history,
        qualification_verdicts=_qualified_verdicts(registry),
    )

    assert result["eligible"] == []
    assert {item["recommendation_id"] for item in result["skipped"]} == {
        "rec_pending",
        "rec_unknown",
    }
    assert all("reconciliation" in item["detail"] for item in result["skipped"])


def test_same_instant_approvals_are_ambiguous_regardless_of_input_order() -> None:
    recommendations = [
        {
            "recommendation_id": "rec_a",
            "family": "independent",
            "timeframe": "15m",
            "recommendation_type": "parameter_upgrade",
            "status": "approved",
            "approved_at": "2026-08-27T12:00:00Z",
            "created_at": "2026-08-27T11:00:00Z",
            "target_parameter_set_id": "ps_a",
        },
        {
            "recommendation_id": "rec_b",
            "family": "independent",
            "timeframe": "15m",
            "recommendation_type": "parameter_upgrade",
            "status": "approved",
            "approved_at": "2026-08-27T12:00:00+00:00",
            "created_at": "2026-08-27T11:00:00+00:00",
            "target_parameter_set_id": "ps_b",
        },
    ]
    for ordered in (recommendations, list(reversed(recommendations))):
        registry = {"recommendations": ordered}
        result = _select_release_candidates(
            registry,
            {"releases": []},
            qualification_verdicts=_qualified_verdicts(registry),
        )
        assert result["eligible"] == []
        assert {
            item.get("reason_code") for item in result["skipped"]
        } == {"ambiguous_approval_order"}


def test_ineligible_latest_approval_never_falls_back_to_older_target() -> None:
    registry = {
        "recommendations": [
            {
                "recommendation_id": "rec_old_qualified",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-08-27T10:00:00Z",
                "target_parameter_set_id": "ps_old",
            },
            {
                "recommendation_id": "rec_new_ineligible",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-08-27T11:00:00Z",
                "target_parameter_set_id": "ps_new",
            },
        ]
    }
    verdicts = _qualified_verdicts(registry)
    verdicts["rec_new_ineligible"] = SimpleNamespace(
        eligible=False,
        reason_code="legacy_evidence_unbound",
        to_dict=lambda: {
            "eligible": False,
            "reason_code": "legacy_evidence_unbound",
        },
    )
    result = _select_release_candidates(
        registry,
        {"releases": []},
        qualification_verdicts=verdicts,
    )
    assert result["eligible"] == []
    assert any(
        item.get("recommendation_id") == "rec_new_ineligible"
        and item.get("reason_code") == "legacy_evidence_unbound"
        for item in result["skipped"]
    )


def test_combo_pending_release_blocks_fallback_to_other_approved_rec() -> None:
    registry = {
        "recommendations": [
            {
                "recommendation_id": "rec_old",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-08-27T10:00:00Z",
                "target_parameter_set_id": "ps_old",
            },
            {
                "recommendation_id": "rec_new_pending",
                "family": "independent",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "approved_at": "2026-08-27T11:00:00Z",
                "target_parameter_set_id": "ps_new",
            },
        ]
    }
    result = _select_release_candidates(
        registry,
        {
            "releases": [
                {
                    "release_id": "rel_pending",
                    "recommendation_id": "rec_new_pending",
                    "family": "independent",
                    "timeframe": "15m",
                    "apply_result": "pending",
                }
            ]
        },
        qualification_verdicts=_qualified_verdicts(registry),
    )
    assert result["eligible"] == []
    assert all(
        item.get("reason_code") == "combo_release_reconciliation_required"
        for item in result["skipped"]
    )


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
            "aats.data_platform.production_workflow.release_cycle."
            "evaluate_promotion_qualifications",
            return_value=_qualified_verdicts(registry),
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

    class _Engine:
        def dispose(self) -> None:
            return None

    class _LockSession:
        def __init__(self, _engine: object) -> None:
            return None

        def close(self) -> None:
            return None

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
            "aats.data_platform.production_workflow.release_cycle."
            "evaluate_promotion_qualifications",
            return_value=_qualified_verdicts(registry),
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle.create_parameter_release",
            return_value=release_result,
        ),
        patch(
            "aats.data_platform.governance._db_util.try_governance_db",
            return_value=(_Engine(), True),
        ),
        patch("sqlalchemy.orm.Session", _LockSession),
        patch(
            "aats.data_platform.governance.operational_state_db."
            "try_acquire_release_cycle_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.operational_state_db."
            "release_release_cycle_lock",
        ),
    ):
        result = run_release_cycle(tmp_path, dry_run=False, save_results=False)

    assert result["ok"] is True
    assert result["blocked_count"] == 1
    assert result["failed_count"] == 0
    assert result["results"][0]["outcome"] == "blocked_by_gate"


def test_release_cycle_never_runs_without_governance_lock(tmp_path: Path) -> None:
    with (
        patch(
            "aats.data_platform.production_workflow.release_cycle."
            "get_current_environment",
            return_value="dev",
        ),
        patch(
            "aats.data_platform.governance._db_util.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle."
            "create_parameter_release",
        ) as create_release_mock,
    ):
        result = run_release_cycle(tmp_path, dry_run=False, save_results=False)

    assert result["ok"] is False
    assert "锁不可用" in result["error"]
    create_release_mock.assert_not_called()


@pytest.mark.parametrize("dry_run", [True, False])
def test_release_cycle_reports_legacy_approved_as_audit_only(
    tmp_path: Path,
    dry_run: bool,
) -> None:
    registry = {
        "recommendations": [
            {
                "recommendation_id": "rec_legacy_missing_round",
                "family": "independent",
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "15m",
                "recommendation_type": "parameter_upgrade",
                "status": "approved",
                "target_parameter_set_id": "ps_legacy",
            }
        ]
    }
    verdicts = {
        "rec_legacy_missing_round": SimpleNamespace(
            eligible=False,
            reason_code="source_round_id_required",
            to_dict=lambda: {
                "eligible": False,
                "reason_code": "source_round_id_required",
            },
        )
    }

    class _Engine:
        def dispose(self) -> None:
            return None

    class _LockSession:
        def __init__(self, _engine: object) -> None:
            return None

        def close(self) -> None:
            return None

    with (
        patch(
            "aats.data_platform.production_workflow.release_cycle."
            "load_recommendation_registry",
            return_value=registry,
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle.load_release_history",
            return_value={"releases": []},
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle."
            "evaluate_promotion_qualifications",
            return_value=verdicts,
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle."
            "get_current_environment",
            return_value="dev",
        ),
        patch(
            "aats.data_platform.governance._db_util.try_governance_db",
            return_value=(_Engine(), True),
        ),
        patch("sqlalchemy.orm.Session", _LockSession),
        patch(
            "aats.data_platform.governance.operational_state_db."
            "try_acquire_release_cycle_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.operational_state_db."
            "release_release_cycle_lock",
        ),
        patch(
            "aats.data_platform.production_workflow.release_cycle."
            "create_parameter_release",
        ) as create_release_mock,
    ):
        result = run_release_cycle(
            tmp_path,
            dry_run=dry_run,
            save_results=False,
        )

    assert result["eligible_count"] == 0
    assert result["selected_count"] == 0
    assert result["results"][0]["outcome"] == "audit_only"
    assert result["results"][0]["reason_code"] == "source_round_id_required"
    create_release_mock.assert_not_called()
