from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings


class TestRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_recovers_portfolio_state_from_persisted_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._sqlite_settings(Path(temp_dir))
            runtime = await build_runtime(settings)
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )
                original_snapshot = runtime.portfolio_repo.latest()
                self.assertIsNotNone(original_snapshot)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

            recovered_runtime = await build_runtime(settings, bootstrap_portfolio_snapshot=False)
            try:
                self.assertEqual(recovered_runtime.recovery_status.status, "recovered")
                self.assertFalse(recovered_runtime.recovery_status.halted)
                recovered_snapshot = recovered_runtime.portfolio_repo.latest()
                self.assertIsNotNone(recovered_snapshot)
                self.assertEqual(
                    recovered_snapshot.model_dump(mode="json"),
                    original_snapshot.model_dump(mode="json"),
                )
            finally:
                if recovered_runtime.database_runtime is not None:
                    recovered_runtime.database_runtime.dispose()

    @staticmethod
    def _sqlite_settings(temp_dir: Path) -> AATSSettings:
        database_path = (temp_dir / "aats_recovery.db").resolve().as_posix()
        return AATSSettings.model_validate(
            {
                "storage_mode": "postgres",
                "database_url": f"sqlite+pysqlite:///{database_path}",
                "database_auto_create_schema": True,
                "local_publish_iterations": 4,
                "local_publish_interval_seconds": 0.0,
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
            }
        )


if __name__ == "__main__":
    unittest.main()
