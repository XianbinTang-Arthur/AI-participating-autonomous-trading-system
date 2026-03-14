from __future__ import annotations

import unittest
from datetime import datetime, timezone

from aats.schemas.execution import FillEvent
from aats.services.portfolio_service.positions import PortfolioState


def build_fill(
    *,
    fill_id: str,
    side: str,
    qty: float,
    price: float,
    fee: float,
) -> FillEvent:
    now = datetime.now(timezone.utc)
    return FillEvent(
        fill_id=fill_id,
        decision_id="decision_test",
        intent_id="intent_test",
        client_order_id="clord_test",
        exchange_order_id="paper_test",
        symbol="BTC-USDT",
        side=side,
        fill_qty=qty,
        fill_price=price,
        fee_amount=fee,
        liquidity_role="taker",
        exchange_timestamp=now,
        ingestion_timestamp=now,
    )


class TestPortfolioState(unittest.TestCase):
    def test_long_position_add_reduce_and_close_tracks_average_cost_and_realized_pnl(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)

        first_buy = state.apply_fill(build_fill(fill_id="fill_1", side="buy", qty=1.0, price=100.0, fee=1.0))
        second_buy = state.apply_fill(build_fill(fill_id="fill_2", side="buy", qty=1.0, price=120.0, fee=1.0))
        partial_sell = state.apply_fill(build_fill(fill_id="fill_3", side="sell", qty=1.0, price=130.0, fee=1.0))
        final_sell = state.apply_fill(build_fill(fill_id="fill_4", side="sell", qty=1.0, price=105.0, fee=1.0))

        self.assertTrue(first_buy.applied)
        self.assertTrue(second_buy.applied)
        self.assertTrue(partial_sell.applied)
        self.assertTrue(final_sell.applied)

        self.assertEqual(state.positions, {})
        self.assertAlmostEqual(state.balances["USDT"], 10_011.0)
        self.assertAlmostEqual(state.realized_pnl, 11.0)
        self.assertAlmostEqual(state.total_fees_paid, 4.0)

    def test_reversal_sets_new_cost_basis_on_opposite_side(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)

        state.apply_fill(build_fill(fill_id="fill_buy", side="buy", qty=1.0, price=100.0, fee=0.0))
        reversal = state.apply_fill(build_fill(fill_id="fill_sell", side="sell", qty=2.0, price=90.0, fee=0.0))

        position = state.positions["BTC-USDT"]
        self.assertTrue(reversal.applied)
        self.assertAlmostEqual(position.quantity, -1.0)
        self.assertAlmostEqual(position.avg_entry_price, 90.0)
        self.assertAlmostEqual(state.realized_pnl, -10.0)
        self.assertAlmostEqual(state.balances["USDT"], 10_080.0)

    def test_duplicate_fill_is_ignored_idempotently(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)
        fill = build_fill(fill_id="fill_dupe", side="buy", qty=1.0, price=100.0, fee=0.5)

        first = state.apply_fill(fill)
        second = state.apply_fill(fill)

        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertAlmostEqual(state.positions["BTC-USDT"].quantity, 1.0)
        self.assertAlmostEqual(state.balances["USDT"], 9_899.5)
        self.assertAlmostEqual(state.realized_pnl, -0.5)
        self.assertAlmostEqual(state.total_fees_paid, 0.5)


if __name__ == "__main__":
    unittest.main()

