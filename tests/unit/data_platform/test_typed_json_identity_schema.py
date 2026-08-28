from __future__ import annotations

from pathlib import Path

from aats.data_platform.migrations._batch_b import BATCH_B_STAGES
from aats.data_platform.rdp_models import RdpBase


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "aats/data_platform/migrations/batch_b_20_typed_json_identity.sql"
ROLLBACK = ROOT / (
    "aats/data_platform/migrations/batch_b_20_typed_json_identity_rollback.sql"
)


def test_typed_json_identity_migration_is_registered_and_transactional() -> None:
    assert BATCH_B_STAGES[-1] == "batch_b_20_typed_json_identity"
    migration = MIGRATION.read_text(encoding="utf-8")
    rollback = ROLLBACK.read_text(encoding="utf-8")
    assert migration.count("BEGIN;") == migration.count("COMMIT;") == 1
    assert rollback.count("BEGIN;") == rollback.count("COMMIT;") == 1


def test_typed_json_identity_schema_covers_all_immutable_governance_rows() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    rollback = ROLLBACK.read_text(encoding="utf-8")
    for qualified_table in (
        "governance.parameter_sets",
        "governance.research_round_snapshots",
        "governance.decision_round_snapshots",
    ):
        table = RdpBase.metadata.tables[qualified_table]
        assert "typed_json_identity_sha256" in table.c
        assert any(
            constraint.name is not None
            and constraint.name.endswith("typed_json_identity_sha256")
            for constraint in table.constraints
        )
        assert f"ALTER TABLE {qualified_table}" in migration
        assert f"ALTER TABLE {qualified_table}" in rollback
    assert migration.count("ADD COLUMN IF NOT EXISTS typed_json_identity_sha256") == 3
    assert migration.count("~ '^[0-9a-f]{64}$'") == 3
