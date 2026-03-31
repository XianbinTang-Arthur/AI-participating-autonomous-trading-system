from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from aats.schemas.exchange import ExchangeAccountConfiguration, ExchangeAccountSnapshot, ExchangePosition
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.services.operator.query_service import OperatorQueryService


class TestOperatorPositionStates(unittest.TestCase):
    def test_smart_arbitrage_runtime_pair_configuration_prefers_snapshot_candidate_metrics(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(settings=SimpleNamespace(default_symbol="BTC-USDT-SWAP"))

        latest_snapshot = {
            "candidates": [
                {
                    "family": "smart_arbitrage",
                    "metrics": {
                        "pair_definitions": [
                            {
                                "pair_id": "btc_usdt_swap",
                                "spot_symbol": "BTC-USDT",
                                "hedge_symbol": "BTC-USDT-SWAP",
                                "metadata": {
                                    "configuration_warning_codes": ["pair_warn"],
                                    "configuration_error_codes": ["pair_err"],
                                },
                            }
                        ],
                        "pair_registry_warning_codes": ["pair_warn"],
                        "pair_registry_error_codes": ["pair_err"],
                        "pair_registry_source": "coordinator_resolved",
                    },
                }
            ]
        }

        with patch(
            "aats.services.operator.query_service.load_pair_definitions",
            side_effect=AssertionError("query_service should reuse coordinator-resolved pair definitions"),
        ):
            pair_definitions, warning_codes, error_codes, source = query._smart_arbitrage_runtime_pair_configuration(
                latest_snapshot=latest_snapshot
            )

        self.assertEqual(len(pair_definitions), 1)
        self.assertEqual(pair_definitions[0]["pair_id"], "btc_usdt_swap")
        self.assertEqual(warning_codes, ["pair_warn"])
        self.assertEqual(error_codes, ["pair_err"])
        self.assertEqual(source, "coordinator_resolved")

    def test_execution_action_summary_prefers_directional_position_intent(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)

        action = query._action_from_execution_fields(
            execution_action="reduce",
            position_intent="reduce_long",
        )

        self.assertEqual(action, "reduce_long")

    def test_abstract_action_from_position_intent_keeps_final_action_semantics(self) -> None:
        self.assertEqual(
            OperatorQueryService._abstract_action_from_position_intent("open_short"),
            "enter",
        )
        self.assertEqual(
            OperatorQueryService._abstract_action_from_position_intent("scale_in_long"),
            "scale_in",
        )

    def test_decision_outcome_payload_prefers_native_outcome_and_backfills_family_summary(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(settings=SimpleNamespace(ai_operating_mode="baseline_only"))
        query.strategy_profile_snapshot = lambda: {"activation": {}}  # type: ignore[method-assign]

        payload = query._decision_outcome_payload(
            finalized_decision_outcome={
                "decision_id": "dec-1",
                "symbol": "BTC-USDT-SWAP",
                "decision_source": "baseline",
                "decision_authority": "reference_only",
                "final_action": "enter",
                "final_direction": "short",
            },
            decision_context=None,
            baseline_assessment=None,
            ai_assessment=None,
            position_target={
                "position_intent": "reduce_long",
                "target_exposure_side": "long",
                "family_execution_summary": {
                    "summary_mode": "single_leg",
                    "family": "protective",
                    "route_action": "override_target",
                    "family_action": "protect",
                    "leg_count": 1,
                    "position_intents": ["open_short"],
                    "directions": ["short"],
                    "leg_actions": ["open"],
                    "execution_modes": ["protective_overlay"],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_gross_edge_bps": 18.0,
                                "expected_signal_edge_bps": 18.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": 12.0,
                            }
                        ],
                    },
                },
            },
            policy_decision=None,
            risk_decision=None,
        )

        assert payload is not None
        self.assertTrue(payload["finalized"])
        self.assertEqual(payload["final_action"], "enter")
        self.assertEqual(payload["final_direction"], "short")
        self.assertEqual(payload["family_execution_summary"]["position_intents"], ["open_short"])
        self.assertEqual(payload["book_expectancy_summary"]["source"], "independent_book")
        self.assertEqual(
            payload["family_execution_summary"]["book_expectancy_summary"]["books"][0]["expected_net_edge_bps"],
            12.0,
        )
        self.assertEqual(payload["book_expectancy_summary"]["books"][0]["expected_net_edge_bps"], 12.0)

    def test_ai_decision_audit_prefers_native_outcome_over_target_fields(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)
        query.runtime = SimpleNamespace(settings=SimpleNamespace(ai_operating_mode="baseline_only"))

        audit = query._ai_decision_audit(
            audit=SimpleNamespace(
                order_intent_refs=[],
                order_state_refs=[],
                fill_event_refs=[],
                reconciliation_refs=[],
                ai_shadow_decision_refs=[],
                ai_shadow_evaluation_refs=[],
            ),
            decision_context=None,
            ai_decision_brief=None,
            baseline_assessment={"direction_bias": "long"},
            ai_assessment=None,
            position_target={
                "position_intent": "reduce_long",
                "target_exposure_side": "long",
                "family_execution_summary": {
                    "summary_mode": "multi_leg",
                    "family": "independent",
                    "route_action": "override_target",
                    "family_action": "open_independent_book",
                    "leg_count": 2,
                    "position_intents": ["open_long", "open_short"],
                    "directions": ["long", "short"],
                    "leg_actions": ["open"],
                    "execution_modes": ["independent_long_book", "independent_short_book"],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_gross_edge_bps": 18.0,
                                "expected_signal_edge_bps": 18.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": 12.0,
                            },
                            {
                                "leg": "short",
                                "expected_gross_edge_bps": 4.0,
                                "expected_signal_edge_bps": 4.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": -2.0,
                            },
                        ],
                    },
                },
            },
            finalized_decision_outcome={
                "decision_source": "baseline",
                "decision_authority": "reference_only",
                "profile_control_source": "system",
                "final_action": "enter",
                "final_direction": "flat",
                "family_execution_summary": {
                    "summary_mode": "multi_leg",
                    "family": "independent",
                    "route_action": "override_target",
                    "family_action": "open_independent_book",
                    "leg_count": 2,
                    "position_intents": ["open_long", "open_short"],
                    "directions": ["long", "short"],
                    "leg_actions": ["open"],
                    "execution_modes": ["independent_long_book", "independent_short_book"],
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_gross_edge_bps": 18.0,
                                "expected_signal_edge_bps": 18.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": 12.0,
                            },
                            {
                                "leg": "short",
                                "expected_gross_edge_bps": 4.0,
                                "expected_signal_edge_bps": 4.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": -2.0,
                            },
                        ],
                    },
                },
            },
            strategy_execution_health=None,
        )

        assert audit is not None
        self.assertTrue(audit["finalized"])
        self.assertEqual(audit["final_action"], "enter")
        self.assertEqual(audit["final_direction"], "flat")
        self.assertEqual(audit["family_execution_summary"]["position_intents"], ["open_long", "open_short"])
        self.assertEqual(audit["book_expectancy_summary"]["source"], "independent_book")
        self.assertEqual(
            audit["family_execution_summary"]["book_expectancy_summary"]["books"][1]["expected_net_edge_bps"],
            -2.0,
        )
        self.assertEqual(audit["book_expectancy_summary"]["books"][1]["expected_net_edge_bps"], -2.0)

    def test_position_target_payload_backfills_top_level_book_expectancy_summary(self) -> None:
        query = OperatorQueryService.__new__(OperatorQueryService)

        payload = query._position_target_payload(
            {
                "position_intent": "hold",
                "family_execution_summary": {
                    "summary_mode": "multi_leg",
                    "family": "independent",
                    "book_expectancy_summary": {
                        "source": "independent_book",
                        "books": [
                            {
                                "leg": "long",
                                "expected_gross_edge_bps": 18.0,
                                "expected_signal_edge_bps": 18.0,
                                "expected_slippage_bps": 1.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": 12.0,
                            }
                        ],
                    },
                },
            }
        )

        assert payload is not None
        self.assertEqual(payload["book_expectancy_summary"]["source"], "independent_book")
        self.assertEqual(payload["book_expectancy_summary"]["books"][0]["expected_net_edge_bps"], 12.0)

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
