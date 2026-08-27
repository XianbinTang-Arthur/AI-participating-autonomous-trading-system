from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from aats.api import rdp_workspace
from aats.api.auth import require_read_access
from aats.api.rdp_workspace_routes import rdp_workspace_router


def _iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _health(*, fresh: bool = True) -> dict[str, object]:
    return {
        "runtime_components": [
            {
                "component": "rdp-daemon",
                "status": "idle",
                "heartbeat_at": _iso(-5 if fresh else -120),
            }
        ]
    }


def _run(run_id: str, *, trigger_kind: str, eligible_at: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "workflow": "research_cycle",
        "status": "queued",
        "trigger_kind": trigger_kind,
        "eligible_at": eligible_at,
        "created_at": _iso(-60),
    }


def _qualified_release_candidate(recommendation_id: str) -> dict[str, object]:
    round_id = "20260827_120000_deadbeef"
    return {
        "combo_key": "independent_15m",
        "family": "independent",
        "timeframe": "15m",
        "recommendation_id": recommendation_id,
        "recommendation_type": "parameter_upgrade",
        "status": "approved",
        "target_parameter_set_id": "ps_1",
        "source_round_id": "research_round_1",
        "evidence_bundle_ref": round_id,
        "allow_apply": True,
        "promotion_qualification": {
            "required": True,
            "eligible": True,
            "reason_code": "qualified",
            "evidence_bundle_ref": round_id,
            "source_round_id": "research_round_1",
            "qualified_round_id": round_id,
        },
    }


def _release_control(
    *,
    decision_status: str = "keep_active",
    decision_truth_available: bool = True,
) -> dict[str, object]:
    return {
        "release_history_status": {"source": "db", "stale": False},
        "gate_history_status": {"source": "db", "available": True},
        "governance_state": {
            "decision_truth_available": decision_truth_available,
            "combo_states": [
                {
                    "combo_key": "independent_15m",
                    "decision_status": decision_status,
                    "decision_truth_available": decision_truth_available,
                }
            ],
        },
    }


def test_queue_position_matches_daemon_priority_and_backoff_truth() -> None:
    runs = {
        "items": [
            _run("run_future_recovery", trigger_kind="recovery", eligible_at=_iso(600)),
            _run("run_scheduled", trigger_kind="schedule", eligible_at=_iso(-30)),
            _run("run_retry", trigger_kind="auto_retry", eligible_at=_iso(-30)),
            _run("run_operator", trigger_kind="manual", eligible_at=_iso(-30)),
        ]
    }
    priorities = {
        "run_future_recovery": "operator_recovery",
        "run_scheduled": "scheduled",
        "run_retry": "retry",
        "run_operator": "operator",
    }
    control = {
        "tasks": {
            run_id: {
                "pending_task": {
                    "run_id": run_id,
                    "task_id": f"task_{run_id}",
                    "priority_class": priority,
                    "earliest_start_at": (
                        _iso(600) if run_id == "run_future_recovery" else _iso(-30)
                    ),
                    "requested_at": _iso(-60),
                }
            }
            for run_id, priority in priorities.items()
        }
    }

    projection = rdp_workspace._execution_projection(runs, _health(), control)

    assert [item["run_id"] for item in projection["queued_runs"]] == [
        "run_operator",
        "run_retry",
        "run_scheduled",
        "run_future_recovery",
    ]
    assert [item["queue_position"] for item in projection["queued_runs"]] == [1, 2, 3, 4]
    assert projection["queued_runs"][-1]["waiting_reason_code"] == "retry_backoff"


def test_queue_explains_busy_slot_and_detects_capacity_violation() -> None:
    runs = {
        "items": [
            {"run_id": "run_active_1", "workflow": "research_cycle", "status": "running"},
            {"run_id": "run_active_2", "workflow": "data_maintenance", "status": "running"},
            _run("run_waiting", trigger_kind="manual", eligible_at=_iso(-30)),
        ]
    }

    projection = rdp_workspace._execution_projection(runs, _health(), {})

    assert projection["active_count"] == 2
    assert projection["capacity_violation"] is True
    assert projection["queued_runs"][0]["waiting_reason_code"] == "execution_slot_busy"


def test_partial_success_requires_lifecycle_action() -> None:
    assert rdp_workspace._stage_status_from_run({"status": "partially_succeeded"}) == "action_required"
    assert rdp_workspace._stage_status_from_run({"status": "succeeded_with_warnings"}) == "action_required"
    assert rdp_workspace._stage_status_from_run({"status": "succeeded"}) == "complete"


def test_release_projection_never_enables_release_before_gate_pass() -> None:
    workbench = {
        "release_candidates": {
            "items": [
                {
                    **_qualified_release_candidate("rec_blocked"),
                    "created_at": "2026-08-25T10:00:00+00:00",
                        "gate_status": "block",
                        "actions": [
                            {
                                "key": "run_gate",
                                "ui_action": "rdp-run-gate",
                                "value": "rec_blocked",
                                "enabled": True,
                            },
                            {
                                "key": "create_release",
                                "ui_action": "rdp-create-release",
                                "value": "rec_blocked",
                                "enabled": True,
                            },
                    ],
                }
            ]
        }
    }

    projection = rdp_workspace._release_projection(
        _release_control(),
        workbench,
    )

    assert projection["selection_status"] == "no_eligible_candidate"
    release_action = projection["candidates"][0]["actions"][1]
    assert release_action["enabled"] is False
    assert "先运行并通过" in release_action["disabled_reason"]


def test_release_projection_requires_status_and_allow_apply_to_agree() -> None:
    candidate = {
        **_qualified_release_candidate("rec_gate_conflict"),
        "created_at": "2026-08-25T10:00:00+00:00",
        "gate_status": "pass",
        "allow_apply": False,
        "actions": [
            {
                "key": "create_release",
                "ui_action": "rdp-create-release",
                "value": "rec_gate_conflict",
                "enabled": True,
            }
        ],
    }

    projection = rdp_workspace._release_projection(
        _release_control(),
        {"release_candidates": {"items": [candidate]}},
    )

    assert projection["selection_status"] == "no_eligible_candidate"
    assert projection["candidates"][0]["actions"][0]["enabled"] is False


def test_release_projection_selects_latest_approved_gate_pass() -> None:
    def candidate(recommendation_id: str, created_at: str) -> dict[str, object]:
        return {
            **_qualified_release_candidate(recommendation_id),
            "created_at": created_at,
            "gate_status": "pass",
            "actions": [{
                "key": "create_release",
                "ui_action": "rdp-create-release",
                "value": recommendation_id,
                "enabled": True,
            }],
        }

    projection = rdp_workspace._release_projection(
        _release_control(),
        {
            "release_candidates": {
                "items": [
                    candidate("rec_old", "2026-08-25T10:00:00+00:00"),
                    candidate("rec_new", "2026-08-25T11:00:00+00:00"),
                ]
            }
        },
    )

    assert projection["selection_status"] == "eligible_for_release_review"
    assert projection["eligible_candidate"]["recommendation_id"] == "rec_new"


def test_release_projection_blocks_create_release_while_combo_is_paused() -> None:
    candidate = {
        **_qualified_release_candidate("rec_paused"),
        "gate_status": "pass",
        "actions": [{
            "key": "create_release",
            "ui_action": "rdp-create-release",
            "value": "rec_paused",
            "enabled": True,
        }],
    }

    projection = rdp_workspace._release_projection(
        _release_control(decision_status="pause"),
        {"release_candidates": {"items": [candidate]}},
    )

    assert projection["selection_status"] == "no_eligible_candidate"
    projected = projection["candidates"][0]
    assert projected["decision_status"] == "pause"
    assert projected["decision_allows_release"] is False
    assert projected["actions"][0]["enabled"] is False
    assert "暂停" in projected["actions"][0]["disabled_reason"]


def test_release_projection_blocks_create_release_when_decision_truth_unavailable() -> None:
    candidate = {
        **_qualified_release_candidate("rec_truth_unknown"),
        "gate_status": "pass",
        "actions": [{
            "key": "create_release",
            "ui_action": "rdp-create-release",
            "value": "rec_truth_unknown",
            "enabled": True,
        }],
    }

    projection = rdp_workspace._release_projection(
        _release_control(decision_truth_available=False),
        {"release_candidates": {"items": [candidate]}},
    )

    assert projection["selection_status"] == "no_eligible_candidate"
    projected = projection["candidates"][0]
    assert projected["decision_truth_available"] is False
    assert projected["actions"][0]["enabled"] is False
    assert "真源不可验证" in projected["actions"][0]["disabled_reason"]


def test_release_projection_fails_closed_when_release_truth_source_is_unknown() -> None:
    projection = rdp_workspace._release_projection(
        {
            **_release_control(),
            "release_history_status": {"source": "unknown", "stale": False},
        },
        {
            "release_candidates": {
                "items": [
                    {
                        **_qualified_release_candidate("rec_untrusted"),
                        "gate_status": "pass",
                        "actions": [{
                            "key": "create_release",
                            "ui_action": "rdp-create-release",
                            "value": "rec_untrusted",
                            "enabled": True,
                        }],
                    }
                ]
            }
        },
    )

    assert projection["selection_status"] == "no_eligible_candidate"
    assert projection["candidates"][0]["actions"][0]["enabled"] is False


def test_release_projection_rejects_legacy_candidate_even_with_persisted_gate_pass() -> None:
    projection = rdp_workspace._release_projection(
        _release_control(),
        {
            "release_candidates": {
                "items": [
                    {
                        "recommendation_id": "rec_legacy_gate_pass",
                        "recommendation_type": "parameter_upgrade",
                        "status": "approved",
                        "target_parameter_set_id": "ps_legacy",
                        "source_round_id": "research_round_legacy",
                        "evidence_bundle_ref": "round_legacy",
                        "gate_status": "pass",
                        "promotion_qualification": {
                            "required": True,
                            "eligible": True,
                            "reason_code": "promotion_round_not_found",
                            "source_round_id": "research_round_legacy",
                            "evidence_bundle_ref": "round_legacy",
                            "qualified_round_id": None,
                        },
                        "actions": [
                            {"key": "run_gate", "enabled": True},
                            {"key": "create_release", "enabled": True},
                        ],
                    }
                ]
            }
        },
    )

    assert projection["selection_status"] == "no_eligible_candidate"
    candidate = projection["candidates"][0]
    assert candidate["eligible_for_release_review"] is False
    assert candidate["actions"] == []


def test_audit_only_history_is_governance_work_and_exposes_supersede_only() -> None:
    workbench = {
        "items": [],
        "release_candidates": {
            "items": [],
            "audit_only_items": [
                {
                    **_qualified_release_candidate("rec_legacy_visible"),
                    "audit_only": True,
                    "created_at": "2026-08-27T12:00:00Z",
                    "actions": [
                        {
                            "key": "supersede",
                            "ui_action": "rdp-create-release",
                            "value": "rec_legacy_visible",
                            "enabled": True,
                        },
                        {
                            "key": "supersede",
                            "ui_action": "rdp-supersede-recommendation",
                            "value": "rec_legacy_visible",
                            "enabled": True,
                        },
                    ],
                }
            ],
        },
    }
    projection = rdp_workspace._release_projection(
        _release_control(),
        workbench,
    )

    assert projection["selection_status"] == "no_eligible_candidate"
    assert len(projection["candidates"]) == 1
    assert projection["candidates"][0]["audit_only"] is True
    assert [action["key"] for action in projection["candidates"][0]["actions"]] == [
        "supersede"
    ]
    lifecycle = rdp_workspace._lifecycle_projection(
        {}, {}, workbench, {}, {"active_run": None, "recent_runs": []},
    )
    governance = next(
        stage for stage in lifecycle["stages"] if stage["key"] == "governance"
    )
    release = next(
        stage for stage in lifecycle["stages"] if stage["key"] == "release"
    )
    assert lifecycle["current_stage"] == "governance"
    assert governance["status"] == "action_required"
    assert governance["evidence_count"] == 1
    assert release["status"] == "idle"
    next_action = rdp_workspace._next_action({}, lifecycle, {}, projection)
    assert next_action["ui_action"] == "rdp-supersede-recommendation"
    assert next_action["value"] == "rec_legacy_visible"


def test_workspace_route_maps_database_failure_to_retryable_503() -> None:
    app = FastAPI()
    app.include_router(rdp_workspace_router, prefix="/rdp")
    app.dependency_overrides[require_read_access] = lambda: object()
    failure = OperationalError("SELECT 1", {}, RuntimeError("database unavailable"))

    with patch("aats.api.rdp_workspace_routes.build_rdp_workspace", side_effect=failure):
        response = TestClient(app).get("/rdp/v3/workspace")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "governance_db_unavailable",
        "retryable": True,
    }


def test_data_governance_route_maps_settings_failure_to_retryable_503() -> None:
    app = FastAPI()
    app.include_router(rdp_workspace_router, prefix="/rdp")
    app.dependency_overrides[require_read_access] = lambda: object()

    with patch(
        "aats.api.rdp_workspace_routes.get_rdp_settings",
        side_effect=RuntimeError("settings unavailable"),
    ):
        response = TestClient(app).get("/rdp/v3/data-governance")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "data_governance_unavailable",
        "retryable": True,
    }


def test_workspace_derives_all_workbench_views_from_one_control_summary() -> None:
    request = object()
    control = {"health": _health(), "tasks": {}}
    bundle = {
        "overview": {},
        "workbench": {},
        "alerts": {},
        "tuning_overview": {},
        "tuning_proposals": {},
    }

    with (
        patch("aats.api.rdp_workspace._project_root") as project_root,
        patch("aats.api.rdp_workspace.build_rdp_control_summary", return_value=control) as build_control,
        patch("aats.api.rdp_workspace.build_rdp_workbench_bundle", return_value=bundle) as build_bundle,
        patch("aats.api.rdp_workspace.build_rdp_runs_panel", return_value={"items": []}),
        patch("aats.api.rdp_workspace._workflow_catalog", return_value=[]),
        patch(
            "aats.api.rdp_workspace.build_data_governance_snapshot",
            return_value={"schema_version": "rdp.data_governance.v1"},
        ) as build_data_governance,
    ):
        project_root.return_value = Path(".").resolve()
        payload = rdp_workspace.build_rdp_workspace(request)  # type: ignore[arg-type]

    assert payload["schema_version"] == "rdp.workspace.v3"
    assert payload["data_governance"]["schema_version"] == "rdp.data_governance.v1"
    build_control.assert_called_once_with(request)
    build_bundle.assert_called_once_with(request, control_summary=control)
    build_data_governance.assert_called_once_with(project_root.return_value)


def test_workspace_surfaces_governance_quarantine_as_blocked_alert() -> None:
    request = object()
    governance_state = {
        "available": True,
        "audit_only": True,
        "reason_code": "governance_quarantine",
        "db_load_failed": False,
        "quarantined_combos": {
            "independent_15m": "active_parameter_lineage_invalid"
        },
        "combo_states": [],
    }
    control = {
        "health": _health(),
        "tasks": {},
        "governance_state": governance_state,
    }
    bundle = {
        "overview": {},
        "workbench": {},
        "alerts": {},
        "tuning_overview": {},
        "tuning_proposals": {},
    }

    with (
        patch("aats.api.rdp_workspace._project_root", return_value=Path(".").resolve()),
        patch("aats.api.rdp_workspace.build_rdp_control_summary", return_value=control),
        patch("aats.api.rdp_workspace.build_rdp_workbench_bundle", return_value=bundle),
        patch("aats.api.rdp_workspace.build_rdp_runs_panel", return_value={"items": []}),
        patch("aats.api.rdp_workspace._workflow_catalog", return_value=[]),
        patch(
            "aats.api.rdp_workspace.build_data_governance_snapshot",
            return_value={"schema_version": "rdp.data_governance.v1"},
        ),
    ):
        payload = rdp_workspace.build_rdp_workspace(request)  # type: ignore[arg-type]

    assert payload["governance_state"] == governance_state
    assert payload["health"]["overall_health"] == "blocked"
    assert payload["lifecycle"]["overall_status"] == "blocked"
    stages = {item["key"]: item["status"] for item in payload["lifecycle"]["stages"]}
    assert stages["governance"] == "blocked"
    assert stages["release"] == "blocked"
    assert stages["runtime"] == "blocked"
    alert = payload["research"]["alerts"]["governance_alerts"][0]
    assert alert["code"] == "governance_quarantine"
    assert alert["blocks_approval"] is True
