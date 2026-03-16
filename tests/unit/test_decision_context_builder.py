from __future__ import annotations

import unittest
from datetime import datetime, timezone

from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.decision_engine.context_builder import DecisionContextBuilder


class TestDecisionContextBuilder(unittest.TestCase):
    def test_position_qty_falls_back_to_base_balance_for_spot_snapshots(self) -> None:
        snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": 1_000.0, "BTC": 0.0015},
            positions=[],
            cost_basis={},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=1_000.0,
            gross_exposure=0.0,
            net_exposure=0.0,
            risk_budget_usage={},
        )

        quantity = DecisionContextBuilder._position_qty(snapshot, "BTC-USDT")

        self.assertAlmostEqual(quantity, 0.0015)


if __name__ == "__main__":
    unittest.main()
