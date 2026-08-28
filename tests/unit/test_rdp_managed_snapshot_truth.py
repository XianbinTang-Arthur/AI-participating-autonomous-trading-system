from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aats.data_platform.governance import snapshot_db
from aats.data_platform.governance._exceptions import DBUnavailableError


class _Engine:
    def dispose(self) -> None:
        return None


class _Session:
    def __init__(self, _engine: object) -> None:
        return None

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _managed_empty_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        snapshot_db,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(snapshot_db, "try_governance_db", lambda: (_Engine(), True))
    monkeypatch.setattr(snapshot_db, "Session", _Session)


def test_strict_quality_snapshot_empty_db_does_not_bootstrap_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "artifacts/governance/quality_monitor_summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"generated_at": "2026-08-27T00:00:00+00:00", "summary": {"health": "healthy"}}),
        encoding="utf-8",
    )
    _managed_empty_db(monkeypatch)
    monkeypatch.setattr(snapshot_db, "db_load_governance_snapshot", lambda *_args, **_kwargs: None)
    bootstrap = MagicMock()
    monkeypatch.setattr(snapshot_db, "db_upsert_governance_snapshot", bootstrap)

    payload = snapshot_db.load_governance_snapshot(
        tmp_path,
        snapshot_type=snapshot_db.SNAPSHOT_QUALITY_MONITOR,
        require_managed_db_truth=True,
    )

    assert payload is None
    bootstrap.assert_not_called()


def test_strict_step2_empty_db_does_not_bootstrap_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_dir = tmp_path / "artifacts/research/step2_rounds/20260827_120000_deadbeef"
    round_dir.mkdir(parents=True)
    (round_dir / "round_manifest.json").write_text(
        json.dumps({"round_id": round_dir.name, "status": "succeeded"}),
        encoding="utf-8",
    )
    _managed_empty_db(monkeypatch)
    monkeypatch.setattr(
        snapshot_db,
        "db_load_latest_research_round_snapshot",
        lambda *_args, **_kwargs: None,
    )
    bootstrap = MagicMock()
    monkeypatch.setattr(snapshot_db, "db_upsert_research_round_snapshot", bootstrap)

    payload = snapshot_db.load_latest_research_round_snapshot(
        phase=snapshot_db.ROUND_PHASE_STEP2,
        project_root=tmp_path,
        require_managed_db_truth=True,
    )

    assert payload is None
    bootstrap.assert_not_called()


def test_strict_exact_round_empty_db_does_not_bootstrap_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_dir = (
        tmp_path
        / "artifacts/research/execution_rounds/20260828_000000_deadbeef"
    )
    round_dir.mkdir(parents=True)
    (round_dir / "round_manifest.json").write_text(
        json.dumps(
            {
                "round_id": round_dir.name,
                "phase": "phase4",
                "status": "succeeded",
            }
        ),
        encoding="utf-8",
    )
    _managed_empty_db(monkeypatch)
    monkeypatch.setattr(
        snapshot_db,
        "db_load_research_round_snapshot",
        lambda *_args, **_kwargs: None,
    )
    bootstrap = MagicMock()
    monkeypatch.setattr(
        snapshot_db,
        "db_upsert_research_round_snapshot",
        bootstrap,
    )

    payload = snapshot_db.load_research_round_snapshot(
        round_id=round_dir.name,
        project_root=tmp_path,
        require_managed_db_truth=True,
    )

    assert payload is None
    bootstrap.assert_not_called()


def test_step3_file_can_never_lazy_bootstrap_over_producer_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_dir = (
        tmp_path
        / "artifacts/research/step3_rounds/20260828_000000_deadbeef"
    )
    round_dir.mkdir(parents=True)
    (round_dir / "round_manifest.json").write_text(
        json.dumps(
            {
                "round_id": round_dir.name,
                "phase": "step3",
                "status": "succeeded",
            }
        ),
        encoding="utf-8",
    )
    (round_dir / "parameter_candidates_merged.json").write_text(
        json.dumps({"round_id": round_dir.name, "candidates": {}}),
        encoding="utf-8",
    )
    _managed_empty_db(monkeypatch)
    monkeypatch.setattr(
        snapshot_db,
        "db_load_research_round_snapshot",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        snapshot_db,
        "db_load_latest_research_round_snapshot",
        lambda *_args, **_kwargs: None,
    )
    bootstrap = MagicMock()
    monkeypatch.setattr(
        snapshot_db,
        "db_upsert_research_round_snapshot",
        bootstrap,
    )

    exact = snapshot_db.load_research_round_snapshot(
        round_id=round_dir.name,
        project_root=tmp_path,
    )
    latest = snapshot_db.load_latest_research_round_snapshot(
        phase=snapshot_db.ROUND_PHASE_STEP3,
        project_root=tmp_path,
    )

    assert exact is not None and exact["data_source"] == "file_untrusted"
    assert latest is not None and latest["data_source"] == "file_untrusted"
    assert exact["bootstrap_reason"] == "producer_managed_snapshot_required"
    assert latest["bootstrap_reason"] == "producer_managed_snapshot_required"
    bootstrap.assert_not_called()


def test_strict_quality_snapshot_query_error_denies_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _managed_empty_db(monkeypatch)
    monkeypatch.setattr(
        snapshot_db,
        "db_load_governance_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic read error")),
    )

    with pytest.raises(DBUnavailableError, match="stale file fallback denied"):
        snapshot_db.load_governance_snapshot(
            tmp_path,
            snapshot_type=snapshot_db.SNAPSHOT_QUALITY_MONITOR,
            require_managed_db_truth=True,
        )


def test_strict_exact_round_query_error_denies_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _managed_empty_db(monkeypatch)
    monkeypatch.setattr(
        snapshot_db,
        "db_load_research_round_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic exact round read error")
        ),
    )

    with pytest.raises(DBUnavailableError, match="stale file fallback denied"):
        snapshot_db.load_research_round_snapshot(
            round_id="20260828_000000_deadbeef",
            project_root=tmp_path,
            require_managed_db_truth=True,
        )


def test_governance_snapshot_uses_column_generated_at_over_payload() -> None:
    column_time = datetime(2026, 8, 20, tzinfo=timezone.utc)
    session = MagicMock()
    session.execute.return_value.fetchone.return_value = SimpleNamespace(
        snapshot_type=snapshot_db.SNAPSHOT_QUALITY_MONITOR,
        generated_at=column_time,
        payload={
            "generated_at": "2026-08-27T12:00:00+00:00",
            "summary": {
                "health": "healthy",
                "critical_failures": 0,
                "warning_failures": 0,
            },
        },
    )

    payload = snapshot_db.db_load_governance_snapshot(
        session,
        snapshot_type=snapshot_db.SNAPSHOT_QUALITY_MONITOR,
    )

    assert payload is not None
    assert payload["generated_at"] == column_time.isoformat()


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"round_id": "different_round", "status": "succeeded"},
    ],
)
def test_step2_integrity_rejects_missing_or_mismatched_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: dict[str, object],
) -> None:
    from aats.data_platform.governance import step2_integrity_guard

    monkeypatch.setattr(
        snapshot_db,
        "load_latest_research_round_snapshot",
        lambda **_kwargs: {
            "round_id": "round_1",
            "phase": snapshot_db.ROUND_PHASE_STEP2,
            "status": "succeeded",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "manifest": manifest,
            "data_source": "db",
        },
    )
    monkeypatch.setattr(snapshot_db, "is_snapshot_incomplete", lambda _snapshot: False)

    result = step2_integrity_guard.assess_step2_integrity(tmp_path)

    assert result["ok"] is False
    assert result["code"] == "snapshot_manifest_invalid"


def test_step2_integrity_accepts_matching_successful_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import step2_integrity_guard

    monkeypatch.setattr(
        snapshot_db,
        "load_latest_research_round_snapshot",
        lambda **_kwargs: {
            "round_id": "round_1",
            "phase": snapshot_db.ROUND_PHASE_STEP2,
            "status": "succeeded",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "manifest": {"round_id": "round_1"},
            "data_source": "db",
        },
    )
    monkeypatch.setattr(snapshot_db, "is_snapshot_incomplete", lambda _snapshot: False)

    assert step2_integrity_guard.assess_step2_integrity(tmp_path)["ok"] is True


def test_step2_integrity_rejects_future_finished_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from aats.data_platform.governance import step2_integrity_guard

    monkeypatch.setattr(
        snapshot_db,
        "load_latest_research_round_snapshot",
        lambda **_kwargs: {
            "round_id": "round_future",
            "phase": snapshot_db.ROUND_PHASE_STEP2,
            "status": "succeeded",
            "finished_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "manifest": {"round_id": "round_future"},
            "data_source": "db",
        },
    )
    monkeypatch.setattr(snapshot_db, "is_snapshot_incomplete", lambda _snapshot: False)

    result = step2_integrity_guard.assess_step2_integrity(tmp_path)

    assert result["ok"] is False
    assert result["code"] == "snapshot_finished_at_invalid"


def test_managed_workflow_query_error_never_reads_stale_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import _db_util, operational_state_db
    from aats.data_platform.production_workflow import gate_runtime_contract

    stale_path = tmp_path / "artifacts/operations/workflow_runs/governance_cycle.json"
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(
        json.dumps(
            {
                "run_id": "run_stale_success",
                "workflow": "governance_cycle",
                "overall_status": "success",
                "finished_at": "2026-08-27T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _db_util,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(_db_util, "try_governance_db", lambda: (_Engine(), True))
    monkeypatch.setattr(gate_runtime_contract, "Session", _Session)
    monkeypatch.setattr(
        operational_state_db,
        "db_load_latest_workflow_runs",
        lambda _session: (_ for _ in ()).throw(RuntimeError("synthetic workflow read error")),
    )
    monkeypatch.setattr(
        "aats.data_platform.operations.alerting.load_current_alerts",
        lambda _root: None,
    )
    monkeypatch.setattr(
        "aats.data_platform.live_query_adapter.check_live_db_health",
        lambda: {"healthy": True, "connection_ok": True, "tables_checked": {}},
    )

    contract = gate_runtime_contract.build_gate_runtime_contract(
        tmp_path,
        environment="prod",
    )

    assert contract["workflow_runs_available"] is False
    assert contract["latest_workflow_runs"] == {}


def test_managed_empty_workflow_db_never_reads_stale_success_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import _db_util, operational_state_db, rdp_task_db
    from aats.data_platform.production_workflow import gate_runtime_contract

    stale_path = tmp_path / "artifacts/operations/workflow_runs/governance_cycle.json"
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(
        json.dumps(
            {
                "run_id": "stale_success",
                "attempt_no": 1,
                "workflow": "governance_cycle",
                "overall_status": "success",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _db_util,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(_db_util, "try_governance_db", lambda: (_Engine(), True))
    monkeypatch.setattr(gate_runtime_contract, "Session", _Session)
    monkeypatch.setattr(
        operational_state_db,
        "db_load_latest_workflow_runs",
        lambda _session: {},
    )
    monkeypatch.setattr(
        rdp_task_db,
        "db_get_latest_task_for_workflow",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        gate_runtime_contract,
        "_load_latest_decision_round_snapshot",
        lambda *_args, **_kwargs: None,
    )

    assert gate_runtime_contract._collect_latest_workflow_runs(tmp_path) == {}


def test_managed_pre_apply_context_never_reads_stale_round_when_db_is_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.decision_system import recommendation_registry
    from aats.data_platform.governance import parameter_registry
    from aats.data_platform.metrics import release_effectiveness
    from aats.data_platform.operations import environment_guard
    from aats.data_platform.production_workflow import pre_apply_gate

    stale_dir = tmp_path / "artifacts/decision_rounds/20991231_235959_stale"
    stale_dir.mkdir(parents=True)
    (stale_dir / "round_manifest.json").write_text(
        json.dumps(
            {
                "round_id": stale_dir.name,
                "status": "succeeded",
                "phase": "phase6",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pre_apply_gate,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(pre_apply_gate, "try_governance_db", lambda: (None, False))
    monkeypatch.setattr(
        recommendation_registry,
        "load_recommendation_registry",
        lambda _path: {"recommendations": []},
    )
    monkeypatch.setattr(
        recommendation_registry,
        "find_recommendation",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        recommendation_registry,
        "load_active_decision_registry",
        lambda _path: {"decisions": []},
    )
    monkeypatch.setattr(pre_apply_gate, "load_governance_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release_effectiveness, "pending_rollback_combos", lambda _root: {})
    monkeypatch.setattr(parameter_registry, "load_registry", lambda *_args, **_kwargs: {"parameter_sets": []})
    monkeypatch.setattr(environment_guard, "get_current_environment", lambda: "dev")
    monkeypatch.setattr(environment_guard, "get_policy", lambda _env: {})
    monkeypatch.setattr(
        pre_apply_gate,
        "build_gate_runtime_contract",
        lambda _root, *, environment: {
            "environment": environment,
            "strict_environment": False,
            "current_alerts": None,
            "latest_workflow_runs": {},
            "workflow_runs_available": True,
            "live_db_health": {},
        },
    )

    context = pre_apply_gate.build_gate_context(tmp_path, "missing_rec")

    assert context["latest_decision_round"] == {}


def test_managed_workflow_reader_blocks_old_success_after_new_failed_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from aats.data_platform.governance import _db_util, operational_state_db, rdp_task_db
    from aats.data_platform.production_workflow import gate_runtime_contract

    report_time = datetime.now(timezone.utc) - timedelta(minutes=20)
    task_time = report_time + timedelta(minutes=10)
    monkeypatch.setattr(
        _db_util,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(_db_util, "try_governance_db", lambda: (_Engine(), True))
    monkeypatch.setattr(gate_runtime_contract, "Session", _Session)
    monkeypatch.setattr(
        operational_state_db,
        "db_load_latest_workflow_runs",
        lambda _session: {
            workflow: {
                "run_id": f"old_{workflow}",
                "workflow": workflow,
                "overall_status": "success",
                "started_at": (report_time - timedelta(minutes=5)).isoformat(),
                "finished_at": report_time.isoformat(),
            }
            for workflow in gate_runtime_contract._GATE_CRITICAL_WORKFLOWS
        },
    )

    def _latest_task(_session: object, workflow: str) -> dict | None:
        if workflow != "data_maintenance":
            return None
        return {
            "task_id": "task_failed_after_old_success",
            "run_id": "logical_failed_after_old_success",
            "attempt_no": 2,
            "workflow": workflow,
            "status": "failed",
            "requested_at": task_time.isoformat(),
            "started_at": task_time.isoformat(),
            "finished_at": (task_time + timedelta(minutes=1)).isoformat(),
        }

    monkeypatch.setattr(rdp_task_db, "db_get_latest_task_for_workflow", _latest_task)
    monkeypatch.setattr(
        gate_runtime_contract,
        "_load_latest_decision_round_snapshot",
        lambda *_args, **_kwargs: None,
    )

    latest = gate_runtime_contract._collect_latest_workflow_runs(tmp_path)

    assert latest["data_maintenance"]["overall_status"] == "task_failed"
    assert latest["data_maintenance"]["reconciliation_required"] is True


def test_exact_done_queue_attempt_and_success_report_are_bound() -> None:
    from aats.data_platform.production_workflow.gate_runtime_contract import (
        _reconcile_workflow_runs_with_task_attempts,
    )

    now = datetime.now(timezone.utc).isoformat()
    reconciled = _reconcile_workflow_runs_with_task_attempts(
        {
            "data_maintenance": {
                "run_id": "logical_run_1",
                "attempt_no": 2,
                "workflow": "data_maintenance",
                "overall_status": "success",
                "started_at": now,
                "finished_at": now,
            }
        },
        {
            "data_maintenance": {
                "task_id": "task_1",
                "run_id": "logical_run_1",
                "attempt_no": 2,
                "workflow": "data_maintenance",
                "status": "done",
                "requested_at": now,
                "started_at": now,
                "finished_at": now,
            }
        },
    )

    run = reconciled["data_maintenance"]
    assert run["overall_status"] == "success"
    assert run["queue_task_status"] == "done"
    assert run["queue_identity_matched"] is True


@pytest.mark.parametrize(
    ("task_status", "report_run_id", "report_attempt_no", "reason_code"),
    [
        ("failed", "logical_run_1", 2, "latest_queue_task_not_done"),
        ("running", "logical_run_1", 2, "latest_queue_task_not_done"),
        ("pending", "logical_run_1", 2, "latest_queue_task_not_done"),
        ("done", "different_run", 2, "workflow_report_identity_mismatch"),
        ("done", "logical_run_1", 1, "workflow_report_identity_mismatch"),
        ("done", "logical_run_1", "2", "workflow_report_identity_mismatch"),
    ],
)
def test_managed_workflow_requires_done_and_exact_report_identity(
    task_status: str,
    report_run_id: str,
    report_attempt_no: object,
    reason_code: str,
) -> None:
    from aats.data_platform.production_workflow.gate_runtime_contract import (
        _reconcile_workflow_runs_with_task_attempts,
    )

    now = datetime.now(timezone.utc).isoformat()
    reconciled = _reconcile_workflow_runs_with_task_attempts(
        {
            "data_maintenance": {
                "run_id": report_run_id,
                "attempt_no": report_attempt_no,
                "workflow": "data_maintenance",
                "overall_status": "success",
                "started_at": now,
                "finished_at": now,
            }
        },
        {
            "data_maintenance": {
                "task_id": "task_1",
                "run_id": "logical_run_1",
                "attempt_no": 2,
                "workflow": "data_maintenance",
                "status": task_status,
                "requested_at": now,
                "started_at": now,
                "finished_at": now if task_status == "done" else None,
            }
        },
    )

    run = reconciled["data_maintenance"]
    assert run["overall_status"] != "success"
    assert run["reconciliation_required"] is True
    assert run["reason_code"] == reason_code


def test_managed_workflow_report_without_latest_queue_task_is_not_success() -> None:
    from aats.data_platform.production_workflow.gate_runtime_contract import (
        _reconcile_workflow_runs_with_task_attempts,
    )

    now = datetime.now(timezone.utc).isoformat()
    reconciled = _reconcile_workflow_runs_with_task_attempts(
        {
            "data_maintenance": {
                "run_id": "orphan_report",
                "attempt_no": 1,
                "workflow": "data_maintenance",
                "overall_status": "success",
                "started_at": now,
                "finished_at": now,
            }
        },
        {"data_maintenance": None},
    )

    assert reconciled["data_maintenance"]["overall_status"] == "task_missing"
    assert (
        reconciled["data_maintenance"]["reason_code"]
        == "latest_queue_task_missing"
    )
