from __future__ import annotations

import unittest
from decimal import Decimal

from aats.services.trade_drag import TradeDragCalculator, TradeDragProfile


class TestTradeDragCalculator(unittest.TestCase):
    def setUp(self) -> None:
        self.calculator = TradeDragCalculator()

    def test_legacy_fallback_profile_is_supported(self) -> None:
        estimate = self.calculator.estimate(
            profile=TradeDragProfile(
                model_name="legacy_disabled",
                cost_model_enabled=False,
                edge_reference_bps=Decimal("40"),
                expected_hold_hours=Decimal("8"),
                legacy_total_cost_bps=Decimal("12"),
            )
        )

        self.assertEqual(estimate.ideal_total_cost_bps, Decimal("12"))
        self.assertEqual(estimate.executable_total_drag_bps, Decimal("12"))
        self.assertEqual(estimate.ideal_edge_bps, Decimal("28"))
        self.assertEqual(estimate.executable_edge_bps, Decimal("28"))
        self.assertEqual(estimate.breakeven_reference_bps, Decimal("12"))
        self.assertEqual(estimate.cost_source_flags, ["cost_model_disabled", "legacy_estimated_cost_fallback"])

    def test_round_trip_pair_profile_aggregates_all_drag_components(self) -> None:
        estimate = self.calculator.estimate(
            profile=TradeDragProfile(
                model_name="pair_round_trip",
                cost_model_enabled=True,
                edge_reference_bps=Decimal("40"),
                expected_hold_hours=Decimal("8"),
                expected_funding_events=1,
                borrow_hour_windows=8,
                ideal_open_fee_bps=Decimal("1"),
                ideal_close_fee_bps=Decimal("1"),
                executable_spread_bps=Decimal("2"),
                executable_slippage_bps=Decimal("3"),
                execution_mismatch_bps=Decimal("1"),
                funding_cost_bps=Decimal("4"),
                borrow_cost_bps=Decimal("5"),
                transfer_cost_bps=Decimal("6"),
                time_decay_cost_bps=Decimal("2"),
                cost_source_flags=[
                    "fee_account_schedule",
                    "funding_account_proxy_total",
                    "borrow_apr_window_model",
                    "time_decay_configured",
                    "transfer_cost_configured",
                ],
            )
        )

        self.assertEqual(estimate.ideal_total_fee_bps, Decimal("2"))
        self.assertEqual(estimate.ideal_total_cost_bps, Decimal("17"))
        self.assertEqual(estimate.executable_total_drag_bps, Decimal("25"))
        self.assertEqual(estimate.ideal_edge_bps, Decimal("23"))
        self.assertEqual(estimate.executable_edge_bps, Decimal("15"))
        self.assertEqual(estimate.breakeven_reference_bps, Decimal("25"))
        self.assertEqual(estimate.expected_funding_events, 1)
        self.assertEqual(estimate.borrow_hour_windows, 8)
        self.assertEqual(estimate.cost_confidence, 0.95)

    def test_single_leg_directional_profile_keeps_entry_side_cost_semantics(self) -> None:
        estimate = self.calculator.estimate(
            profile=TradeDragProfile(
                model_name="directional_single_leg",
                cost_model_enabled=True,
                edge_reference_bps=Decimal("0"),
                ideal_open_fee_bps=Decimal("5"),
                ideal_close_fee_bps=Decimal("0"),
                executable_slippage_bps=Decimal("12"),
                funding_cost_bps=Decimal("8"),
                cost_source_flags=[
                    "directional_single_leg_cost_model",
                    "fee_execution_profile_estimate",
                    "slippage_directional_expectation",
                    "funding_account_proxy_total",
                ],
            )
        )

        self.assertEqual(estimate.ideal_total_fee_bps, Decimal("5"))
        self.assertEqual(estimate.ideal_total_cost_bps, Decimal("13"))
        self.assertEqual(estimate.executable_total_drag_bps, Decimal("25"))
        self.assertEqual(estimate.ideal_edge_bps, Decimal("-13"))
        self.assertEqual(estimate.executable_edge_bps, Decimal("-25"))
        self.assertEqual(estimate.ideal_close_fee_bps, Decimal("0"))
        self.assertGreaterEqual(estimate.cost_confidence, 0.55)

    def test_named_cost_components_allow_different_trading_modes_to_extend_common_model(self) -> None:
        estimate = self.calculator.estimate(
            profile=TradeDragProfile(
                model_name="delivery_contract",
                cost_model_enabled=True,
                edge_reference_bps=Decimal("35"),
                ideal_open_fee_bps=Decimal("2"),
                ideal_close_fee_bps=Decimal("2"),
                explicit_cost_components_bps={
                    "settlement_fee_bps": Decimal("1"),
                    "withdrawal_fee_bps": Decimal("3"),
                },
                execution_drag_components_bps={
                    "fiat_cashout_fee_bps": Decimal("4"),
                },
                cost_source_flags=["fee_configured_per_leg"],
            )
        )

        self.assertEqual(estimate.explicit_cost_components_bps["settlement_fee_bps"], Decimal("1"))
        self.assertEqual(estimate.explicit_cost_components_bps["withdrawal_fee_bps"], Decimal("3"))
        self.assertEqual(estimate.execution_drag_components_bps["fiat_cashout_fee_bps"], Decimal("4"))
        self.assertEqual(estimate.ideal_total_cost_bps, Decimal("8"))
        self.assertEqual(estimate.executable_total_drag_bps, Decimal("12"))

    def test_negative_fee_rebates_reduce_drag_without_triggering_legacy_fallback(self) -> None:
        estimate = self.calculator.estimate(
            profile=TradeDragProfile(
                model_name="maker_rebate_profile",
                cost_model_enabled=True,
                edge_reference_bps=Decimal("10"),
                ideal_open_fee_bps=Decimal("-6"),
                executable_spread_bps=Decimal("1"),
                legacy_total_cost_bps=Decimal("12"),
                cost_source_flags=["fee_account_schedule"],
            )
        )

        self.assertEqual(estimate.ideal_total_fee_bps, Decimal("-6"))
        self.assertEqual(estimate.ideal_total_cost_bps, Decimal("-6"))
        self.assertEqual(estimate.executable_total_drag_bps, Decimal("-5"))
        self.assertNotIn("legacy_estimated_cost_fallback", estimate.cost_source_flags)


if __name__ == "__main__":
    unittest.main()
