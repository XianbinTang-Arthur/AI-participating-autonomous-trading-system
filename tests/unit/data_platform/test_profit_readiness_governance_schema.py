from __future__ import annotations

from pathlib import Path

from aats.data_platform.migrations._batch_b import BATCH_B_STAGES
from aats.data_platform.rdp_models import (
    ParameterActivationOperationModel,
    ParameterRuntimeAckModel,
    ResearchHoldoutAccessLedgerModel,
)


_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS = _ROOT / "aats" / "data_platform" / "migrations"


def test_profit_readiness_governance_migration_precedes_rdp_run_observability() -> None:
    stage_16 = "batch_b_16_profit_readiness_governance"
    stage_17 = "batch_b_17_rdp_run_observability"
    assert stage_16 in BATCH_B_STAGES
    assert stage_17 in BATCH_B_STAGES
    assert BATCH_B_STAGES.index(stage_17) == BATCH_B_STAGES.index(stage_16) + 1


def test_profit_readiness_governance_schema_has_fail_closed_constraints() -> None:
    sql = (_MIGRATIONS / "batch_b_16_profit_readiness_governance.sql").read_text(
        encoding="utf-8"
    )
    assert "uq_holdout_candidate_fingerprint" in sql
    assert "uq_parameter_activation_nonterminal_scope" in sql
    assert "uq_parameter_runtime_ack" in sql
    assert "expected_process_roles" in sql
    assert "ck_holdout_identity_shape" in sql
    assert "ck_holdout_terminal_shape" in sql
    assert "ck_parameter_activation_identity_shape" in sql
    assert "ck_parameter_activation_terminal_shape" in sql
    assert "ck_parameter_runtime_ack_identity_shape" in sql
    assert "DROP FUNCTION IF EXISTS governance.reject_profit_readiness_writes" in sql


def test_profit_readiness_rollback_preserves_rows_and_disables_writes() -> None:
    rollback = (
        _MIGRATIONS
        / "batch_b_16_profit_readiness_governance_rollback.sql"
    ).read_text(encoding="utf-8")
    assert "DROP TABLE" not in rollback.upper()
    assert "reject_profit_readiness_writes" in rollback
    assert "BEFORE INSERT OR UPDATE OR DELETE" in rollback


def test_profit_readiness_orm_models_match_governance_tables() -> None:
    assert ResearchHoldoutAccessLedgerModel.__table__.schema == "governance"
    assert ParameterActivationOperationModel.__table__.schema == "governance"
    assert ParameterRuntimeAckModel.__table__.schema == "governance"
    assert {
        "candidate_id",
        "holdout_content_fingerprint",
        "status",
    }.issubset(ResearchHoldoutAccessLedgerModel.__table__.columns.keys())
    assert {
        "generation",
        "payload_sha256",
        "expected_process_roles",
        "state",
    }.issubset(ParameterActivationOperationModel.__table__.columns.keys())
    holdout_constraints = {
        constraint.name for constraint in ResearchHoldoutAccessLedgerModel.__table__.constraints
    }
    operation_constraints = {
        constraint.name for constraint in ParameterActivationOperationModel.__table__.constraints
    }
    assert {
        "ck_holdout_access_status",
        "ck_holdout_reason_nonempty",
        "ck_holdout_terminal_shape",
        "uq_holdout_candidate_fingerprint",
    }.issubset(holdout_constraints)
    assert {
        "ck_parameter_activation_operation_type",
        "ck_parameter_activation_state",
        "ck_parameter_activation_roles_nonempty",
        "ck_parameter_activation_reason_nonempty",
        "uq_parameter_activation_generation",
    }.issubset(operation_constraints)
