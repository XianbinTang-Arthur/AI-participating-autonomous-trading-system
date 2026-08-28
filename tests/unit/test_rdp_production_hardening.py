from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aats.data_platform.decision_system.active_parameter_apply import (
    apply_approved_recommendation,
    rollback_active_parameter_set,
)
from aats.data_platform.production_workflow.gate_rules import (
    check_current_alerts,
    check_decision_consistency,
    check_evidence_freshness,
    check_live_db_health,
    check_quality_monitor_health,
    check_workflow_freshness,
)
from aats.data_platform.decision_system.promotion_qualification import (
    PromotionQualificationVerdict,
)
from aats.data_platform.production_workflow.pre_apply_gate import (
    build_gate_context,
    run_pre_apply_gate,
)
from aats.data_platform.production_workflow.release_registry import (
    create_parameter_release,
)
from aats.services.operator.rdp_queries import query_rdp_health


@pytest.fixture(autouse=True)
def _isolate_exact_round_qualification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy workflow tests focused on their original guard semantics."""
    for name in (
        "AATS_PROFILE",
        "AATS_ENV_TEMPLATE_PROFILE",
        "AATS_STARTUP_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    eligible = SimpleNamespace(
        required=False,
        eligible=True,
        reason_code="not_required",
        detail="not required in isolated workflow test",
    )
    values_bound = SimpleNamespace(
        to_dict=lambda: {"parameter_values_fingerprint": "a" * 64}
    )
    monkeypatch.setattr(
        "aats.data_platform.decision_system.promotion_guard.promotion_qualification_failure",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "aats.data_platform.decision_system.promotion_qualification.evaluate_promotion_qualification",
        lambda *_args, **_kwargs: eligible,
    )
    monkeypatch.setattr(
        "aats.data_platform.decision_system.promotion_guard."
        "require_apply_promotion_qualification",
        lambda *_args, **_kwargs: values_bound,
    )
    monkeypatch.setattr(
        "aats.data_platform.governance.parameter_identity."
        "parameter_values_fingerprint",
        lambda _values: "a" * 64,
    )
    monkeypatch.setattr(
        "aats.data_platform.decision_system.recommendation_registry.has_explicit_governance_db_configuration",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "aats.data_platform.decision_system.active_parameter_apply."
        "has_explicit_governance_db_configuration",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "aats.data_platform.governance.active_params_db."
        "db_get_known_bad_release_id_for_parameter_set",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "aats.data_platform.governance.operational_state_db."
        "db_get_parameter_release_for_update",
        lambda _session, **identity: {
            "release_id": identity["release_id"],
            "family": identity["family"],
            "timeframe": str(identity["timeframe"]).lower(),
            "combo_key": (
                f"{identity['family']}_{str(identity['timeframe']).lower()}"
            ),
            "recommendation_id": identity["recommendation_id"],
            "parameter_set_id": identity["parameter_set_id"],
            "previous_parameter_set_id": None,
            "gate_result_ref": None,
            "apply_result": "pending",
            "observation_status": "pending",
            "observation_window_hours": 24,
            "actor": "operator",
            "notes": None,
            "created_at": "2026-08-27T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        "aats.data_platform.governance.active_params_db."
        "db_get_parameter_set_for_update",
        lambda _session, **identity: {
            "parameter_set_id": identity["parameter_set_id"],
            "family": identity["family"],
            "symbol": identity["symbol"],
            "timeframe": str(identity["timeframe"]).lower(),
            "source_round_id": identity["source_round_id"],
            "values": identity["expected_values"],
            "status": "candidate",
        },
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _fake_save_release_history(history: dict, project_root: Path) -> Path:
    """Test stub for A-0.3: mimic the 'DB succeeded → JSON written' path without DB.

    The real `save_release_history` now raises DBUnavailableError when governance
    DB is unreachable (A-0.3 contract). These tests don't exercise the DB layer,
    so we substitute this stub that performs only the JSON audit write.
    """
    path = project_root / "artifacts/production_workflow/parameter_release_history.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    return path


def test_rdp_environment_is_derived_from_managed_profile() -> None:
    from aats.data_platform.operations.environment_guard import (
        get_current_environment,
    )

    with patch.dict(os.environ, {"AATS_PROFILE": "derivatives_live"}, clear=True):
        assert get_current_environment() == "prod"
    with patch.dict(os.environ, {"AATS_PROFILE": "derivatives"}, clear=True):
        assert get_current_environment() == "staging"


def test_rdp_environment_rejects_live_profile_conflict_and_partial_identity() -> None:
    from aats.data_platform.operations.environment_guard import (
        get_current_environment,
    )

    with (
        patch.dict(
            os.environ,
            {"AATS_PROFILE": "derivatives_live", "RDP_ENV": "dev"},
            clear=True,
        ),
        pytest.raises(ValueError, match="conflicts"),
    ):
        get_current_environment()

    with (
        patch.dict(
            os.environ,
            {"AATS_STARTUP_PROFILE": "derivatives"},
            clear=True,
        ),
        pytest.raises(ValueError, match="incomplete"),
    ):
        get_current_environment()


def test_parameter_registry_strict_read_denies_json_on_db_query_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import parameter_registry, parameter_sets_db
    from aats.data_platform.governance._exceptions import DBUnavailableError

    registry_path = (
        tmp_path / "artifacts/governance/current_parameter_registry.json"
    )
    _write_json(
        registry_path,
        {
            "parameter_sets": [
                {
                    "parameter_set_id": "ps_stale",
                    "family": "independent",
                    "timeframe": "15m",
                    "values": {"entry_threshold": 999},
                }
            ]
        },
    )

    class _Engine:
        def dispose(self) -> None: ...

    class _Session:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_Session": return self
        def __exit__(self, *_args: object) -> None: ...

    monkeypatch.setattr(
        parameter_registry,
        "try_governance_db",
        lambda: (_Engine(), True),
    )
    monkeypatch.setattr(
        parameter_sets_db,
        "db_load_full_registry",
        lambda _session: (_ for _ in ()).throw(RuntimeError("synthetic DB error")),
    )
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _Session)

    with pytest.raises(DBUnavailableError, match="stale JSON fallback denied"):
        parameter_registry.load_registry(
            registry_path,
            fail_closed_on_db_error=True,
        )


def test_managed_release_history_error_denies_stale_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance._exceptions import DBUnavailableError
    from aats.data_platform.production_workflow import release_registry

    _write_json(
        tmp_path / "artifacts/production_workflow/parameter_release_history.json",
        {"releases": [{"release_id": "rel_stale"}]},
    )

    class _Engine:
        def dispose(self) -> None: ...

    class _Session:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_Session": return self
        def __exit__(self, *_args: object) -> None: ...

    monkeypatch.setattr(
        release_registry,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(release_registry, "try_governance_db", lambda: (_Engine(), True))
    import aats.data_platform.governance.operational_state_db as state_db
    monkeypatch.setattr(
        state_db,
        "db_load_release_history",
        lambda _session: (_ for _ in ()).throw(RuntimeError("synthetic release read failure")),
    )
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _Session)

    with pytest.raises(DBUnavailableError, match="stale JSON fallback denied"):
        release_registry.load_release_history(tmp_path)


@pytest.mark.parametrize("writer_kind", ["observation", "rollback", "effectiveness"])
def test_managed_operational_writer_never_creates_file_when_db_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_kind: str,
) -> None:
    from aats.data_platform.governance._exceptions import DBUnavailableError

    if writer_kind == "observation":
        from aats.data_platform.production_workflow import observation_window as module

        result = {
            "release_id": "rel_writer_guard",
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "status": "completed",
            "recommendation": "keep",
            "observation_window_hours": 24,
            "window_active": False,
            "started_at": "2026-08-27T10:00:00+00:00",
            "evaluated_at": "2026-08-27T11:00:00+00:00",
            "checklist": [],
        }
        writer = lambda: module._save_observation(  # noqa: E731
            tmp_path, "rel_writer_guard", result
        )
        expected_path = (
            tmp_path
            / "artifacts/production_workflow/observations/rel_writer_guard/observation_summary.json"
        )
    elif writer_kind == "rollback":
        from aats.data_platform.production_workflow import rollback_policy as module

        result = {
            "release_id": "rel_writer_guard",
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "rollback_recommended": False,
            "severity": "none",
            "suggested_target_parameter_set_id": None,
            "evaluated_at": "2026-08-27T11:00:00+00:00",
            "triggers": [],
            "reasons": [],
        }
        writer = lambda: module._save_rollback_recommendation(  # noqa: E731
            tmp_path, "rel_writer_guard", result
        )
        expected_path = (
            tmp_path
            / "artifacts/production_workflow/rollback_recommendations/rel_writer_guard/rollback_recommendation.json"
        )
    else:
        from aats.data_platform.metrics import release_effectiveness as module

        result = {"evaluations": []}
        writer = lambda: module.save_effectiveness_registry(tmp_path, result)  # noqa: E731
        expected_path = (
            tmp_path / "artifacts/metrics/release_effectiveness_registry.json"
        )

    monkeypatch.setattr(
        module,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(module, "try_governance_db", lambda: (None, False))

    with pytest.raises(DBUnavailableError):
        writer()

    assert not expected_path.exists()


def test_decision_consistency_blocks_when_managed_truth_is_unavailable() -> None:
    result = check_decision_consistency(
        {
            "active_decisions": [],
            "active_decisions_available": False,
            "recommendation": {"family": "independent", "timeframe": "15m"},
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "active_decision_truth_unavailable"


def test_decision_consistency_blocks_unresolved_rollback() -> None:
    result = check_decision_consistency(
        {
            "environment": "prod",
            "active_decisions_available": True,
            "pending_rollback_truth_available": True,
            "pending_rollback_combos": {
                "independent_15m": "rel_needs_reconciliation"
            },
            "recommendation": {"family": "independent", "timeframe": "15m"},
            "active_decisions": [],
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "pending_rollback_unresolved"


def test_decision_consistency_requires_combo_decision_in_strict_environment() -> None:
    result = check_decision_consistency(
        {
            "environment": "staging",
            "active_decisions_available": True,
            "pending_rollback_truth_available": True,
            "pending_rollback_combos": {},
            "recommendation": {"family": "independent", "timeframe": "15m"},
            "active_decisions": [],
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "active_decision_missing"


@pytest.mark.parametrize("status", [None, "", "unknown", 1])
def test_strict_decision_consistency_rejects_malformed_matching_state(
    status: object,
) -> None:
    result = check_decision_consistency(
        {
            "environment": "prod",
            "active_decisions_available": True,
            "recommendation": {"family": "independent", "timeframe": "15m"},
            "active_decisions": [
                {
                    "combo_key": "independent_15m",
                    "current_status": status,
                }
            ],
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "active_decision_status_invalid"


@pytest.mark.parametrize(
    ("evidence_ref", "finished_at", "reason_code"),
    [
        (None, None, "evidence_freshness_missing"),
        ("round_1", "not-a-time", "evidence_finished_at_invalid"),
        (
            "round_1",
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "evidence_finished_at_future",
        ),
    ],
)
def test_strict_evidence_freshness_rejects_unprovable_or_future_evidence(
    evidence_ref: object,
    finished_at: object,
    reason_code: str,
) -> None:
    recommendation = {
        "recommendation_id": "rec_freshness",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_freshness",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "source_round_id": "source_round_1",
        "evidence_bundle_ref": evidence_ref,
    }
    verdict = None
    if evidence_ref is not None:
        verdict = PromotionQualificationVerdict(
            required=True,
            eligible=True,
            reason_code="qualified",
            evidence_bundle_ref=str(evidence_ref),
            source_round_id="source_round_1",
            qualified_round_id=str(evidence_ref),
            detail="qualified",
            qualified_finished_at=finished_at,
            parameter_values_fingerprint="a" * 64,
        )
    result = check_evidence_freshness({
        "environment": "prod",
        "recommendation": recommendation,
        "promotion_qualification": verdict,
    })

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == reason_code


@pytest.mark.parametrize(
    "health",
    [
        {"healthy": "false", "connection_ok": True},
        {"healthy": True},
        {"healthy": True, "connection_ok": "true"},
    ],
)
def test_strict_live_db_health_requires_exact_boolean_connection_truth(
    health: dict[str, object],
) -> None:
    result = check_live_db_health(
        {
            "runtime_contract": {
                "environment": "prod",
                "strict_environment": True,
                "live_db_health": {
                    **health,
                    "errors": ["postgresql://user:secret@host/db"],
                },
            }
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "live_db_health_invalid"
    assert "secret" not in result.detail


def test_quality_monitor_stale_file_cannot_claim_healthy() -> None:
    result = check_quality_monitor_health(
        {
            "quality_monitor_available": True,
            "quality_monitor_managed_truth": True,
            "quality_monitor": {
                "data_source": "file_fallback",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "health": "healthy",
                    "critical_failures": 0,
                    "warning_failures": 0,
                },
            },
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "quality_monitor_source_invalid"


@pytest.mark.parametrize(
    ("summary", "reason_code"),
    [
        ({"critical_failures": 0}, "quality_monitor_health_invalid"),
        (
            {"health": "unknown", "critical_failures": 0},
            "quality_monitor_health_invalid",
        ),
        (
            {"health": "healthy", "critical_failures": False},
                "quality_monitor_failure_counts_invalid",
        ),
        (
            {"health": "healthy", "critical_failures": "0"},
            "quality_monitor_failure_counts_invalid",
        ),
        (
            {"health": "healthy", "critical_failures": -1},
            "quality_monitor_failure_counts_invalid",
        ),
        (
            {
                "health": "healthy",
                "critical_failures": 0,
                "warning_failures": 999,
            },
            "quality_monitor_health_inconsistent",
        ),
        (
            {
                "health": "degraded",
                "critical_failures": 0,
                "warning_failures": 0,
            },
            "quality_monitor_health_inconsistent",
        ),
        (
            {
                "health": "healthy",
                "critical_failures": 0,
                "warning_failures": -1,
            },
            "quality_monitor_failure_counts_invalid",
        ),
    ],
)
def test_strict_quality_monitor_rejects_unknown_or_malformed_truth(
    summary: dict[str, object],
    reason_code: str,
) -> None:
    result = check_quality_monitor_health(
        {
            "environment": "prod",
            "quality_monitor_available": True,
            "quality_monitor": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary": summary,
            },
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == reason_code


def test_stale_healthy_alerts_block_strict_gate() -> None:
    result = check_current_alerts(
        {
            "runtime_contract": {
                "environment": "prod",
                "strict_environment": True,
                "current_alerts": {
                    "generated_at": "2020-01-01T00:00:00+00:00",
                    "overall_status": "healthy",
                    "critical_alerts": 0,
                    "warning_alerts": 0,
                },
            }
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "current_alerts_stale"


@pytest.mark.parametrize(
    ("overall", "critical", "warning", "reason_code"),
    [
        (None, 0, 0, "current_alerts_status_invalid"),
        ("unknown", 0, 0, "current_alerts_status_invalid"),
        ("healthy", False, 0, "current_alerts_count_invalid"),
        ("healthy", "0", 0, "current_alerts_count_invalid"),
        ("healthy", -1, 0, "current_alerts_count_invalid"),
        ("healthy", 0, 1, "current_alerts_inconsistent"),
        ("warning", 0, 0, "current_alerts_inconsistent"),
        ("critical", 0, 0, "current_alerts_inconsistent"),
    ],
)
def test_strict_current_alerts_rejects_malformed_or_inconsistent_truth(
    overall: object,
    critical: object,
    warning: object,
    reason_code: str,
) -> None:
    result = check_current_alerts(
        {
            "runtime_contract": {
                "environment": "prod",
                "strict_environment": True,
                "current_alerts": {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "overall_status": overall,
                    "critical_alerts": critical,
                    "warning_alerts": warning,
                },
            }
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == reason_code


def test_workflow_truth_unavailable_blocks_even_dev_gate() -> None:
    result = check_workflow_freshness(
        {
            "runtime_contract": {
                "environment": "dev",
                "strict_environment": False,
                "latest_workflow_runs": {},
                "workflow_runs_available": False,
            }
        }
    )

    assert result.passed is False
    assert result.severity == "block"
    assert result.reason_code == "workflow_truth_unavailable"


def _workflow_gate_context(
    *,
    status: str = "success",
    finished_at: str | None = None,
) -> dict:
    timestamp = finished_at or datetime.now(timezone.utc).isoformat()
    runs = {
        workflow: {
            "workflow": workflow,
            "overall_status": "success",
            "finished_at": timestamp,
        }
        for workflow in (
            "reliability_cycle",
            "data_maintenance",
            "governance_cycle",
            "decision_cycle",
        )
    }
    runs["data_maintenance"]["overall_status"] = status
    return {
        "runtime_contract": {
            "environment": "prod",
            "strict_environment": True,
            "latest_workflow_runs": runs,
            "workflow_runs_available": True,
        }
    }


def test_strict_workflow_gate_rejects_partial_status() -> None:
    result = check_workflow_freshness(_workflow_gate_context(status="partial"))

    assert result.passed is False
    assert result.severity == "block"
    assert "status=partial" in result.detail


def test_strict_workflow_gate_rejects_future_timestamp() -> None:
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = check_workflow_freshness(_workflow_gate_context(finished_at=future))

    assert result.passed is False
    assert result.severity == "block"
    assert "future" in result.detail


def test_strict_workflow_gate_accepts_small_clock_skew() -> None:
    slight_future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    result = check_workflow_freshness(
        _workflow_gate_context(finished_at=slight_future)
    )

    assert result.passed is True
    assert result.severity == "info"


def test_strict_workflow_gate_rejects_success_without_timestamp() -> None:
    context = _workflow_gate_context()
    run = context["runtime_contract"]["latest_workflow_runs"]["data_maintenance"]
    run.pop("finished_at")

    result = check_workflow_freshness(context)

    assert result.passed is False
    assert result.severity == "block"
    assert "missing finished_at/started_at" in result.detail


def test_phase6_snapshot_never_rewrites_failed_governance_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.production_workflow import gate_runtime_contract

    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        gate_runtime_contract,
        "_load_latest_decision_round_snapshot",
        lambda *_args, **_kwargs: {
            "round_id": "20260827_120000_deadbeef",
            "started_at": now,
            "finished_at": now,
            "status": "succeeded",
            "phase": "phase6",
            "data_source": "db",
            "path": "decision_round:20260827_120000_deadbeef",
        },
    )

    augmented = gate_runtime_contract._augment_workflow_runs_with_decision_round(
        {
            "governance_cycle": {
                "workflow": "governance_cycle",
                "overall_status": "failed",
                "finished_at": "2026-08-20T00:00:00+00:00",
            }
        },
        tmp_path,
        require_managed_db_truth=True,
    )

    assert augmented["governance_cycle"]["overall_status"] == "failed"
    assert augmented["decision_cycle"]["overall_status"] == "success"


@pytest.mark.parametrize("task_status", ["pending", "running", "failed"])
def test_newer_queue_attempt_overrides_old_success_report(
    task_status: str,
) -> None:
    from aats.data_platform.production_workflow.gate_runtime_contract import (
        _reconcile_workflow_runs_with_task_attempts,
    )

    old_report_time = datetime.now(timezone.utc) - timedelta(minutes=20)
    newer_attempt_time = old_report_time + timedelta(minutes=10)
    runs = _workflow_gate_context(
        finished_at=old_report_time.isoformat(),
    )["runtime_contract"]["latest_workflow_runs"]
    reconciled = _reconcile_workflow_runs_with_task_attempts(
        runs,
        {
            "data_maintenance": {
                "task_id": "task_newer_attempt",
                "run_id": "logical_run_newer_attempt",
                "attempt_no": 2,
                "workflow": "data_maintenance",
                "status": task_status,
                "requested_at": newer_attempt_time.isoformat(),
                "started_at": (
                    newer_attempt_time.isoformat()
                    if task_status != "pending"
                    else None
                ),
            }
        },
    )

    assert reconciled["data_maintenance"]["overall_status"] == f"task_{task_status}"
    result = check_workflow_freshness(
        {
            "runtime_contract": {
                "environment": "prod",
                "strict_environment": True,
                "latest_workflow_runs": reconciled,
                "workflow_runs_available": True,
            }
        }
    )
    assert result.passed is False
    assert result.severity == "block"
    assert f"status=task_{task_status}" in result.detail


def test_completed_queue_attempt_does_not_hide_its_newer_success_report() -> None:
    from aats.data_platform.production_workflow.gate_runtime_contract import (
        _reconcile_workflow_runs_with_task_attempts,
    )

    started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    finished_at = started_at + timedelta(minutes=5)
    runs = _workflow_gate_context(finished_at=finished_at.isoformat())[
        "runtime_contract"
    ]["latest_workflow_runs"]
    runs["data_maintenance"].update({
        "run_id": "logical_run_completed",
        "attempt_no": 1,
    })
    reconciled = _reconcile_workflow_runs_with_task_attempts(
        runs,
        {
            "data_maintenance": {
                "task_id": "task_completed",
                "run_id": "logical_run_completed",
                "attempt_no": 1,
                "workflow": "data_maintenance",
                "status": "done",
                "requested_at": (started_at - timedelta(minutes=1)).isoformat(),
                "started_at": started_at.isoformat(),
                "finished_at": (finished_at + timedelta(seconds=1)).isoformat(),
            }
        },
    )

    assert reconciled["data_maintenance"]["overall_status"] == "success"


@pytest.mark.parametrize("task_status", ["running", "failed"])
def test_phase6_synthetic_success_never_closes_nonterminal_decision_task(
    task_status: str,
) -> None:
    from aats.data_platform.production_workflow.gate_runtime_contract import (
        _reconcile_workflow_runs_with_task_attempts,
    )

    task_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    snapshot_time = task_time + timedelta(minutes=5)
    reconciled = _reconcile_workflow_runs_with_task_attempts(
        {
            "decision_cycle": {
                "run_id": "phase6_round",
                "workflow": "decision_cycle",
                "overall_status": "success",
                "started_at": task_time.isoformat(),
                "finished_at": snapshot_time.isoformat(),
                "synthetic_from": "db",
            }
        },
        {
            "decision_cycle": {
                "task_id": "task_incomplete_decision_cycle",
                "run_id": "logical_incomplete_decision_cycle",
                "attempt_no": 2,
                "workflow": "decision_cycle",
                "status": task_status,
                "requested_at": task_time.isoformat(),
                "started_at": task_time.isoformat(),
            }
        },
    )

    assert reconciled["decision_cycle"]["overall_status"] == f"task_{task_status}"
    assert reconciled["decision_cycle"]["reconciliation_required"] is True


def _seed_rdp_health_artifacts(root: Path) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    _write_json(root / "artifacts/governance/artifact_index.json", {"generated_at": generated_at})
    _write_json(root / "artifacts/governance/active_round_index.json", {"generated_at": generated_at})
    _write_json(
        root / "artifacts/governance/current_parameter_registry.json",
        {"generated_at": generated_at, "parameter_sets": []},
    )
    _write_json(
        root / "artifacts/governance/quality_monitor_summary.json",
        {
            "generated_at": generated_at,
            "summary": {
                "health": "healthy",
                "critical_failures": 0,
                "warning_failures": 0,
            },
        },
    )
    _write_json(root / "artifacts/decision_system/recommendation_registry.json", {"generated_at": generated_at})
    _write_json(root / "artifacts/decision_system/active_decision_registry.json", {"generated_at": generated_at})
    _write_json(root / "artifacts/decision_system/evidence_bundle_index.json", {"generated_at": generated_at})
    _write_json(
        root / "artifacts/operations/alerts/current_alerts.json",
        {
            "generated_at": generated_at,
            "overall_status": "healthy",
            "critical_alerts": 0,
            "warning_alerts": 0,
            "alerts": [],
        },
    )

    finished_at = datetime.now(timezone.utc) - timedelta(hours=1)
    for workflow in ("data_maintenance", "governance_cycle", "decision_cycle"):
        _write_json(
            root / "artifacts/operations/workflow_runs" / f"{workflow}.json",
            {
                "run_id": f"run_{workflow}",
                "workflow": workflow,
                "overall_status": "success",
                "started_at": (finished_at - timedelta(minutes=10)).isoformat(),
                "finished_at": finished_at.isoformat(),
            },
        )


def test_prod_direct_apply_requires_release_context() -> None:
    recommendation = {
        "recommendation_id": "rec_prod_1",
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-04-14T10:00:00+00:00",
        "target_parameter_set_id": "ps_prod_1",
    }
    registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_prod_1",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.41},
            },
        ],
    }
    with (
        patch.dict(
            os.environ,
            {"RDP_ENV": "prod"},
            clear=False,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=registry,
        ),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "has_explicit_governance_db_configuration",
            return_value=True,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_prod_1",
            actor="operator",
            gate_result={
                "allow_apply": True,
                "gate_status": "pass",
                "blocking_reasons": [],
            },
        )

    assert result["ok"] is False
    assert "direct apply" in result["message"]


@pytest.mark.parametrize(
    "gate_result",
    [
        {"allow_apply": "false", "gate_status": "pass", "blocking_reasons": []},
        {"allow_apply": True, "gate_status": "block", "blocking_reasons": []},
        {"allow_apply": True, "gate_status": "error", "blocking_reasons": []},
        {"allow_apply": True, "blocking_reasons": []},
    ],
)
def test_direct_apply_rejects_malformed_or_nonpass_gate_contract(
    gate_result: dict,
) -> None:
    recommendation = {
        "recommendation_id": "rec_gate_contract",
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-08-27T12:00:00+00:00",
        "target_parameter_set_id": "ps_gate_contract",
    }
    with (
        patch.dict(os.environ, {"RDP_ENV": "staging"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_gate_contract",
            actor="operator",
            gate_result=gate_result,
            release_id="rel_gate_contract",
        )

    assert result["ok"] is False
    assert "gate blocked apply" in result["message"]


@pytest.mark.parametrize(
    "persisted_gate",
    [
        None,
        {
            "gate_run_id": "gate_persisted",
            "recommendation_id": "rec_persisted_gate",
            "release_id": "rel_persisted_gate",
            "allow_apply": "true",
            "gate_status": "pass",
        },
        {
            "gate_run_id": "gate_persisted",
            "recommendation_id": "rec_persisted_gate",
            "release_id": "rel_persisted_gate",
            "allow_apply": True,
            "gate_status": "block",
        },
        {
            "gate_run_id": "gate_persisted",
            "recommendation_id": "rec_other",
            "release_id": "rel_persisted_gate",
            "allow_apply": True,
            "gate_status": "pass",
        },
        {
            "gate_run_id": "gate_persisted",
            "recommendation_id": "rec_persisted_gate",
            "release_id": "rel_other",
            "allow_apply": True,
            "gate_status": "pass",
        },
    ],
)
def test_apply_revalidates_persisted_gate_inside_transaction(
    persisted_gate: dict | None,
) -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    recommendation = {
        "recommendation_id": "rec_persisted_gate",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_persisted_gate",
        "source_round_id": "round_persisted_gate",
        "evidence_bundle_ref": "bundle_persisted_gate",
        "status": "approved",
        "approved_by": "risk-reviewer",
        "approved_at": "2026-08-27T12:00:00+00:00",
    }

    @contextmanager
    def _session():
        yield object()

    upsert = MagicMock()
    with (
        patch.dict(os.environ, {"RDP_ENV": "staging"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={
                "parameter_sets": [
                    {
                            "parameter_set_id": "ps_persisted_gate",
                            "family": "independent",
                            "symbol": "BTC-USDT-SWAP",
                            "timeframe": "15m",
                            "source_round_id": "round_persisted_gate",
                            "values": {"entry_threshold": 0.35},
                    }
                ]
            },
        ),
        patch("aats.data_platform.db.get_session", side_effect=_session),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_pending_rollback_release_id",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_recommendation_for_update",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_active_decision_for_update",
            return_value={
                "family": "independent",
                "timeframe": "15m",
                "current_status": "keep_active",
            },
        ),
        patch(
            "aats.data_platform.governance.operational_state_db."
            "db_get_gate_result_by_run_id",
            return_value=persisted_gate,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
            upsert,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_persisted_gate",
            release_id="rel_persisted_gate",
            gate_result={
                "gate_run_id": "gate_persisted",
                "recommendation_id": "rec_persisted_gate",
                "allow_apply": True,
                "gate_status": "pass",
                "blocking_reasons": [],
            },
        )

    assert result["ok"] is False
    assert result["code"] == "gate_state_changed"
    upsert.assert_not_called()


@pytest.mark.parametrize(
    "gate_result",
    [
        {"allow_apply": "false", "gate_status": "pass", "blocking_reasons": []},
        {"allow_apply": True, "gate_status": "block", "blocking_reasons": []},
        {"allow_apply": True, "gate_status": "error", "blocking_reasons": []},
        {"allow_apply": True, "blocking_reasons": []},
    ],
)
def test_release_never_calls_apply_for_malformed_or_nonpass_gate_contract(
    gate_result: dict,
) -> None:
    from unittest.mock import MagicMock

    recommendation = {
        "recommendation_id": "rec_release_gate_contract",
        "recommendation_type": "parameter_upgrade",
        "status": "approved",
        "target_parameter_set_id": "ps_release_gate_contract",
    }
    parameter_registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_release_gate_contract",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.35},
            }
        ]
    }
    apply_mock = MagicMock(
        return_value={"ok": True, "from_parameter_set_id": "ps_before_apply"}
    )
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=parameter_registry,
        ),
        patch(
            "aats.bootstrap.active_parameters.load_active_parameter_registry",
            return_value={"active_sets": {}},
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.run_pre_apply_gate",
            return_value=gate_result,
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.load_release_history",
            return_value={"releases": []},
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.save_release_history",
            side_effect=_fake_save_release_history,
        ),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "apply_approved_recommendation",
            apply_mock,
        ),
    ):
        result = create_parameter_release(
            Path("."),
            recommendation_id="rec_release_gate_contract",
            run_gate=True,
            run_apply=True,
        )

    assert result["ok"] is False
    assert result["release"]["apply_result"] == "blocked_by_gate"
    apply_mock.assert_not_called()


@pytest.mark.parametrize("locked_status", ["superseded", "rejected", "draft"])
def test_apply_rechecks_recommendation_status_after_combo_lock(
    locked_status: str,
) -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    recommendation = {
        "recommendation_id": "rec_concurrent_apply",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_concurrent_apply",
        "source_round_id": "round_concurrent_apply",
        "evidence_bundle_ref": "bundle_concurrent_apply",
        "status": "approved",
    }
    parameter_registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_concurrent_apply",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.35},
            }
        ]
    }

    @contextmanager
    def _session():
        yield object()

    upsert = MagicMock()
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=parameter_registry,
        ),
        patch("aats.data_platform.db.get_session", side_effect=_session),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_pending_rollback_release_id",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_recommendation_for_update",
            return_value={**recommendation, "status": locked_status},
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
            upsert,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_concurrent_apply",
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "recommendation_state_changed"
    upsert.assert_not_called()


def test_apply_rejects_parameter_set_with_known_bad_effectiveness() -> None:
    """A fresh approval must not resurrect an immutable known-bad parameter set."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    recommendation = {
        "recommendation_id": "rec_reintroduce_bad",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_known_bad",
        "source_round_id": "round_reintroduce_bad",
        "evidence_bundle_ref": "bundle_reintroduce_bad",
        "status": "approved",
    }
    parameter_registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_known_bad",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.35},
            }
        ]
    }

    @contextmanager
    def _session():
        yield object()

    upsert = MagicMock()
    locked_reader = MagicMock()
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=parameter_registry,
        ),
        patch("aats.data_platform.db.get_session", side_effect=_session),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_pending_rollback_release_id",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_known_bad_release_id_for_parameter_set",
            return_value="rel_prior_bad",
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_recommendation_for_update",
            locked_reader,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
            upsert,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_reintroduce_bad",
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "known_bad_parameter_set"
    assert result["known_bad_release_id"] == "rel_prior_bad"
    locked_reader.assert_not_called()
    upsert.assert_not_called()


def test_apply_rejects_reusing_an_already_applied_recommendation() -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    recommendation = {
        "recommendation_id": "rec_already_applied",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_already_applied",
        "source_round_id": "round_already_applied",
        "evidence_bundle_ref": "bundle_already_applied",
        "status": "approved",
    }
    parameter_registry = {
        "parameter_sets": [
                {
                    "parameter_set_id": "ps_already_applied",
                    "family": "independent",
                    "symbol": "BTC-USDT-SWAP",
                    "timeframe": "15m",
                    "source_round_id": "round_already_applied",
                    "values": {"entry_threshold": 0.35},
                }
        ]
    }

    class _Result:
        def __init__(self, row: object) -> None:
            self._row = row

        def fetchone(self) -> object:
            return self._row

    class _Session:
        def execute(self, statement: object, _params: dict) -> _Result:
            sql = str(statement)
            if "parameter_apply_history" in sql:
                return _Result(SimpleNamespace(operation_id="op_first_apply"))
            if "active_parameter_sets" in sql:
                return _Result(
                    SimpleNamespace(
                        parameter_set_id="ps_already_applied",
                        approval_recommendation_id="rec_already_applied",
                    )
                )
            raise AssertionError(f"unexpected SQL: {sql}")

    @contextmanager
    def _session():
        yield _Session()

    upsert = MagicMock()
    history = MagicMock()
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=parameter_registry,
        ),
        patch("aats.data_platform.db.get_session", side_effect=_session),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_pending_rollback_release_id",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_recommendation_for_update",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
            upsert,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history",
            history,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_already_applied",
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "recommendation_already_applied"
    assert result["existing_operation_id"] == "op_first_apply"
    upsert.assert_not_called()
    history.assert_not_called()


def test_apply_blocks_unresolved_rollback_after_combo_lock() -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    recommendation = {
        "recommendation_id": "rec_pending_rollback",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_pending_rollback",
        "source_round_id": "round_pending_rollback",
        "evidence_bundle_ref": "bundle_pending_rollback",
        "status": "approved",
    }

    @contextmanager
    def _session():
        yield object()

    locked_reader = MagicMock()
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={
                "parameter_sets": [
                    {
                        "parameter_set_id": "ps_pending_rollback",
                        "family": "independent",
                        "timeframe": "15m",
                        "values": {"entry_threshold": 0.35},
                    }
                ]
            },
        ),
        patch("aats.data_platform.db.get_session", side_effect=_session),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_get_pending_rollback_release_id",
            return_value="rel_needs_reconciliation",
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_recommendation_for_update",
            locked_reader,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_pending_rollback",
        )

    assert result["ok"] is False
    assert result["code"] == "pending_rollback"
    assert result["pending_rollback_release_id"] == "rel_needs_reconciliation"
    locked_reader.assert_not_called()


def test_apply_fails_closed_when_combo_transaction_lock_is_busy() -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    recommendation = {
        "recommendation_id": "rec_combo_busy",
        "family": "independent",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_combo_busy",
        "source_round_id": "round_combo_busy",
        "evidence_bundle_ref": "bundle_combo_busy",
        "status": "approved",
    }
    parameter_registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_combo_busy",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.35},
            }
        ]
    }

    @contextmanager
    def _session():
        yield object()

    locked_reader = MagicMock()
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=parameter_registry,
        ),
        patch("aats.data_platform.db.get_session", side_effect=_session),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=False,
        ),
        patch(
            "aats.data_platform.governance.recommendations_db."
            "db_get_recommendation_for_update",
            locked_reader,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_combo_busy",
            actor="operator",
        )

    assert result["ok"] is False
    assert result["code"] == "parameter_apply_conflict"
    locked_reader.assert_not_called()


def test_parameter_apply_lock_is_transaction_scoped_and_combo_bound() -> None:
    from aats.data_platform.governance.active_params_db import (
        db_try_acquire_parameter_apply_lock,
    )

    class _Result:
        def fetchone(self) -> SimpleNamespace:
            return SimpleNamespace(acquired=True)

    class _Session:
        statement = ""
        params: dict = {}

        def execute(self, statement: object, params: dict) -> _Result:
            self.statement = str(statement)
            self.params = params
            return _Result()

    session = _Session()
    acquired = db_try_acquire_parameter_apply_lock(
        session,  # type: ignore[arg-type]
        family=" Independent ",
        timeframe=" 1H ",
    )

    assert acquired is True
    assert "pg_try_advisory_xact_lock" in session.statement
    assert session.params["combo_key"] == "independent_1h"


@pytest.mark.parametrize(
    ("family", "timeframe"),
    [
        ("", "1h"),
        ("   ", "1h"),
        ("independent", ""),
        ("independent", "   "),
    ],
)
def test_parameter_apply_lock_rejects_empty_combo_identity(
    family: str,
    timeframe: str,
) -> None:
    from aats.data_platform.governance.active_params_db import (
        db_try_acquire_parameter_apply_lock,
    )

    class _Session:
        def execute(self, _statement: object, _params: dict) -> None:
            raise AssertionError("invalid combo must fail before database access")

    with pytest.raises(ValueError, match="must be non-empty"):
        db_try_acquire_parameter_apply_lock(
            _Session(),  # type: ignore[arg-type]
            family=family,
            timeframe=timeframe,
        )


def test_parameter_producer_and_apply_variants_contend_on_same_lock() -> None:
    """Mixed-case producer identity cannot bypass a canonical apply lock."""
    from aats.data_platform.governance.active_params_db import (
        db_try_acquire_parameter_apply_lock,
    )

    class _LockBackend:
        def __init__(self) -> None:
            self.held: set[tuple[int, str]] = set()

        def try_acquire(self, namespace: int, combo_key: str) -> bool:
            key = (namespace, combo_key)
            if key in self.held:
                return False
            self.held.add(key)
            return True

    class _Result:
        def __init__(self, acquired: bool) -> None:
            self.acquired = acquired

        def fetchone(self) -> SimpleNamespace:
            return SimpleNamespace(acquired=self.acquired)

    class _Session:
        def __init__(self, backend: _LockBackend) -> None:
            self.backend = backend
            self.combo_keys: list[str] = []

        def execute(self, _statement: object, params: dict) -> _Result:
            combo_key = str(params["combo_key"])
            self.combo_keys.append(combo_key)
            return _Result(
                self.backend.try_acquire(int(params["namespace"]), combo_key)
            )

    backend = _LockBackend()
    producer_session = _Session(backend)
    apply_session = _Session(backend)

    assert db_try_acquire_parameter_apply_lock(
        producer_session,  # type: ignore[arg-type]
        family=" Independent ",
        timeframe=" 1H ",
    )
    assert not db_try_acquire_parameter_apply_lock(
        apply_session,  # type: ignore[arg-type]
        family="independent",
        timeframe="1h",
    )
    assert producer_session.combo_keys == ["independent_1h"]
    assert apply_session.combo_keys == ["independent_1h"]


def test_pending_rollback_lookup_is_combo_bound_and_fail_closed() -> None:
    from aats.data_platform.governance.active_params_db import (
        db_get_pending_rollback_release_id,
    )

    class _Result:
        def __init__(self, row: SimpleNamespace | None) -> None:
            self.row = row

        def fetchone(self) -> SimpleNamespace | None:
            return self.row

    class _Session:
        statements: list[str] = []
        params: dict = {}

        def execute(self, statement: object, params: dict) -> _Result:
            self.statements.append(str(statement))
            self.params = params
            if "WITH raw_obligations AS" in str(statement):
                return _Result(None)
            return _Result(SimpleNamespace(release_id="rel_pending"))

    session = _Session()
    release_id = db_get_pending_rollback_release_id(
        session,  # type: ignore[arg-type]
        family="independent",
        timeframe="1H",
    )

    assert release_id == "rel_pending"
    sql = "\n".join(session.statements)
    assert "WITH raw_obligations AS" in sql
    assert "governance.observation_results" in sql
    assert "governance.rollback_recommendations" in sql
    assert "conclusion = 'rollback_triggered'" in sql
    assert "rollback_enforced" in sql
    assert "rollback_cancelled" in sql
    assert "rollback_enforcement_status" in sql
    assert "= 'enforced'" in sql
    assert "= 'cancelled'" in sql
    assert "IS NOT TRUE" in sql
    assert "payload ? 'rollback_enforced'" in sql
    assert "payload ? 'rollback_cancelled'" in sql
    assert "rollback_soft_pause_applied" in sql
    assert "rollback_cancelled_reason" in sql
    assert "soft_paused_no_valid_rollback_target:" in sql
    assert "rdp-rollback-capital-proof/v1" in sql
    assert "rdp-release-rollback-capital-proof/v1" in sql
    assert "rollback_capital_proof_verified" in sql
    assert "rollback_enforcement_attempt_id" in sql
    assert "rollback_enforcement_started_at" in sql
    assert "rollback_enforcement_finished_at" in sql
    assert "governance.parameter_apply_history AS rh" in sql
    assert "governance.release_effectiveness_action_proofs AS ep" in sql
    assert "ep.attempt_id" in sql
    assert "ep.started_at_utc" in sql
    assert "ep.finished_at_utc" in sql
    # Legacy calendar garbage must fail closed without throwing a DB cast error.
    assert "mod(" in sql
    assert "to_date(" not in sql
    assert "::timestamptz" not in sql
    assert "now() + interval '5 minutes'" in sql
    assert "LEFT JOIN governance.parameter_releases" in sql
    assert "r.apply_result IS DISTINCT FROM 'success'" in sql
    assert "NULLIF(btrim(e.family), '') IS NULL" in sql
    assert "NULLIF(btrim(e.timeframe), '') IS NULL" in sql
    assert "lower(btrim(r.family)) = lower(btrim(:family))" in sql
    assert "e.payload ->> 'combo_key'" in sql
    assert session.params == {"family": "independent", "timeframe": "1h"}


def test_create_parameter_release_rejects_prod_skip_gate_and_short_window() -> None:
    with patch.dict(
        os.environ,
        {"RDP_ENV": "prod"},
        clear=False,
    ):
        skip_gate = create_parameter_release(
            Path("."),
            recommendation_id="rec_any",
            run_gate=False,
            run_apply=True,
        )
        short_window = create_parameter_release(
            Path("."),
            recommendation_id="rec_any",
            run_gate=True,
            run_apply=True,
            observation_window_hours=24,
        )

    assert skip_gate["ok"] is False
    assert "requires gate pass" in skip_gate["message"]
    assert short_window["ok"] is False
    assert "observation_window_hours" in short_window["message"]


def test_failed_apply_uses_db_valid_pending_observation_status() -> None:
    from aats.data_platform.production_workflow.release_registry import (
        _record_release_apply_outcome,
    )

    release = {"apply_result": "pending", "observation_status": "pending"}
    _record_release_apply_outcome(release, {"ok": False})

    assert release["apply_result"] == "failed"
    assert release["observation_status"] == "pending"


def test_successful_apply_overwrites_snapshot_predecessor_with_transaction_truth() -> None:
    from aats.data_platform.production_workflow.release_registry import (
        _record_release_apply_outcome,
    )

    release = {
        "previous_parameter_set_id": "ps_stale_snapshot",
        "apply_result": "pending",
        "observation_status": "pending",
    }
    _record_release_apply_outcome(
        release,
        {"ok": True, "from_parameter_set_id": "ps_locked_transaction"},
    )

    assert release["apply_result"] == "success"
    assert release["observation_status"] == "observing"
    assert release["previous_parameter_set_id"] == "ps_locked_transaction"


def test_success_without_transaction_predecessor_requires_reconciliation() -> None:
    from aats.data_platform.production_workflow.release_registry import (
        _record_release_apply_outcome,
    )

    release = {
        "previous_parameter_set_id": "ps_stale_snapshot",
        "apply_result": "pending",
        "observation_status": "pending",
    }
    _record_release_apply_outcome(release, {"ok": True})

    assert release["apply_result"] == "pending"
    assert release["observation_status"] == "pending"
    assert release["apply_reconciliation_required"] is True


def test_create_parameter_release_passes_release_context_to_apply() -> None:
    recommendation = {
        "recommendation_id": "rec_dev_1",
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-04-14T10:00:00+00:00",
        "target_parameter_set_id": "ps_dev_1",
    }
    registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_dev_1",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.35},
            },
        ],
    }
    gate_result = {
        "allow_apply": True,
        "gate_status": "pass",
        "blocking_reasons": [],
        "warnings": [],
        "gate_run_id": "gate_1",
    }
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=registry,
        ),
        patch(
            "aats.bootstrap.active_parameters.load_active_parameter_registry",
            return_value={"active_sets": {}},
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.run_pre_apply_gate",
            return_value=gate_result,
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.load_release_history",
            return_value={"releases": []},
        ),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply.apply_approved_recommendation",
            return_value={
                "ok": True,
                "message": "applied",
                "from_parameter_set_id": "ps_transaction_previous",
            },
        ) as apply_mock,
        patch(
            "aats.data_platform.production_workflow.release_registry.save_release_history",
            side_effect=_fake_save_release_history,
        ),
    ):
        result = create_parameter_release(
            Path("."),
            recommendation_id="rec_dev_1",
            run_gate=True,
            run_apply=True,
        )

    assert result["ok"] is True
    kwargs = apply_mock.call_args.kwargs
    assert kwargs["gate_result"] == gate_result
    assert kwargs["release_id"].startswith("rel_")
    assert result["release"]["previous_parameter_set_id"] == "ps_transaction_previous"


def test_release_blocks_before_gate_when_active_parameter_truth_failed() -> None:
    from unittest.mock import MagicMock

    recommendation = {
        "recommendation_id": "rec_active_truth_failed",
        "status": "approved",
        "target_parameter_set_id": "ps_active_truth_failed",
    }
    gate = MagicMock()
    apply = MagicMock()
    save = MagicMock()
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={
                "parameter_sets": [
                    {
                        "parameter_set_id": "ps_active_truth_failed",
                        "family": "independent",
                        "timeframe": "15m",
                        "values": {"entry_threshold": 0.35},
                    }
                ]
            },
        ),
        patch(
            "aats.bootstrap.active_parameters.load_active_parameter_registry",
            return_value={
                "active_sets": {},
                "db_load_failed": True,
            },
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.run_pre_apply_gate",
            gate,
        ),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "apply_approved_recommendation",
            apply,
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry."
            "save_release_history",
            save,
        ),
    ):
        result = create_parameter_release(
            Path("."),
            recommendation_id="rec_active_truth_failed",
            run_gate=True,
            run_apply=True,
        )

    assert result["ok"] is False
    assert result["code"] == "active_parameter_truth_unavailable"
    gate.assert_not_called()
    apply.assert_not_called()
    save.assert_not_called()


def test_release_does_not_apply_when_pending_audit_cannot_persist() -> None:
    """active 参数写入前必须先留下 pending release 审计锚点。"""
    from unittest.mock import MagicMock

    from aats.data_platform.governance._exceptions import DBUnavailableError

    recommendation = {
        "recommendation_id": "rec_pending_anchor",
        "recommendation_type": "parameter_upgrade",
        "status": "approved",
        "target_parameter_set_id": "ps_pending_anchor",
    }
    parameter_registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_pending_anchor",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.35},
            }
        ]
    }
    gate_result = {
        "allow_apply": True,
        "gate_status": "pass",
        "blocking_reasons": [],
        "warnings": [],
        "gate_run_id": "gate_pending_anchor",
    }
    apply_mock = MagicMock(
        return_value={"ok": True, "from_parameter_set_id": "ps_before_apply"}
    )
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=parameter_registry,
        ),
        patch(
            "aats.bootstrap.active_parameters.load_active_parameter_registry",
            return_value={"active_sets": {}},
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.run_pre_apply_gate",
            return_value=gate_result,
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.load_release_history",
            return_value={"releases": []},
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.save_release_history",
            side_effect=DBUnavailableError("pending release persistence unavailable"),
        ),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply.apply_approved_recommendation",
            apply_mock,
        ),
        pytest.raises(DBUnavailableError, match="pending release persistence unavailable"),
    ):
        create_parameter_release(
            Path("."),
            recommendation_id="rec_pending_anchor",
            run_gate=True,
            run_apply=True,
        )

    apply_mock.assert_not_called()


def test_final_release_save_failure_quarantines_next_automatic_cycle() -> None:
    """apply outcome 未能回写时，pending 锚点必须阻止下一周期再次 apply。"""
    from unittest.mock import MagicMock

    from aats.data_platform.governance._exceptions import DBUnavailableError
    from aats.data_platform.production_workflow.release_cycle import (
        _select_release_candidates,
    )

    recommendation = {
        "recommendation_id": "rec_uncertain_outcome",
        "recommendation_type": "parameter_upgrade",
        "status": "approved",
        "approved_at": "2026-08-27T12:00:00+00:00",
        "family": "independent",
        "timeframe": "15m",
        "target_parameter_set_id": "ps_uncertain_outcome",
    }
    parameter_registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_uncertain_outcome",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.35},
            }
        ]
    }
    gate_result = {
        "allow_apply": True,
        "gate_status": "pass",
        "blocking_reasons": [],
        "warnings": [],
        "gate_run_id": "gate_uncertain_outcome",
    }
    persisted_history: dict = {}
    save_calls = 0

    def _save_then_fail(history: dict, _root: Path) -> Path:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            persisted_history.update(deepcopy(history))
            return Path("pending-audit.json")
        raise DBUnavailableError("final release state unavailable")

    apply_mock = MagicMock(
        return_value={"ok": True, "from_parameter_set_id": "ps_before_apply"}
    )
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry."
            "find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=parameter_registry,
        ),
        patch(
            "aats.bootstrap.active_parameters.load_active_parameter_registry",
            return_value={"active_sets": {}},
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.run_pre_apply_gate",
            return_value=gate_result,
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.load_release_history",
            return_value={"releases": []},
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.save_release_history",
            side_effect=_save_then_fail,
        ),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "apply_approved_recommendation",
            apply_mock,
        ),
        pytest.raises(DBUnavailableError, match="final release state unavailable"),
    ):
        create_parameter_release(
            Path("."),
            recommendation_id="rec_uncertain_outcome",
            run_gate=True,
            run_apply=True,
        )

    apply_mock.assert_called_once()
    assert persisted_history["releases"][0]["apply_result"] == "pending"
    selection = _select_release_candidates(
        {"recommendations": [recommendation]},
        persisted_history,
        qualification_verdicts={
            "rec_uncertain_outcome": SimpleNamespace(
                eligible=True,
                reason_code="qualified",
                to_dict=lambda: {"eligible": True, "reason_code": "qualified"},
            )
        },
    )
    assert selection["eligible"] == []
    assert "reconciliation" in selection["skipped"][0]["detail"]


def test_rollback_expected_source_mismatch_has_no_parameter_side_effect(
    tmp_path: Path,
) -> None:
    """Automatic rollback compare-and-set must preserve a newer operator choice."""
    from unittest.mock import MagicMock

    class _Result:
        @staticmethod
        def fetchone() -> SimpleNamespace:
            return SimpleNamespace(parameter_set_id="ps_operator_choice")

    class _Session:
        def execute(self, *_args, **_kwargs) -> _Result:
            return _Result()

        def __enter__(self) -> "_Session":
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> bool:
            return False

    rollback_guard = SimpleNamespace(allowed=True, reason="")
    upsert = MagicMock()
    validate = MagicMock(return_value=(True, ""))
    with (
        patch(
            "aats.data_platform.operations.environment_guard.get_current_environment",
            return_value="dev",
        ),
        patch(
            "aats.data_platform.operations.environment_guard.guard_parameter_rollback",
            return_value=rollback_guard,
        ),
        patch("aats.data_platform.db.get_session", return_value=_Session()),
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.validate_rollback_target",
            validate,
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
            upsert,
        ),
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_old",
            expected_from_parameter_set_id="ps_release_under_review",
            actor="release_effectiveness_auto_rollback",
        )

    assert result["ok"] is False
    assert result["code"] == "ACTIVE_SET_CHANGED"
    assert result["from_parameter_set_id"] == "ps_operator_choice"
    validate.assert_not_called()
    upsert.assert_not_called()


def test_rollback_marks_latest_successful_release_as_rolled_back(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/governance/current_parameter_registry.json",
        {
            "generated_at": "2026-04-16T10:00:00Z",
            "parameter_sets": [
                {
                    "parameter_set_id": "ps_live_0",
                    "family": "independent",
                    "timeframe": "15m",
                    "status": "frozen",
                    "source_round_id": "round_prev",
                    "values": {"entry_threshold": 0.4},
                },
            ],
        },
    )
    _write_json(
        tmp_path / "artifacts/production_workflow/parameter_release_history.json",
        {
            "generated_at": "2026-04-16T10:05:00Z",
            "releases": [
                {
                    "release_id": "rel_active_1",
                    "created_at": "2026-04-16T10:01:00Z",
                    "family": "independent",
                    "timeframe": "15m",
                    "combo_key": "independent_15m",
                    "recommendation_id": "rec_active_1",
                    "parameter_set_id": "ps_live_1",
                    "previous_parameter_set_id": "ps_live_0",
                    "actor": "operator",
                    "apply_result": "success",
                    "observation_status": "observing",
                },
            ],
        },
    )

    class _Result:
        """A-0.1 后 rollback 走多条 SELECT，本测试只关心 release history 副作用，
        因此所有查询都返回 "当前 active=ps_live_1"，其余 DB 语义交由 validate/
        get_values 的 patch 伪造。
        """

        def __init__(self, row: SimpleNamespace, *, rowcount: int = 1) -> None:
            self.row = row
            self.rowcount = rowcount

        def fetchone(self) -> SimpleNamespace:
            return self.row

    class _Session:
        def execute(self, statement: object, *_args, **_kwargs) -> _Result:
            if "COUNT(*) AS release_count" in str(statement):
                return _Result(SimpleNamespace(
                    release_count=1,
                    release_id="rel_active_1",
                ))
            return _Result(SimpleNamespace(
                parameter_set_id="ps_live_1",
                approval_recommendation_id="rec_active_1",
            ))

        def __enter__(self) -> "_Session":
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> bool:
            return False

    rollback_guard = SimpleNamespace(allowed=True, reason="")

    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.operations.environment_guard.get_current_environment",
            return_value="dev",
        ),
        patch(
            "aats.data_platform.operations.environment_guard.guard_parameter_rollback",
            return_value=rollback_guard,
        ),
        patch(
            "aats.data_platform.db.get_session",
            side_effect=lambda: _Session(),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.validate_rollback_target",
            return_value=(True, ""),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_get_parameter_set_values",
            return_value={
                "values": {"entry_threshold": 0.4},
                "source_round_id": "round_prev",
                "approval_recommendation_id": "rec_prev",
            },
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history",
        ),
        patch(
            "aats.data_platform.governance.operational_state_db."
            "db_upsert_parameter_release",
            side_effect=lambda _session, release, **_kwargs: dict(release),
        ) as release_upsert,
        patch(
            "aats.data_platform.governance.active_params_db."
            "db_try_acquire_parameter_apply_lock",
            return_value=True,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.save_release_history",
            side_effect=_fake_save_release_history,
        ),
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_live_0",
            actor="operator",
        )

        assert result["ok"] is True
        assert result["release_id"] == "rel_active_1"
        release_upsert.assert_called_once()

    release_history = json.loads(
        (tmp_path / "artifacts/production_workflow/parameter_release_history.json").read_text(encoding="utf-8"),
    )
    release = release_history["releases"][0]
    assert release["observation_status"] == "rolled_back"
    assert release["rollback_to_parameter_set_id"] == "ps_live_0"
    assert release["rollback_operation_id"] == result["operation_id"]
    assert release["rolled_back_at"]


def test_gate_rules_block_prod_on_critical_alerts_and_warn_dev_on_live_db_failure() -> None:
    alert_result = check_current_alerts(
        {
            "environment": "prod",
            "current_alerts": {
                "overall_status": "critical",
                "critical_alerts": 1,
                "warning_alerts": 0,
            },
        }
    )
    live_db_result = check_live_db_health(
        {
            "environment": "dev",
            "live_db_health": {
                "healthy": False,
                "errors": ["RDP_LIVE_DATABASE_URL 未配置"],
            },
        }
    )

    assert alert_result.passed is False
    assert alert_result.severity == "block"
    assert live_db_result.passed is True
    assert live_db_result.severity == "warn"


def test_query_rdp_health_blocks_in_prod_when_daemon_status_missing(tmp_path: Path) -> None:
    _seed_rdp_health_artifacts(tmp_path)

    with (
        patch.dict(os.environ, {"RDP_ENV": "prod"}, clear=False),
        patch(
            "aats.services.operator.rdp_queries._query_governance_runtime_state",
            return_value={
                "connection_ok": True,
                "task_queue": {
                    "pending_count": 0,
                    "running_count": 0,
                    "failed_count": 0,
                    "done_count": 0,
                },
                "runtime_components": [],
                "errors": [],
            },
        ),
        patch(
            "aats.data_platform.live_query_adapter.check_live_db_health",
            return_value={"healthy": True, "connection_ok": True, "tables_checked": {}},
        ),
        patch(
            "aats.bootstrap.active_parameters.load_all_active_parameter_sets",
            return_value={},
        ),
    ):
        payload = query_rdp_health(tmp_path)

    assert payload["overall_health"] == "blocked"
    assert "rdp_daemon_status_missing" in payload["blocking_reasons"]


def test_pre_apply_gate_fails_closed_on_rule_exception_in_prod(tmp_path: Path) -> None:
    def broken_rule(_ctx: dict) -> None:
        raise RuntimeError("postgresql://user:secret@host/db")

    with patch.dict(os.environ, {"RDP_ENV": "prod"}, clear=False):
        result = run_pre_apply_gate(
            tmp_path,
            "rec_prod_broken",
            rules=[broken_rule],
            save_result=False,
        )

    assert result["allow_apply"] is False
    assert result["gate_status"] == "block"
    serialized = json.dumps(result)
    assert "gate_rule_execution_failed" in serialized
    assert "secret" not in serialized
    assert "postgresql://" not in serialized


def test_pre_apply_gate_keeps_warn_semantics_on_rule_exception_in_dev(tmp_path: Path) -> None:
    def broken_rule(_ctx: dict) -> None:
        raise RuntimeError("postgresql://user:secret@host/db")

    with patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False):
        result = run_pre_apply_gate(
            tmp_path,
            "rec_dev_broken",
            rules=[broken_rule],
            save_result=False,
        )

    assert result["allow_apply"] is True
    assert result["gate_status"] == "warn"
    serialized = json.dumps(result)
    assert "gate_rule_execution_failed" in serialized
    assert "secret" not in serialized
    assert "postgresql://" not in serialized


def test_gate_rules_can_read_runtime_contract_without_legacy_scattered_fields() -> None:
    alert_result = check_current_alerts(
        {
            "runtime_contract": {
                "environment": "prod",
                "strict_environment": True,
                "current_alerts": {
                    "overall_status": "critical",
                    "critical_alerts": 1,
                    "warning_alerts": 0,
                },
            }
        }
    )
    live_db_result = check_live_db_health(
        {
            "runtime_contract": {
                "environment": "dev",
                "strict_environment": False,
                "live_db_health": {
                    "healthy": False,
                    "errors": ["RDP_LIVE_DATABASE_URL missing"],
                },
            }
        }
    )

    assert alert_result.passed is False
    assert alert_result.severity == "block"
    assert live_db_result.passed is True
    assert live_db_result.severity == "warn"


def test_build_gate_context_includes_runtime_contract(tmp_path: Path) -> None:
    _seed_rdp_health_artifacts(tmp_path)

    with (
        patch.dict(os.environ, {"RDP_ENV": "prod"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": []},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={"parameter_sets": [{"parameter_set_id": "ps_1"}]},
        ),
        patch(
            "aats.data_platform.live_query_adapter.check_live_db_health",
            return_value={"healthy": True, "connection_ok": True, "tables_checked": {}},
        ),
    ):
        ctx = build_gate_context(tmp_path, "rec_contract")

    runtime_contract = ctx["runtime_contract"]
    assert runtime_contract["version"] == 1
    assert runtime_contract["environment"] == "prod"
    assert runtime_contract["strict_environment"] is True
    assert "current_alerts" in runtime_contract
    assert "latest_workflow_runs" in runtime_contract
    assert "live_db_health" in runtime_contract
    assert ctx["current_alerts"] == runtime_contract["current_alerts"]
    assert ctx["latest_workflow_runs"] == runtime_contract["latest_workflow_runs"]
    assert ctx["live_db_health"] == runtime_contract["live_db_health"]


# ── P0-2 阶段 C：DB 为单一真源，写失败 → 拒绝 apply；JSON 导出由 flag 控制 ──


def _ok_rule(_ctx: dict) -> object:
    from aats.data_platform.production_workflow.gate_rules import GateCheckResult

    return GateCheckResult(
        name="ok_rule",
        category="test",
        passed=True,
        severity="pass",
        detail="ok",
    )


def test_run_pre_apply_gate_db_failure_blocks_apply(tmp_path: Path) -> None:
    """DB 不可达时 gate 必须拒绝 apply，不得静默返回 pass."""

    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.try_governance_db",
            return_value=(None, False),
        ),
    ):
        result = run_pre_apply_gate(
            tmp_path,
            "rec_db_down",
            rules=[_ok_rule],
            save_result=True,
        )

    assert result["allow_apply"] is False
    assert result["gate_status"] == "error"
    assert any(
        "gate_persistence" in reason for reason in result["blocking_reasons"]
    )


def test_run_pre_apply_gate_db_exception_blocks_apply(tmp_path: Path) -> None:
    """DB 可达但写入异常时同样拒绝 apply."""

    class _BoomEngine:
        def dispose(self) -> None:
            return None

    def _boom_record(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("postgresql://user:secret@host/db")

    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.try_governance_db",
            return_value=(_BoomEngine(), True),
        ),
        patch(
            "aats.data_platform.governance.operational_state_db.db_record_gate_result",
            side_effect=_boom_record,
        ),
    ):
        result = run_pre_apply_gate(
            tmp_path,
            "rec_db_exc",
            rules=[_ok_rule],
            save_result=True,
        )

    assert result["allow_apply"] is False
    assert result["gate_status"] == "error"
    serialized = json.dumps(result)
    assert "gate_persistence_failed" in serialized
    assert "secret" not in serialized
    assert "postgresql://" not in serialized


def test_run_pre_apply_gate_json_export_disabled_by_default(tmp_path: Path) -> None:
    """默认不再写 JSON / Markdown 副本——artifacts/gates/ 必须保持空."""

    class _OKEngine:
        def dispose(self) -> None:
            return None

    env = {k: v for k, v in os.environ.items() if k != "AATS_P0_GATE_JSON_EXPORT"}
    env["RDP_ENV"] = "dev"

    with (
        patch.dict(os.environ, env, clear=True),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.try_governance_db",
            return_value=(_OKEngine(), True),
        ),
        patch(
            "aats.data_platform.governance.operational_state_db.db_record_gate_result",
            return_value=None,
        ),
    ):
        result = run_pre_apply_gate(
            tmp_path,
            "rec_no_export",
            rules=[_ok_rule],
            save_result=True,
        )

    assert result["allow_apply"] is True
    assert result["gate_status"] == "pass"
    gates_dir = tmp_path / "artifacts" / "production_workflow" / "gates"
    assert not gates_dir.exists() or not list(gates_dir.iterdir())


def test_run_pre_apply_gate_json_export_enabled_by_flag(tmp_path: Path) -> None:
    """AATS_P0_GATE_JSON_EXPORT=on 时继续导出 JSON + Markdown 副本."""

    class _OKEngine:
        def dispose(self) -> None:
            return None

    env = {k: v for k, v in os.environ.items()}
    env["RDP_ENV"] = "dev"
    env["AATS_P0_GATE_JSON_EXPORT"] = "on"

    with (
        patch.dict(os.environ, env, clear=True),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.try_governance_db",
            return_value=(_OKEngine(), True),
        ),
        patch(
            "aats.data_platform.governance.operational_state_db.db_record_gate_result",
            return_value=None,
        ),
    ):
        result = run_pre_apply_gate(
            tmp_path,
            "rec_export_on",
            rules=[_ok_rule],
            save_result=True,
        )

    assert result["allow_apply"] is True
    gate_run_id = result["gate_run_id"]
    gate_dir = tmp_path / "artifacts" / "production_workflow" / "gates" / gate_run_id
    assert (gate_dir / "pre_apply_gate_result.json").exists()
    assert (gate_dir / "pre_apply_gate_report.md").exists()
