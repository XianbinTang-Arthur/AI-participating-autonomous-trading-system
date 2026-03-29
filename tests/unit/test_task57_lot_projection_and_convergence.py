from __future__ import annotations

import os
import unittest
from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
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

    def test_lot_projection_and_reconstruction_preserve_negative_fee_rebate(self) -> None:
        builder = LotBasedProjectionBuilder()
        fills = [
            _fill(fill_id="fill_buy_rebate_open", side="buy", qty="1", price="100", fee="0"),
            _fill(fill_id="fill_sell_rebate_close", side="sell", qty="1", price="110", fee="-1"),
        ]

        state = builder.rebuild_portfolio_state(
            fills=fills,
            balances={"USDT": Decimal("0")},
            default_product_type="spot",
            default_margin_mode="cash",
        )
        reconstructed = PortfolioReconstructionService(
            initial_usdt_balance=Decimal("0"),
            snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
        ).rebuild_snapshot(
            fills=fills,
            price_provider=lambda _symbol: Decimal("110"),
        )

        self.assertEqual(state.positions, {})
        self.assertEqual(state.total_fees_paid, Decimal("-1"))
        self.assertEqual(state.realized_pnl, Decimal("11"))
        self.assertEqual(reconstructed.realized_pnl, Decimal("11"))
        self.assertEqual(reconstructed.balances["USDT"], Decimal("11"))

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

    def test_lot_projection_and_reconstruction_follow_exchange_timestamp_when_ingestion_matches(self) -> None:
        builder = LotBasedProjectionBuilder()
        base_ts = utc_now()
        shared_ingestion = base_ts + timedelta(seconds=5)
        fills = [
            _fill(fill_id="z_buy_100", side="buy", qty="1", price="100").model_copy(
                update={"exchange_timestamp": base_ts, "ingestion_timestamp": shared_ingestion}
            ),
            _fill(fill_id="y_buy_120", side="buy", qty="1", price="120").model_copy(
                update={"exchange_timestamp": base_ts + timedelta(milliseconds=1), "ingestion_timestamp": shared_ingestion}
            ),
            _fill(fill_id="x_sell_110", side="sell", qty="1", price="110").model_copy(
                update={"exchange_timestamp": base_ts + timedelta(milliseconds=2), "ingestion_timestamp": shared_ingestion}
            ),
        ]

        expected_state = PortfolioState(initial_usdt_balance=Decimal("1000"))
        for fill in fills:
            expected_state.apply_fill(fill)
        expected_snapshot = PortfolioSnapshotBuilder(
            pnl_calculator=PortfolioPnLCalculator()
        ).build(state=expected_state, price_provider=lambda _symbol: Decimal("110"))

        lot_book = builder.rebuild_lot_book(fills=fills)
        reconstructed = PortfolioReconstructionService(
            initial_usdt_balance=Decimal("1000"),
            snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
        ).rebuild_snapshot(
            fills=fills,
            price_provider=lambda _symbol: Decimal("110"),
        )

        self.assertEqual(reconstructed.positions[0].position_qty, expected_snapshot.positions[0].position_qty)
        self.assertEqual(reconstructed.positions[0].avg_entry_price, expected_snapshot.positions[0].avg_entry_price)
        self.assertEqual(reconstructed.realized_pnl, expected_snapshot.realized_pnl)
        self.assertEqual(reconstructed.balances["USDT"], expected_snapshot.balances["USDT"])
        self.assertEqual(reconstructed.cost_basis["BTC-USDT"], expected_snapshot.cost_basis["BTC-USDT"])
        open_lot = next(lot for lot in lot_book.lots if lot["status"] == "OPEN")
        self.assertEqual(open_lot["entry_price"], Decimal("120"))
        self.assertEqual(open_lot["signed_quantity_open"], Decimal("1"))
        self.assertEqual(lot_book.realized_pnl, Decimal("10"))

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
