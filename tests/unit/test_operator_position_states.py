from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aats.schemas.exchange import ExchangeAccountConfiguration, ExchangeAccountSnapshot, ExchangePosition
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.services.operator.query_service import OperatorQueryService


class TestOperatorPositionStates(unittest.TestCase):
    def test_aggregate_local_positions_exposes_dual_leg_state(self) -> None:
        snapshot = PortfolioSnapshot(
            snapshot_ts=datetime.now(timezone.utc),
            balances={"USDT": 75_000.0},
            positions=[
                Position(
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:long",
                    position_qty=Decimal("0.02"),
                    position_notional=Decimal("1400"),
                    avg_entry_price=Decimal("70000"),
                    unrealized_pnl=Decimal("15"),
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                ),
                Position(
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:short",
                    position_qty=Decimal("-0.01"),
                    position_notional=Decimal("-700"),
                    avg_entry_price=Decimal("70500"),
                    unrealized_pnl=Decimal("-3"),
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="short",
                ),
            ],
            cost_basis={},
            realized_pnl=0.0,
            unrealized_pnl=12.0,
            total_equity=75_012.0,
            gross_exposure=2100.0,
            net_exposure=700.0,
            risk_budget_usage={},
            product_type="derivatives",
            margin_mode="cross",
        )

        rows = OperatorQueryService._aggregate_local_positions(snapshot)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(Decimal(str(row["position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["net_position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["gross_position_qty"])), Decimal("0.03"))
        self.assertEqual(Decimal(str(row["long_position_qty"])), Decimal("0.02"))
        self.assertEqual(Decimal(str(row["short_position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["net_position_notional"])), Decimal("700"))
        self.assertEqual(Decimal(str(row["gross_position_notional"])), Decimal("2100"))
        self.assertTrue(row["dual_legged"])
        self.assertEqual(len(row["legs"]), 2)

    def test_aggregate_exchange_positions_exposes_dual_leg_state(self) -> None:
        exchange = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=datetime.now(timezone.utc),
            balances=[],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.02"),
                    average_entry_price=Decimal("70000"),
                    notional_usd=Decimal("1400"),
                    side="long",
                    margin_mode="cross",
                    unrealized_pnl=Decimal("15"),
                ),
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.01"),
                    average_entry_price=Decimal("70500"),
                    notional_usd=Decimal("700"),
                    side="short",
                    margin_mode="cross",
                    unrealized_pnl=Decimal("-3"),
                ),
            ],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cross",
            position_mode="long_short_mode",
            account_configuration=ExchangeAccountConfiguration(position_mode="long_short_mode"),
        )

        rows = OperatorQueryService._aggregate_exchange_positions(exchange)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(Decimal(str(row["position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["net_position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["gross_position_qty"])), Decimal("0.03"))
        self.assertEqual(Decimal(str(row["long_position_qty"])), Decimal("0.02"))
        self.assertEqual(Decimal(str(row["short_position_qty"])), Decimal("0.01"))
        self.assertEqual(Decimal(str(row["net_position_notional"])), Decimal("700"))
        self.assertEqual(Decimal(str(row["gross_position_notional"])), Decimal("2100"))
        self.assertTrue(row["dual_legged"])
        self.assertEqual(len(row["legs"]), 2)

    def test_position_mode_audit_summary_collects_modes_and_pos_sides(self) -> None:
        summary = OperatorQueryService._position_mode_audit_summary(
            position_mode_contract={
                "configured_derivatives_position_mode": "hedge",
                "required_exchange_position_mode": "long_short_mode",
                "exchange_position_mode": "long_short_mode",
                "exchange_position_mode_matches_configured": True,
                "position_mode_match_required": True,
            },
            order_intents=[
                {
                    "position_mode": "long_short_mode",
                    "pos_side": "long",
                }
            ],
            order_updates=[
                {
                    "position_mode": "long_short_mode",
                    "pos_side": "short",
                }
            ],
            fills=[],
            reconciliations=[],
        )

        self.assertTrue(summary["hedge_mode_active"])
        self.assertEqual(summary["observed_position_modes"], ["long_short_mode"])
        self.assertEqual(summary["observed_pos_sides"], ["long", "short"])
        self.assertFalse(summary["mode_change_detected"])

    def test_leg_order_audit_summary_collects_leg_actions(self) -> None:
        summary = OperatorQueryService._leg_order_audit_summary(
            order_intents=[
                {
                    "symbol": "BTC-USDT-SWAP",
                    "position_mode": "long_short_mode",
                    "pos_side": "long",
                    "leg_action": "open",
                    "quantity": "0.02",
                    "client_order_id": "clord_long_open",
                    "intent_id": "intent_long_open",
                    "leg_intent_id": "leg_long_open",
                },
                {
                    "symbol": "BTC-USDT-SWAP",
                    "position_mode": "long_short_mode",
                    "pos_side": "short",
                    "leg_action": "close",
                    "quantity": "0.01",
                    "client_order_id": "clord_short_close",
                    "intent_id": "intent_short_close",
                    "leg_intent_id": "leg_short_close",
                },
            ],
            order_updates=[
                {
                    "client_order_id": "clord_long_open",
                    "intent_id": "intent_long_open",
                    "leg_intent_id": "leg_long_open",
                    "status": "FILLED",
                }
            ],
            fills=[
                {
                    "client_order_id": "clord_long_open",
                    "intent_id": "intent_long_open",
                    "leg_intent_id": "leg_long_open",
                }
            ],
        )

        self.assertEqual(summary["total_count"], 2)
        self.assertEqual(summary["open_count"], 1)
        self.assertEqual(summary["close_count"], 1)
        self.assertEqual(summary["items"][0]["fill_count"], 1)
        self.assertIn("BTC-USDT-SWAP", summary["symbols"])

    def test_leg_reconciliation_audit_summary_collects_leg_mismatches(self) -> None:
        summary = OperatorQueryService._leg_reconciliation_audit_summary(
            [
                {
                    "reconciliation_id": "recon_leg_1",
                    "position_diff": {
                        "exchange_leg_mismatches": {
                            "BTC-USDT-SWAP:short": {
                                "symbol": "BTC-USDT-SWAP",
                                "leg_side": "short",
                                "stored_qty": "0",
                                "exchange_qty": "-0.01",
                            }
                        },
                        "exchange_instrument_mismatches": {},
                    },
                    "unknown_state_details": [
                        {
                            "kind": "exchange_position_without_local_execution_chain",
                            "position_key": "BTC-USDT-SWAP:short",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(summary["total_count"], 1)
        self.assertEqual(summary["missing_execution_chain_count"], 1)
        self.assertEqual(summary["items"][0]["reconciliation_id"], "recon_leg_1")
        self.assertEqual(summary["items"][0]["kind"], "missing_execution_chain")


if __name__ == "__main__":
    unittest.main()
