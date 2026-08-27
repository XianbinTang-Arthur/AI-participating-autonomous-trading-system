from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from aats.data_platform.rdp_models import RdpBase


def test_release_effectiveness_action_proof_schema_is_immutable_and_exact() -> None:
    table = RdpBase.metadata.tables[
        "governance.release_effectiveness_action_proofs"
    ]

    assert set(table.columns.keys()) == {
        "id",
        "release_id",
        "attempt_id",
        "outcome",
        "proof_kind",
        "started_at_utc",
        "finished_at_utc",
        "operation_id",
        "target_parameter_set_id",
        "observed_active_parameter_set_id",
        "decision_status",
        "fact_observed_at",
        "created_at",
    }
    unique_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert {
        "uq_release_eff_action_proof_release",
        "uq_release_eff_action_proof_attempt",
    } <= unique_names
    check_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "ck_release_eff_action_proof_outcome",
        "ck_release_eff_action_proof_kind",
        "ck_release_eff_action_proof_shape",
    } <= check_names
    foreign_keys = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    assert len(foreign_keys) == 1
    assert foreign_keys[0].name == "fk_release_eff_action_proof_release"
    assert foreign_keys[0].referred_table.fullname == (
        "governance.parameter_releases"
    )
