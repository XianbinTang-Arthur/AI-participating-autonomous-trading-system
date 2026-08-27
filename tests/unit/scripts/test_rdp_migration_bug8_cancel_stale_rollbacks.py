from __future__ import annotations

from unittest.mock import patch

from scripts.rdp_migration_bug8_cancel_stale_rollbacks import run_migration


def test_legacy_bulk_cancel_apply_is_permanently_blocked() -> None:
    with patch(
        "scripts.rdp_migration_bug8_cancel_stale_rollbacks.try_governance_db"
    ) as connect:
        assert run_migration(dry_run=False) == 3
    connect.assert_not_called()
