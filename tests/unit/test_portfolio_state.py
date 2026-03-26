from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance, InstrumentMetadata
from aats.schemas.execution import FillEvent
from aats.services.portfolio_service.positions import PortfolioState


def build_fill(
    *,
    fill_id: str,
    side: str,
    qty: float,
    price: float,
    fee: float,
    fee_currency: str | None = None,
    venue: str = "PAPER",
    symbol: str = "BTC-USDT",
    product_type: str = "spot",
    margin_mode: str = "cash",
    position_mode: str | None = None,
    pos_side: str | None = None,
    instrument_family: str | None = None,
    settle_currency: str | None = None,
) -> FillEvent:
    now = datetime.now(timezone.utc)
    return FillEvent(
        fill_id=fill_id,
        decision_id="decision_test",
        intent_id="intent_test",
        client_order_id="clord_test",
        exchange_order_id="paper_test",
        symbol=symbol,
        venue=venue,
        side=side,
        fill_qty=qty,
        fill_price=price,
        fee_amount=fee,
        fee_currency=fee_currency,
        product_type=product_type,
        margin_mode=margin_mode,
        position_mode=position_mode,
        pos_side=pos_side,
        instrument_family=instrument_family,
        settle_currency=settle_currency,
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
        self.assertEqual(state.balances["USDT"], Decimal("10011.0"))
        self.assertEqual(state.balances["BTC"], Decimal("0"))
        self.assertEqual(state.realized_pnl, Decimal("11.0"))
        self.assertEqual(state.total_fees_paid, Decimal("4.0"))

    def test_reversal_sets_new_cost_basis_on_opposite_side(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)

        state.apply_fill(build_fill(fill_id="fill_buy", side="buy", qty=1.0, price=100.0, fee=0.0))
        reversal = state.apply_fill(build_fill(fill_id="fill_sell", side="sell", qty=2.0, price=90.0, fee=0.0))

        position = state.positions["BTC-USDT"]
        self.assertTrue(reversal.applied)
        self.assertEqual(position.quantity, Decimal("-1.0"))
        self.assertEqual(position.avg_entry_price, Decimal("90.0"))
        self.assertEqual(state.balances["BTC"], Decimal("-1.0"))
        self.assertEqual(state.realized_pnl, Decimal("-10.0"))
        self.assertEqual(state.balances["USDT"], Decimal("10080.0"))

    def test_duplicate_fill_is_ignored_idempotently(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)
        fill = build_fill(fill_id="fill_dupe", side="buy", qty=1.0, price=100.0, fee=0.5)

        first = state.apply_fill(fill)
        second = state.apply_fill(fill)

        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertEqual(state.positions["BTC-USDT"].quantity, Decimal("1.0"))
        self.assertEqual(state.balances["BTC"], Decimal("1.0"))
        self.assertEqual(state.balances["USDT"], Decimal("9899.5"))
        self.assertEqual(state.realized_pnl, Decimal("-0.5"))
        self.assertEqual(state.total_fees_paid, Decimal("0.5"))

    def test_okx_buy_fee_in_base_currency_reduces_base_balance_and_converts_fee_to_quote_pnl(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)

        state.apply_fill(
            build_fill(
                fill_id="fill_okx_buy",
                side="buy",
                qty=0.001,
                price=70_000.0,
                fee=0.0000005,
                fee_currency="BTC",
                venue="OKX",
            )
        )

        self.assertEqual(state.balances["USDT"], Decimal("9930.0"))
        self.assertEqual(state.balances["BTC"], Decimal("0.0009995"))
        self.assertEqual(state.realized_pnl, Decimal("-0.035"))
        self.assertEqual(state.total_fees_paid, Decimal("0.035"))

    def test_okx_legacy_fill_without_fee_currency_infers_spot_fee_side(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)

        state.apply_fill(
            build_fill(
                fill_id="fill_okx_legacy",
                side="buy",
                qty=0.001,
                price=70_000.0,
                fee=0.0000005,
                venue="OKX",
            )
        )

        self.assertEqual(state.balances["USDT"], Decimal("9930.0"))
        self.assertEqual(state.balances["BTC"], Decimal("0.0009995"))

    def test_load_exchange_snapshot_synthesizes_spot_positions_from_balances(self) -> None:
        state = PortfolioState(initial_usdt_balance=0.0)
        now = datetime.now(timezone.utc)

        state.load_exchange_snapshot(
            ExchangeAccountSnapshot(
                account_source="okx",
                fetched_at=now,
                balances=[
                    ExchangeBalance(currency="USDT", total=1_000.0, available=1_000.0, frozen=0.0),
                    ExchangeBalance(currency="BTC", total=0.001, available=0.001, frozen=0.0),
                ],
                positions=[],
                open_orders=[],
                fills=[],
                instruments=[
                    InstrumentMetadata(
                        instrument_id="BTC-USDT",
                        symbol="BTC-USDT",
                        base_currency="BTC",
                        quote_currency="USDT",
                        lot_size=0.0001,
                        tick_size=0.1,
                        min_size=0.0001,
                        state="live",
                    )
                ],
                account_mode="cash",
            )
        )

        self.assertEqual(state.positions["BTC-USDT"].quantity, Decimal("0.001"))

    def test_spot_cash_and_cross_positions_use_distinct_position_keys(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0)

        state.apply_fill(build_fill(fill_id="fill_cash_buy", side="buy", qty=1.0, price=100.0, fee=0.0))
        state.apply_fill(
            build_fill(
                fill_id="fill_cross_sell",
                side="sell",
                qty=0.2,
                price=101.0,
                fee=0.0,
                margin_mode="cross",
            )
        )

        self.assertIn("BTC-USDT", state.positions)
        self.assertIn("BTC-USDT:spot:cross", state.positions)
        self.assertEqual(state.positions["BTC-USDT"].quantity, Decimal("1.0"))
        self.assertEqual(state.positions["BTC-USDT:spot:cross"].quantity, Decimal("-0.2"))

    def test_derivatives_fill_updates_position_without_spot_notional_balance_transfer(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0, default_product_type="derivatives", default_margin_mode="cross")

        state.apply_fill(
            build_fill(
                fill_id="fill_swap_open_short",
                side="sell",
                qty=0.01,
                price=70_000.0,
                fee=0.5,
                fee_currency="USDT",
                venue="OKX",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
            )
        )

        self.assertEqual(state.positions["BTC-USDT-SWAP"].quantity, Decimal("-0.01"))
        self.assertEqual(state.balances["USDT"], Decimal("9999.5"))
        self.assertNotIn("BTC", state.balances)
        self.assertEqual(state.realized_pnl, Decimal("-0.5"))

    def test_derivatives_closing_fill_realizes_pnl_into_quote_balance(self) -> None:
        state = PortfolioState(initial_usdt_balance=10_000.0, default_product_type="derivatives", default_margin_mode="cross")
        state.apply_fill(
            build_fill(
                fill_id="fill_swap_open_long",
                side="buy",
                qty=0.01,
                price=70_000.0,
                fee=0.2,
                fee_currency="USDT",
                venue="OKX",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        state.apply_fill(
            build_fill(
                fill_id="fill_swap_close_long",
                side="sell",
                qty=0.01,
                price=71_000.0,
                fee=0.2,
                fee_currency="USDT",
                venue="OKX",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
            )
        )

        self.assertEqual(state.positions, {})
        self.assertEqual(state.balances["USDT"], Decimal("10009.6"))
        self.assertEqual(state.realized_pnl, Decimal("9.6"))

    def test_derivatives_long_short_mode_tracks_each_leg_by_position_key(self) -> None:
        state = PortfolioState(
            initial_usdt_balance=10_000.0,
            default_product_type="derivatives",
            default_margin_mode="cross",
        )

        state.apply_fill(
            build_fill(
                fill_id="fill_open_long",
                side="buy",
                qty=0.02,
                price=70_000.0,
                fee=0.1,
                fee_currency="USDT",
                venue="OKX",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                position_mode="long_short_mode",
                pos_side="long",
                instrument_family="BTC-USDT",
                settle_currency="USDT",
            )
        )
        state.apply_fill(
            build_fill(
                fill_id="fill_open_short",
                side="sell",
                qty=0.01,
                price=71_000.0,
                fee=0.1,
                fee_currency="USDT",
                venue="OKX",
                symbol="BTC-USDT-SWAP",
                product_type="derivatives",
                margin_mode="cross",
                position_mode="long_short_mode",
                pos_side="short",
                instrument_family="BTC-USDT",
                settle_currency="USDT",
            )
        )

        self.assertEqual(state.positions["BTC-USDT-SWAP:long"].quantity, Decimal("0.02"))
        self.assertEqual(state.positions["BTC-USDT-SWAP:short"].quantity, Decimal("-0.01"))
        self.assertEqual(state.positions["BTC-USDT-SWAP:long"].pos_side, "long")
        self.assertEqual(state.positions["BTC-USDT-SWAP:short"].pos_side, "short")
        self.assertEqual(state.position_quantity_for_symbol("BTC-USDT-SWAP"), Decimal("0.01"))


if __name__ == "__main__":
    unittest.main()
