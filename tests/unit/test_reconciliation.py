from __future__ import annotations

import unittest

from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.services.reconciliation_service.comparator import StateComparator


class TestReconciliationComparator(unittest.TestCase):
    def test_compare_detects_hard_snapshot_mismatch(self) -> None:
        comparator = StateComparator()
        report = comparator.compare(
            order_states=[
                OrderState(
                    intent_id="intent_1",
                    client_order_id="clord_1",
                    exchange_order_id="paper_1",
                    status="FILLED",
                    submitted_ts=utc_now(),
                    last_update_ts=utc_now(),
                    requested_qty=1.0,
                    filled_qty=1.0,
                    remaining_qty=0.0,
                    average_fill_price=100.0,
                    fees=0.0,
                )
            ],
            fills=[
                FillEvent(
                    fill_id="fill_1",
                    decision_id="decision_1",
                    intent_id="intent_1",
                    client_order_id="clord_1",
                    exchange_order_id="paper_1",
                    symbol="BTC-USDT",
                    side="buy",
                    fill_qty=1.0,
                    fill_price=100.0,
                    fee_amount=0.0,
                    liquidity_role="taker",
                    exchange_timestamp=utc_now(),
                    ingestion_timestamp=utc_now(),
                )
            ],
            stored_snapshot=PortfolioSnapshot(
                snapshot_ts=utc_now(),
                balances={"USDT": 9_000.0},
                positions=[
                    Position(
                        symbol="BTC-USDT",
                        position_qty=0.0,
                        position_notional=0.0,
                        avg_entry_price=0.0,
                        unrealized_pnl=0.0,
                    )
                ],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=9_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
            ),
            reconstructed_snapshot=PortfolioSnapshot(
                snapshot_ts=utc_now(),
                balances={"USDT": 9_900.0},
                positions=[
                    Position(
                        symbol="BTC-USDT",
                        position_qty=1.0,
                        position_notional=100.0,
                        avg_entry_price=100.0,
                        unrealized_pnl=0.0,
                    )
                ],
                cost_basis={"BTC-USDT": 100.0},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=10_000.0,
                gross_exposure=100.0,
                net_exposure=100.0,
                risk_budget_usage={},
            ),
        )

        self.assertEqual(report.severity, "HARD_MISMATCH")
        self.assertTrue(report.halt_required)
        self.assertTrue(report.balance_diff)
        self.assertTrue(report.position_diff["mismatches"])


if __name__ == "__main__":
    unittest.main()
