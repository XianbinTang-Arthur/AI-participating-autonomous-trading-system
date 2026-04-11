from __future__ import annotations

import os
import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from sqlalchemy.exc import OperationalError as SAOperationalError

from tests.support.postgres import temporary_postgres_url


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestTask55ControlPlaneGuards(unittest.IsolatedAsyncioTestCase):
    async def test_phase5_requires_phase4_and_persistent_storage(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "portfolio_ledger_truth_requires_persistent_storage|operator_control_plane_execution_ledger_requires_persistent_storage",
        ):
            await build_runtime(
                AATSSettings.model_validate(
                    {
                        "storage_mode": "memory",
                        "operator_control_plane_execution_ledger_enabled": True,
                        "portfolio_ledger_truth_enabled": True,
                        "recovery_reconciliation_execution_ledger_enabled": True,
                    }
                )
            )

        try:
            with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
                with self.assertRaisesRegex(ValueError, "operator_control_plane_execution_ledger_requires_phase4_recovery"):
                    await build_runtime(
                        AATSSettings.model_validate(
                            {
                                "storage_mode": "postgres",
                                "database_url": database_url,
                                "database_auto_create_schema": True,
                                "database_single_runtime_guard_enabled": False,
                                "portfolio_ledger_truth_enabled": True,
                                "operator_control_plane_execution_ledger_enabled": True,
                            }
                        )
                    )

                with self.assertRaisesRegex(
                    ValueError,
                    "operator_control_plane_execution_ledger_requires_execution_command_flow",
                ):
                    await build_runtime(
                        AATSSettings.model_validate(
                            {
                                "storage_mode": "postgres",
                                "database_url": database_url,
                                "database_auto_create_schema": True,
                                "database_single_runtime_guard_enabled": False,
                                "portfolio_ledger_truth_enabled": True,
                                "recovery_reconciliation_execution_ledger_enabled": True,
                                "operator_control_plane_execution_ledger_enabled": True,
                            }
                        )
                    )
        except SAOperationalError:
            self.skipTest("Postgres 不可达")

    async def test_phase5_disallows_unsafe_writes_and_requires_auth_for_exchange_coupled_runtime(self) -> None:
        try:
            with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
                with self.assertRaisesRegex(ValueError, "operator_control_plane_execution_ledger_disallows_unsafe_write_without_auth"):
                    await build_runtime(
                        AATSSettings.model_validate(
                            {
                                "storage_mode": "postgres",
                                "database_url": database_url,
                                "database_auto_create_schema": True,
                                "database_single_runtime_guard_enabled": False,
                                "execution_command_flow_enabled": True,
                                "portfolio_ledger_truth_enabled": True,
                                "recovery_reconciliation_execution_ledger_enabled": True,
                                "operator_control_plane_execution_ledger_enabled": True,
                                "operator_unsafe_write_without_auth": True,
                            }
                        )
                    )

                with self.assertRaisesRegex(ValueError, "operator_control_plane_execution_ledger_requires_operator_auth"):
                    await build_runtime(
                        AATSSettings.model_validate(
                            {
                                "config_profile": "guarded_simulated_submit_dry_run",
                                "mode": "guarded_live",
                                "market_data_backend": "demo",
                                "execution_backend": "okx",
                                "account_backend": "okx",
                                "account_read_enabled": True,
                                "okx_simulated_trading": True,
                                "live_submit_enabled": False,
                                "guarded_execution_dry_run": True,
                                "storage_mode": "postgres",
                                "database_url": database_url,
                                "database_auto_create_schema": True,
                                "database_single_runtime_guard_enabled": False,
                                "execution_command_flow_enabled": True,
                                "portfolio_ledger_truth_enabled": True,
                                "recovery_reconciliation_execution_ledger_enabled": True,
                                "operator_control_plane_execution_ledger_enabled": True,
                                "operator_auth_enabled": False,
                            }
                        )
                    )
        except SAOperationalError:
            self.skipTest("Postgres 不可达")


if __name__ == "__main__":
    unittest.main()
