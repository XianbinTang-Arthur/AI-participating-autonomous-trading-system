from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.schemas.common import utc_now
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.runtime_scope import (
    fill_event_matches_scope,
    inferred_order_state_margin_mode,
    inferred_order_state_product_type,
    order_state_matches_scope,
    portfolio_snapshot_matches_scope,
    reconciliation_report_matches_scope,
    runtime_state_scope,
)


class TestRuntimeScope(unittest.TestCase):
    def test_legacy_order_state_is_inferred_as_derivatives_from_symbol_and_td_mode(self) -> None:
        scope = runtime_state_scope(
            AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                    "default_symbol": "BTC-USDT-SWAP",
                }
            )
        )
        legacy_order = OrderState(
            decision_id="decision_1",
            intent_id="intent_1",
            symbol="BTC-USDT-SWAP",
            client_order_id="clord_1",
            venue="OKX",
            exchange_order_id="ord_1",
            status="SUBMITTED",
            requested_qty=0.01,
            remaining_qty=0.01,
            submitted_ts=utc_now(),
            last_update_ts=utc_now(),
            submission_payload={"tdMode": "cross"},
        )

        self.assertEqual(inferred_order_state_product_type(legacy_order), "derivatives")
        self.assertEqual(inferred_order_state_margin_mode(legacy_order), "cross")
        self.assertTrue(order_state_matches_scope(legacy_order, scope))

    def test_scope_filters_out_spot_snapshot_when_runtime_is_derivatives(self) -> None:
        scope = runtime_state_scope(
            AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                    "default_symbol": "BTC-USDT-SWAP",
                }
            )
        )
        spot_snapshot = PortfolioSnapshot(
            snapshot_ts=utc_now(),
            balances={"USDT": 1000.0, "BTC": 0.01},
            positions=[
                Position(
                    symbol="BTC-USDT",
                    position_qty=0.01,
                    position_notional=700.0,
                    avg_entry_price=70000.0,
                    unrealized_pnl=0.0,
                )
            ],
            cost_basis={},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=1000.0,
            gross_exposure=700.0,
            net_exposure=700.0,
            risk_budget_usage={},
            product_type="spot",
            margin_mode="cash",
        )
        derivatives_report = ReconciliationReport(
            reconciliation_id="recon_1",
            as_of_ts=utc_now(),
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=["BTC-USDT-SWAP"],
            order_diff={},
            fill_diff={},
            balance_diff={},
            position_diff={},
            severity="CLEAN",
        )

        self.assertFalse(portfolio_snapshot_matches_scope(spot_snapshot, scope))
        self.assertTrue(reconciliation_report_matches_scope(derivatives_report, scope))

    def test_fill_with_wrong_symbol_is_out_of_scope(self) -> None:
        scope = runtime_state_scope(
            AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                    "default_symbol": "BTC-USDT-SWAP",
                }
            )
        )
        fill = FillEvent(
            fill_id="fill_1",
            decision_id="decision_1",
            intent_id="intent_1",
            client_order_id="clord_1",
            exchange_order_id="ord_1",
            symbol="BTC-USDT",
            venue="OKX",
            side="buy",
            fill_qty=0.001,
            fill_price=100.0,
            fee_amount=0.1,
            product_type="spot",
            margin_mode="cash",
            liquidity_role="taker",
            exchange_timestamp=utc_now(),
            ingestion_timestamp=utc_now(),
        )

        self.assertFalse(order_state_matches_scope(
            OrderState(
                decision_id="decision_1",
                intent_id="intent_1",
                symbol="BTC-USDT",
                client_order_id="clord_1",
                status="FILLED",
                requested_qty=0.001,
                filled_qty=0.001,
                remaining_qty=0.0,
            ),
            scope,
        ))
        self.assertFalse(fill_event_matches_scope(fill, scope))


if __name__ == "__main__":
    unittest.main()
