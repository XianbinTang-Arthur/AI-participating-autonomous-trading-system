from __future__ import annotations

import os
import unittest
from decimal import Decimal

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from tests.support.postgres import temporary_postgres_url


def _fill(
    *,
    fill_id: str,
    side: str,
    qty: str,
    price: str,
    fee: str = "0",
    product_type: str = "spot",
    position_mode: str | None = None,
    pos_side: str | None = None,
    strategy_sleeve_id: str | None = None,
    allocation_id: str | None = None,
) -> FillEvent:
    timestamp = utc_now()
    return FillEvent(
        fill_id=fill_id,
        decision_id=f"decision_{fill_id}",
        intent_id=f"intent_{fill_id}",
        client_order_id=f"order_{fill_id}",
        exchange_order_id=f"venue_{fill_id}",
        symbol="BTC-USDT",
        venue="PAPER",
        side=side,  # type: ignore[arg-type]
        fill_qty=Decimal(qty),
        fill_price=Decimal(price),
        fee_amount=Decimal(fee),
        fee_currency="USDT",
        product_type=product_type,  # type: ignore[arg-type]
        target_leverage=1.0,
        margin_mode="cash",
        position_mode=position_mode,  # type: ignore[arg-type]
        pos_side=pos_side,  # type: ignore[arg-type]
        strategy_sleeve_id=strategy_sleeve_id,
        allocation_id=allocation_id,
        exposure_side="long" if side == "buy" else "short",
        execution_action="enter",
        position_intent="open_long" if side == "buy" else "close_long",
        liquidity_role="taker",
        exchange_timestamp=timestamp,
        ingestion_timestamp=timestamp,
        order_status_after_fill="FILLED",
    )


class TestTask57LotProjectionAndConvergence(unittest.IsolatedAsyncioTestCase):
    def test_lot_projection_uses_fifo_cost_basis_for_partial_close_and_reverse(self) -> None:
        builder = LotBasedProjectionBuilder()
        fills = [
            _fill(fill_id="fill_buy_1", side="buy", qty="1", price="100"),
            _fill(fill_id="fill_buy_2", side="buy", qty="1", price="110"),
            _fill(fill_id="fill_sell_1", side="sell", qty="1.5", price="120"),
            _fill(fill_id="fill_sell_2", side="sell", qty="1", price="90"),
        ]

        state = builder.rebuild_portfolio_state(
            fills=fills,
            balances={"USDT": Decimal("0")},
            default_product_type="spot",
            default_margin_mode="cash",
        )

        position = state.positions["BTC-USDT"]
        self.assertEqual(position.quantity, Decimal("-0.5"))
        self.assertEqual(position.avg_entry_price, Decimal("90"))
        self.assertEqual(state.realized_pnl, Decimal("15"))
        self.assertEqual(state.total_fees_paid, Decimal("0"))

    def test_lot_projection_keeps_long_short_mode_legs_separate(self) -> None:
        builder = LotBasedProjectionBuilder()
        fills = [
            _fill(
                fill_id="fill_long_open",
                side="buy",
                qty="2",
                price="100",
                product_type="derivatives",
                position_mode="long_short_mode",
                pos_side="long",
            ),
            _fill(
                fill_id="fill_short_open",
                side="sell",
                qty="1",
                price="110",
                product_type="derivatives",
                position_mode="long_short_mode",
                pos_side="short",
            ),
            _fill(
                fill_id="fill_long_reduce",
                side="sell",
                qty="0.5",
                price="120",
                product_type="derivatives",
                position_mode="long_short_mode",
                pos_side="long",
            ),
        ]

        state = builder.rebuild_portfolio_state(
            fills=fills,
            balances={"USDT": Decimal("0")},
            default_product_type="derivatives",
            default_margin_mode="cross",
        )

        self.assertEqual(state.positions["BTC-USDT:long"].quantity, Decimal("1.5"))
        self.assertEqual(state.positions["BTC-USDT:short"].quantity, Decimal("-1"))
        self.assertEqual(state.realized_pnl, Decimal("10"))

    def test_lot_projection_preserves_strategy_sleeve_identity(self) -> None:
        builder = LotBasedProjectionBuilder()
        fills = [
            _fill(
                fill_id="fill_open",
                side="buy",
                qty="1",
                price="100",
                strategy_sleeve_id="sleeve-smart-arb",
                allocation_id="alloc-1",
            ),
            _fill(
                fill_id="fill_close",
                side="sell",
                qty="0.4",
                price="110",
                strategy_sleeve_id="sleeve-smart-arb",
                allocation_id="alloc-1",
            ),
        ]

        lot_book = builder.rebuild_lot_book(fills=fills)

        self.assertTrue(lot_book.lots)
        self.assertEqual(lot_book.lots[0]["strategy_sleeve_id"], "sleeve-smart-arb")
        self.assertEqual(lot_book.lots[0]["allocation_id"], "alloc-1")
        close_events = [event for event in lot_book.events if event["event_type"] == "close"]
        self.assertTrue(close_events)
        self.assertEqual(close_events[0]["strategy_sleeve_id"], "sleeve-smart-arb")
        self.assertEqual(close_events[0]["allocation_id"], "alloc-1")

    async def test_financial_convergence_mode_requires_all_strict_guards(self) -> None:
        if not os.getenv("AATS_DATABASE_URL"):
            raise unittest.SkipTest("AATS_DATABASE_URL is required for PostgreSQL-backed tests")
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            with self.assertRaisesRegex(
                ValueError,
                "financial_convergence_mode_requires_execution_command_flow|operator_control_plane_execution_ledger_requires_execution_command_flow",
            ):
                await build_runtime(
                    AATSSettings.model_validate(
                        {
                            "storage_mode": "postgres",
                            "database_url": database_url,
                            "database_auto_create_schema": True,
                            "database_single_runtime_guard_enabled": True,
                            "portfolio_ledger_truth_enabled": True,
                            "recovery_reconciliation_execution_ledger_enabled": True,
                            "operator_control_plane_execution_ledger_enabled": True,
                            "financial_convergence_mode_enabled": True,
                        }
                    )
                )
            with self.assertRaisesRegex(ValueError, "financial_convergence_mode_requires_single_runtime_guard"):
                await build_runtime(
                    AATSSettings.model_validate(
                        {
                            "storage_mode": "postgres",
                            "database_url": database_url,
                            "database_auto_create_schema": True,
                            "database_single_runtime_guard_enabled": False,
                            "event_persistence_mode": "strict",
                            "execution_command_flow_enabled": True,
                            "portfolio_ledger_truth_enabled": True,
                            "recovery_reconciliation_execution_ledger_enabled": True,
                            "operator_control_plane_execution_ledger_enabled": True,
                            "financial_convergence_mode_enabled": True,
                        }
                    )
                )


if __name__ == "__main__":
    unittest.main()
