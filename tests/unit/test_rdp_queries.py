from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import aats.services.operator.rdp_queries as rdp_queries


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _valid_promotion_readiness_round(
    *,
    ready: bool = True,
    now: datetime | None = None,
) -> dict:
    from aats.data_platform.decision_system.evidence_bundle import (
        PHASE2_PROMOTION_QUALIFICATION_POLICY,
    )

    now = now or datetime.now(timezone.utc)
    started_at = now - timedelta(minutes=5)
    generated_at = now - timedelta(minutes=1)
    round_id = "20260827_120000_deadbeef"
    check_names = (
        "research_stability",
        "attribution_no_severe_issue",
        "execution_not_severe",
        "governance_healthy",
        "parameter_traceable",
        "has_promote_candidate",
        "has_keep_active_ft",
    )
    checks = [
        {
            "check": name,
            "passed": ready or index != 0,
            "detail": f"{name}=fixture",
        }
        for index, name in enumerate(check_names)
    ]
    checks_passed = sum(1 for check in checks if check["passed"])
    blockers = [] if ready else ["research_stability fixture blocker"]
    readiness = (
        "ready_for_next_live_test"
        if ready
        else "not_ready_more_research_needed"
    )
    return {
        "available": True,
        "round_id": round_id,
        "started_at": started_at.isoformat(),
        "finished_at": now.isoformat(),
        "manifest": {
            "round_id": round_id,
            "phase": "phase6",
            "status": "succeeded",
            "started_at": started_at.isoformat(),
            "finished_at": now.isoformat(),
            "readiness": readiness,
            "upgrade_candidates_count": 1,
            "ft_decisions_count": 1,
        },
        "evidence_bundle_summary": {
            "phase2_evidence": {
                "promotion_qualification_policy": (
                    PHASE2_PROMOTION_QUALIFICATION_POLICY
                ),
            },
        },
        "parameter_upgrade_candidates": [
            {
                "parameter_set_id": "ps_001",
                "decision": "promote_candidate",
                "score_ratio": 0.9,
            },
        ],
        "family_timeframe_decisions": [
            {
                "combo_key": "independent_1h",
                "decision": "keep_active",
                "confidence": "high",
            },
        ],
        "promotion_readiness_assessment": {
            "generated_at": generated_at.isoformat(),
            "readiness": readiness,
            "overall_confidence": "high" if ready else "medium",
            "checks_total": len(checks),
            "checks_passed": checks_passed,
            "checks_failed": len(checks) - checks_passed,
            "blockers": blockers,
            "checks": checks,
            "promoted_candidates": [
                {"parameter_set_id": "ps_001", "score_ratio": 0.9},
            ],
            "active_family_timeframes": [
                {"combo_key": "independent_1h", "confidence": "high"},
            ],
        },
    }


def test_query_latest_decision_round_prefers_db_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    round_dir = tmp_path / "artifacts" / "decision_rounds" / "20260401_000000_deadbeef"
    round_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        rdp_queries,
        "_load_latest_decision_round_from_db",
        lambda _root=None: {
            "available": True,
            "data_source": "db",
            "round_id": "20260415_190910_d412ef64",
            "round_dir": None,
            "started_at": "2026-04-15T19:07:32+00:00",
            "finished_at": "2026-04-15T19:09:10+00:00",
            "parameter_upgrade_candidates": [{"combo_key": "directional_1h"}],
            "family_timeframe_decisions": [{"combo_key": "directional_1h"}],
            "promotion_readiness_assessment": {"overall_status": "blocked"},
            "has_conclusion_report": True,
        },
    )

    result = rdp_queries.query_latest_decision_round(tmp_path)

    assert result["available"] is True
    assert result["data_source"] == "db"
    assert result["round_id"] == "20260415_190910_d412ef64"
    assert result["parameter_upgrade_candidates"][0]["combo_key"] == "directional_1h"


def test_query_latest_decision_round_file_fallback_supports_real_output_filenames(
    tmp_path: Path,
    monkeypatch,
) -> None:
    round_dir = tmp_path / "artifacts" / "decision_rounds" / "20260415_190910_d412ef64"
    round_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": "20260415_190910_d412ef64",
            "started_at": "2026-04-15T19:07:32+00:00",
            "finished_at": "2026-04-15T19:09:10+00:00",
        },
    )
    _write_json(round_dir / "evidence_summary.json", {"summary": "ok"})
    _write_json(round_dir / "parameter_upgrade_candidates.json", [{"combo_key": "directional_1h"}])
    _write_json(round_dir / "family_timeframe_decisions.json", [{"combo_key": "directional_1h"}])
    _write_json(round_dir / "promotion_readiness_report.json", {"overall_status": "blocked"})
    (round_dir / "phase6_closed_loop_decision_conclusion.md").write_text(
        "# conclusion\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        rdp_queries,
        "_load_latest_decision_round_from_db",
        lambda _root=None: None,
    )

    result = rdp_queries.query_latest_decision_round(tmp_path)

    assert result["available"] is True
    assert result["data_source"] == "file"
    assert result["round_id"] == "20260415_190910_d412ef64"
    assert result["evidence_bundle_summary"] == {"summary": "ok"}
    assert result["promotion_readiness_assessment"] == {"overall_status": "blocked"}
    assert result["has_conclusion_report"] is True


def test_db_empty_is_authoritative_and_stale_round_cannot_restore_readiness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aats.data_platform.decision_system.evidence_bundle import (
        PHASE2_PROMOTION_QUALIFICATION_POLICY,
    )
    from aats.data_platform.governance import _db_util, decision_rounds_db

    round_dir = tmp_path / "artifacts/decision_rounds/20260827_120000_deadbeef"
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": round_dir.name,
            "started_at": "2026-08-27T12:00:00+00:00",
            "finished_at": "2026-08-27T12:01:00+00:00",
        },
    )
    _write_json(
        round_dir / "evidence_summary.json",
        {
            "phase2_evidence": {
                "promotion_qualification_policy": (
                    PHASE2_PROMOTION_QUALIFICATION_POLICY
                )
            }
        },
    )
    _write_json(
        round_dir / "promotion_readiness_report.json",
        {"readiness": "ready_for_next_live_test"},
    )

    class _Engine:
        def dispose(self) -> None: ...

    class _Session:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_Session": return self
        def __exit__(self, *_args: object) -> None: ...

    monkeypatch.setattr(_db_util, "try_governance_db", lambda: (_Engine(), True))
    monkeypatch.setattr(
        decision_rounds_db,
        "db_load_latest_decision_round_snapshot",
        lambda _session: None,
    )
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _Session)

    latest = rdp_queries.query_latest_decision_round(tmp_path)
    readiness = rdp_queries.query_promotion_readiness(tmp_path)
    augmented = rdp_queries._augment_workflow_runs_with_decision_round({}, tmp_path)

    assert latest["available"] is False
    assert latest["data_source"] == "db"
    assert latest["reason_code"] == "decision_round_db_empty"
    assert readiness == {
        "available": False,
        "audit_only": True,
        "data_source": "db",
        "reason_code": "decision_round_db_empty",
    }
    assert augmented == {}


def test_db_query_error_is_authoritative_and_does_not_fallback_to_round_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aats.data_platform.governance import _db_util, decision_rounds_db

    round_dir = tmp_path / "artifacts/decision_rounds/20260827_120000_deadbeef"
    _write_json(round_dir / "round_manifest.json", {"round_id": round_dir.name})

    class _Engine:
        def dispose(self) -> None: ...

    class _Session:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_Session": return self
        def __exit__(self, *_args: object) -> None: ...

    monkeypatch.setattr(_db_util, "try_governance_db", lambda: (_Engine(), True))
    monkeypatch.setattr(
        decision_rounds_db,
        "db_load_latest_decision_round_snapshot",
        lambda _session: (_ for _ in ()).throw(RuntimeError("synthetic query error")),
    )
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _Session)

    latest = rdp_queries.query_latest_decision_round(tmp_path)

    assert latest["available"] is False
    assert latest["data_source"] == "db"
    assert latest["reason_code"] == "decision_round_db_error"


def test_query_promotion_readiness_marks_legacy_round_audit_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = _valid_promotion_readiness_round()
    payload["evidence_bundle_summary"] = {
        "phase2_evidence": {"global_stats": {"available": True}},
    }
    monkeypatch.setattr(
        rdp_queries,
        "query_latest_decision_round",
        lambda _root: payload,
    )

    result = rdp_queries.query_promotion_readiness(tmp_path)

    assert result["available"] is False
    assert result["audit_only"] is True
    assert result["reason_code"] == "promotion_qualification_policy_unsupported"


def test_latest_recommendations_does_not_resurrect_file_after_truth_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aats.data_platform.governance._exceptions import DBUnavailableError

    rec_path = tmp_path / "artifacts/decision_system/recommendation_registry.json"
    _write_json(
        rec_path,
        {
            "recommendations": [
                {
                    "recommendation_id": "rec_stale_approved",
                    "status": "approved",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
        lambda _path: (_ for _ in ()).throw(
            DBUnavailableError("managed recommendation truth unavailable")
        ),
    )

    result = rdp_queries.query_latest_recommendations(tmp_path)

    assert result["available"] is False
    assert result["total_count"] == 0
    assert result["recommendations"] == []
    assert result["data_source"] == "db"
    assert result["audit_only"] is True
    assert result["reason_code"] == "recommendation_db_unavailable"


def test_query_promotion_readiness_requires_current_qualification_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = _valid_promotion_readiness_round(ready=False)
    monkeypatch.setattr(
        rdp_queries,
        "query_latest_decision_round",
        lambda _root: payload,
    )

    result = rdp_queries.query_promotion_readiness(tmp_path)

    assert result["available"] is True
    assert result["audit_only"] is False
    assert result["assessment"]["readiness"] == "not_ready_more_research_needed"


@pytest.mark.parametrize(
    ("round_change", "reason_code"),
    [
        ({"manifest": {"status": "failed"}}, "promotion_readiness_round_status_invalid"),
        ({"manifest": None}, "promotion_readiness_manifest_invalid"),
        (
            {"finished_at": "stale"},
            "promotion_readiness_round_stale",
        ),
        (
            {"finished_at": "future"},
            "promotion_readiness_finished_at_future",
        ),
        (
            {"manifest": {"round_id": "20260827_120001_deadbeef"}},
            "promotion_readiness_round_id_mismatch",
        ),
        (
            {"manifest": {"phase": "phase5"}},
            "promotion_readiness_round_phase_invalid",
        ),
    ],
)
def test_query_promotion_readiness_exposes_only_current_complete_rounds(
    tmp_path: Path,
    monkeypatch,
    round_change: dict,
    reason_code: str,
) -> None:
    now = datetime.now(timezone.utc)
    payload = _valid_promotion_readiness_round(now=now)
    round_id = payload["round_id"]
    manifest = payload["manifest"]
    for key, value in round_change.items():
        if key == "manifest" and isinstance(value, dict):
            manifest.update(value)
        elif key == "finished_at" and value == "stale":
            changed_finished_at = now - timedelta(hours=168, seconds=1)
            changed_started_at = changed_finished_at - timedelta(minutes=5)
            payload[key] = changed_finished_at.isoformat()
            manifest[key] = changed_finished_at.isoformat()
            payload["started_at"] = changed_started_at.isoformat()
            manifest["started_at"] = changed_started_at.isoformat()
        elif key == "finished_at" and value == "future":
            changed_finished_at = (now + timedelta(seconds=1)).isoformat()
            payload[key] = changed_finished_at
            manifest[key] = changed_finished_at
        else:
            payload[key] = value
    monkeypatch.setattr(
        rdp_queries,
        "query_latest_decision_round",
        lambda _root: payload,
    )

    result = rdp_queries.query_promotion_readiness(tmp_path)

    assert result["available"] is False
    assert result["audit_only"] is True
    assert result["round_id"] == round_id
    assert result["reason_code"] == reason_code


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("empty", "promotion_readiness_assessment_invalid"),
        ("missing_field", "promotion_readiness_assessment_schema_invalid"),
        ("extra_field", "promotion_readiness_assessment_schema_invalid"),
        ("unknown_readiness", "promotion_readiness_assessment_schema_invalid"),
        (
            "manifest_readiness_mismatch",
            "promotion_readiness_assessment_manifest_mismatch",
        ),
        (
            "generated_outside_round",
            "promotion_readiness_assessment_identity_mismatch",
        ),
        ("wrong_check_order", "promotion_readiness_assessment_schema_invalid"),
        ("wrong_check_counts", "promotion_readiness_assessment_count_mismatch"),
        ("wrong_blocker_count", "promotion_readiness_assessment_count_mismatch"),
        (
            "promoted_exceeds_manifest",
            "promotion_readiness_manifest_count_mismatch",
        ),
        (
            "source_candidate_decision_mismatch",
            "promotion_readiness_assessment_count_mismatch",
        ),
        (
            "source_decision_count_mismatch",
            "promotion_readiness_manifest_count_mismatch",
        ),
        (
            "source_payload_invalid",
            "promotion_readiness_round_payload_invalid",
        ),
        ("manifest_count_type", "promotion_readiness_manifest_count_invalid"),
    ],
)
def test_query_promotion_readiness_rejects_malformed_assessment_contract(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
    reason_code: str,
) -> None:
    payload = deepcopy(_valid_promotion_readiness_round())
    assessment = payload["promotion_readiness_assessment"]
    manifest = payload["manifest"]

    if mutation == "empty":
        payload["promotion_readiness_assessment"] = {}
    elif mutation == "missing_field":
        assessment.pop("checks_failed")
    elif mutation == "extra_field":
        assessment["schema_version"] = "unrecognized/v99"
    elif mutation == "unknown_readiness":
        assessment["readiness"] = "ready_because_file_exists"
        manifest["readiness"] = assessment["readiness"]
    elif mutation == "manifest_readiness_mismatch":
        manifest["readiness"] = "not_ready_more_research_needed"
    elif mutation == "generated_outside_round":
        assessment["generated_at"] = (
            datetime.fromisoformat(payload["started_at"]) - timedelta(seconds=1)
        ).isoformat()
    elif mutation == "wrong_check_order":
        assessment["checks"][0], assessment["checks"][1] = (
            assessment["checks"][1],
            assessment["checks"][0],
        )
    elif mutation == "wrong_check_counts":
        assessment["checks_passed"] -= 1
        assessment["checks_failed"] += 1
    elif mutation == "wrong_blocker_count":
        assessment["blockers"] = ["不存在对应 failed check 的 blocker"]
    elif mutation == "promoted_exceeds_manifest":
        manifest["upgrade_candidates_count"] = 0
    elif mutation == "source_candidate_decision_mismatch":
        payload["parameter_upgrade_candidates"][0]["decision"] = "hold"
    elif mutation == "source_decision_count_mismatch":
        payload["family_timeframe_decisions"] = []
    elif mutation == "source_payload_invalid":
        payload["parameter_upgrade_candidates"] = {"not": "a list"}
    elif mutation == "manifest_count_type":
        manifest["ft_decisions_count"] = True
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)

    monkeypatch.setattr(
        rdp_queries,
        "query_latest_decision_round",
        lambda _root: payload,
    )

    result = rdp_queries.query_promotion_readiness(tmp_path)

    assert result == {
        "available": False,
        "round_id": payload["round_id"],
        "audit_only": True,
        "reason_code": reason_code,
    }


@pytest.mark.parametrize(
    "mutation",
    ["started_at_mismatch", "finished_at_mismatch", "started_after_finished"],
)
def test_query_promotion_readiness_binds_snapshot_and_manifest_timestamps(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    payload = deepcopy(_valid_promotion_readiness_round())
    manifest = payload["manifest"]
    if mutation == "started_at_mismatch":
        manifest["started_at"] = (
            datetime.fromisoformat(payload["started_at"]) - timedelta(seconds=1)
        ).isoformat()
    elif mutation == "finished_at_mismatch":
        manifest["finished_at"] = (
            datetime.fromisoformat(payload["finished_at"]) - timedelta(seconds=1)
        ).isoformat()
    else:
        changed_started_at = (
            datetime.fromisoformat(payload["finished_at"]) + timedelta(seconds=1)
        ).isoformat()
        payload["started_at"] = changed_started_at
        manifest["started_at"] = changed_started_at

    monkeypatch.setattr(
        rdp_queries,
        "query_latest_decision_round",
        lambda _root: payload,
    )

    result = rdp_queries.query_promotion_readiness(tmp_path)

    assert result["available"] is False
    assert result["audit_only"] is True
    assert result["reason_code"] == "promotion_readiness_round_timestamp_mismatch"


def test_operator_workflow_reader_reuses_gate_managed_truth_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aats.data_platform.production_workflow import gate_runtime_contract

    expected = {
        "data_maintenance": {
            "run_id": "logical_run_1",
            "attempt_no": 1,
            "workflow": "data_maintenance",
            "overall_status": "task_failed",
            "reconciliation_required": True,
        }
    }
    monkeypatch.setattr(
        gate_runtime_contract,
        "_collect_latest_workflow_runs",
        lambda root: expected if root == tmp_path else {},
    )

    assert rdp_queries._collect_latest_workflow_runs(tmp_path) == expected


def test_query_rdp_health_uses_canonical_workflow_truth_for_freshness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "aats.data_platform.operations.environment_guard.get_current_environment",
        lambda: "dev",
    )
    monkeypatch.setattr(
        rdp_queries,
        "_query_governance_runtime_state",
        lambda: {
            "connection_ok": True,
            "errors": [],
            "runtime_components": [],
            "task_queue": {
                "pending_count": 0,
                "running_count": 0,
                "failed_count": 0,
            },
        },
    )
    monkeypatch.setattr(rdp_queries, "_check_db_initialization", lambda *_args, **_kwargs: (True, True))
    monkeypatch.setattr(
        rdp_queries,
        "_collect_latest_workflow_runs",
        lambda _root: {
            workflow: {
                "workflow": workflow,
                "overall_status": "success",
                "finished_at": (now - timedelta(hours=1)).isoformat(),
            }
            for workflow in (
                "data_maintenance",
                "governance_cycle",
                "decision_cycle",
            )
        },
    )
    monkeypatch.setattr(
        rdp_queries,
        "_load_latest_decision_round_from_db",
        lambda _root=None: {
            "available": True,
            "data_source": "db",
            "round_id": "20260415_190910_d412ef64",
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "finished_at": (now - timedelta(hours=1)).isoformat(),
        },
    )
    monkeypatch.setattr(
        "aats.data_platform.live_query_adapter.check_live_db_health",
        lambda: {"healthy": True, "connection_ok": True, "tables_checked": {}, "errors": []},
    )
    monkeypatch.setattr(
        "aats.bootstrap.active_parameters.load_all_active_parameter_sets",
        lambda project_root: {"independent_15m": {"parameter_set_id": "ps_live_1"}},
    )

    health = rdp_queries.query_rdp_health(tmp_path)
    workflow_check = next(item for item in health["checks"] if item["category"] == "workflow_runs")

    assert workflow_check["status"] == "ok"
    assert "governance_cycle=ok:" in workflow_check["detail"]
    assert "decision_cycle=ok:" in workflow_check["detail"]
    assert "workflow_runs_stale_or_missing" not in health["warnings"]


def test_query_rdp_health_does_not_treat_historical_failures_as_queue_backlog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "aats.data_platform.operations.environment_guard.get_current_environment",
        lambda: "dev",
    )
    monkeypatch.setattr(
        rdp_queries,
        "_query_governance_runtime_state",
        lambda: {
            "connection_ok": True,
            "errors": [],
            "runtime_components": [],
            "task_queue": {
                "pending_count": 0,
                "running_count": 0,
                "failed_count": 31,
                "latest_failed_count": 0,
            },
        },
    )
    monkeypatch.setattr(rdp_queries, "_check_db_initialization", lambda *_args, **_kwargs: (True, True))
    monkeypatch.setattr(
        rdp_queries,
        "_collect_latest_workflow_runs",
        lambda _root: {
            "data_maintenance": {
                "workflow": "data_maintenance",
                "overall_status": "success",
                "finished_at": now.isoformat(),
            },
        },
    )
    monkeypatch.setattr(
        rdp_queries,
        "_load_latest_decision_round_from_db",
        lambda _root=None: {
            "available": True,
            "data_source": "db",
            "round_id": "20260826_044509_2e1f9967",
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
        },
    )
    monkeypatch.setattr(
        "aats.data_platform.live_query_adapter.check_live_db_health",
        lambda: {"healthy": True, "connection_ok": True, "tables_checked": {}, "errors": []},
    )
    monkeypatch.setattr(
        "aats.bootstrap.active_parameters.load_all_active_parameter_sets",
        lambda project_root: {"independent_15m": {"parameter_set_id": "ps_live_1"}},
    )

    health = rdp_queries.query_rdp_health(tmp_path)
    queue_check = next(item for item in health["checks"] if item["category"] == "task_queue")

    assert queue_check["status"] == "ok"
    assert "failed_history=31" in queue_check["detail"]
    assert "rdp_task_queue_backlog_or_failures" not in health["warnings"]


@pytest.mark.parametrize(
    ("status", "finished_at", "detail_fragment"),
    [
        ("partial", "now", "status:partial"),
        ("garbage", "now", "status:garbage"),
        ("success", None, "missing_finished_at"),
        ("success", "future", "future_finished_at"),
        ("success", "invalid", "invalid_finished_at"),
    ],
)
def test_query_rdp_health_blocks_non_exact_or_invalid_workflow_truth(
    tmp_path: Path,
    monkeypatch,
    status: str,
    finished_at: str | None,
    detail_fragment: str,
) -> None:
    now = datetime.now(timezone.utc)
    if finished_at == "now":
        bad_finished_at = now.isoformat()
    elif finished_at == "future":
        bad_finished_at = (now + timedelta(hours=1)).isoformat()
    elif finished_at == "invalid":
        bad_finished_at = "not-a-timestamp"
    else:
        bad_finished_at = None

    monkeypatch.setattr(
        "aats.data_platform.operations.environment_guard.get_current_environment",
        lambda: "prod",
    )
    monkeypatch.setattr(
        rdp_queries,
        "_query_governance_runtime_state",
        lambda: {
            "connection_ok": True,
            "errors": [],
            "runtime_components": [],
            "task_queue": {
                "pending_count": 0,
                "running_count": 0,
                "failed_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        rdp_queries,
        "_check_db_initialization",
        lambda *_args, **_kwargs: (True, True),
    )
    runs = {
        workflow: {
            "workflow": workflow,
            "overall_status": "success",
            "finished_at": now.isoformat(),
        }
        for workflow in ("data_maintenance", "governance_cycle", "decision_cycle")
    }
    runs["data_maintenance"]["overall_status"] = status
    runs["data_maintenance"]["finished_at"] = bad_finished_at
    monkeypatch.setattr(
        rdp_queries,
        "_collect_latest_workflow_runs",
        lambda _root: runs,
    )
    monkeypatch.setattr(
        "aats.data_platform.live_query_adapter.check_live_db_health",
        lambda: {
            "healthy": True,
            "connection_ok": True,
            "tables_checked": {},
            "errors": [],
        },
    )
    monkeypatch.setattr(
        "aats.bootstrap.active_parameters.load_all_active_parameter_sets",
        lambda project_root: {},
    )

    health = rdp_queries.query_rdp_health(tmp_path)
    workflow_check = next(
        item for item in health["checks"] if item["category"] == "workflow_runs"
    )

    assert workflow_check["status"] == "blocked"
    assert detail_fragment in workflow_check["detail"]
    assert "workflow_runs_stale_or_missing" in health["blocking_reasons"]
