from __future__ import annotations

from pathlib import Path

from aats.data_platform.migrations._batch_b import BATCH_B_STAGES
from aats.data_platform.rdp_models import RdpBase


ROOT = Path(__file__).resolve().parents[3]


def test_rdp_run_observability_migration_is_registered_after_stage_16() -> None:
    stage_16 = "batch_b_16_profit_readiness_governance"
    stage_17 = "batch_b_17_rdp_run_observability"
    assert BATCH_B_STAGES[-1] == stage_17
    assert BATCH_B_STAGES.index(stage_17) == BATCH_B_STAGES.index(stage_16) + 1


def test_rdp_run_observability_orm_contract() -> None:
    runs = RdpBase.metadata.tables["governance.rdp_runs"]
    steps = RdpBase.metadata.tables["governance.rdp_run_steps"]
    events = RdpBase.metadata.tables["governance.rdp_run_events"]
    queue = RdpBase.metadata.tables["governance.rdp_task_queue"]

    assert {
        "run_id",
        "workflow",
        "status",
        "trigger_kind",
        "idempotency_key",
        "heartbeat_at",
        "current_step_key",
        "completed_steps",
        "total_steps",
    } <= set(runs.c.keys())
    assert {"run_id", "attempt_no", "step_key", "status", "allow_failure"} <= set(
        steps.c.keys()
    )
    assert {"run_id", "sequence_no", "event_type", "occurred_at"} <= set(events.c.keys())
    assert {
        "run_id",
        "attempt_no",
        "parent_task_id",
        "trigger_kind",
        "priority_class",
        "heartbeat_at",
        "cancel_requested_at",
    } <= set(queue.c.keys())


def test_rdp_run_observability_migration_and_rollback_cover_all_objects() -> None:
    migration = (
        ROOT / "aats/data_platform/migrations/batch_b_17_rdp_run_observability.sql"
    ).read_text(encoding="utf-8")
    rollback = (
        ROOT
        / "aats/data_platform/migrations/batch_b_17_rdp_run_observability_rollback.sql"
    ).read_text(encoding="utf-8")

    for table in ("rdp_runs", "rdp_run_steps", "rdp_run_events"):
        assert f"CREATE TABLE IF NOT EXISTS governance.{table}" in migration
        assert f"DROP TABLE IF EXISTS governance.{table}" in rollback
    assert "ON CONFLICT (run_id) DO NOTHING" in migration
    assert "status IN ('pending', 'running', 'done', 'failed', 'cancelled')" in migration
    assert "ADD CONSTRAINT chk_rdp_task_status" in rollback
