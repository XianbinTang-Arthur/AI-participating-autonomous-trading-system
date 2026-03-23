from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings


class TestTask53LedgerTruthGuards(unittest.IsolatedAsyncioTestCase):
    async def test_portfolio_ledger_truth_requires_persistent_storage(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "portfolio_ledger_truth_enabled": True,
            }
        )

        with self.assertRaisesRegex(ValueError, "portfolio_ledger_truth_requires_persistent_storage"):
            await build_runtime(settings)
