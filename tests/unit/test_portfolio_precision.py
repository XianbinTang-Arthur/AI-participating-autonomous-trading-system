from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aats.schemas.execution import FillEvent
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder


def build_fill(
    *,
    fill_id: str,
    side: str,
    qty: float,
    price: float,
    fee: float = 0.0,
    symbol: str = "BTC-USDT",
    venue: str = "PAPER",
    product_type: str = "spot",
    margin_mode: str = "cash",
) -> FillEvent:
    now = datetime.now(timezone.utc)
    return FillEvent(
        fill_id=fill_id,
        decision_id="decision_precision",
        intent_id="intent_precision",
        client_order_id="clord_precision",
        exchange_order_id="ord_precision",
        symbol=symbol,
        venue=venue,
        side=side,
        fill_qty=qty,
        fill_price=price,
        fee_amount=fee,
        fee_currency="USDT",
        product_type=product_type,
        margin_mode=margin_mode,
        liquidity_role="taker",
        exchange_timestamp=now,
        ingestion_timestamp=now,
    )


class TestPortfolioPrecision(unittest.TestCase):
    def test_micro_bitcoin_fills_preserve_decimal_balance(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)

        state.apply_fill(build_fill(fill_id="fill_1", side="buy", qty=0.00000001, price=70_000.12345678))
        state.apply_fill(build_fill(fill_id="fill_2", side="buy", qty=0.00000001, price=70_000.12345678))
        state.apply_fill(build_fill(fill_id="fill_3", side="buy", qty=0.00000001, price=70_000.12345678))

        self.assertAlmostEqual(state.balances["BTC"], 0.00000003, places=16)
        self.assertAlmostEqual(state.positions["BTC-USDT"].quantity, 0.00000003, places=16)

    def test_snapshot_counts_off_position_assets_in_collateral(self) -> None:
        state = PortfolioState(initial_usdt_balance=1_000.0)
        state.balances["ETH"] = Decimal("2")
        snapshot = PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()).build(
            state=state,
            price_provider=lambda symbol: 2_000.0 if symbol == "ETH-USDT" else 0.0,
        )

        self.assertEqual(snapshot.cash_equity, Decimal("1000.0"))
        self.assertEqual(snapshot.off_position_asset_equity, Decimal("4000.0"))
        self.assertEqual(snapshot.collateral_value, Decimal("5000.0"))
        self.assertEqual(snapshot.total_equity, Decimal("5000.0"))


if __name__ == "__main__":
    unittest.main()
