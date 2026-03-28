from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from aats.bootstrap.settings import AATSSettings
from aats.services.strategy_engines.smart_arbitrage.capabilities import resolve_execution_capability
from aats.services.strategy_engines.smart_arbitrage.cost_model import build_cost_breakdown
from aats.services.strategy_engines.smart_arbitrage.leg_planner import build_legs
from aats.services.strategy_engines.smart_arbitrage.pair_registry import load_pair_definitions
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitrageOpportunity, ArbitragePairDefinition
from aats.services.strategy_engines.smart_arbitrage.state_machine import resolve_pair_state


class TestSmartArbitrageV2Components(unittest.TestCase):
    def test_pair_registry_loads_configured_pairs_alongside_primary_fallback(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_quarterly",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-260626",
                    },
                ),
            }
        )

        pairs = load_pair_definitions(settings=settings, primary_symbol="BTC-USDT-SWAP")

        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0].hedge_symbol, "BTC-USDT-SWAP")
        self.assertEqual(pairs[1].pair_id, "btc_quarterly")

    def test_pair_registry_dedupes_configured_primary_pair_against_derived_fallback(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_usdt_swap",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                ),
            }
        )

        pairs = load_pair_definitions(settings=settings, primary_symbol="BTC-USDT-SWAP")

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].pair_id, "btc_usdt_swap")
        self.assertEqual(pairs[0].metadata.get("source"), "pair_registry")

    def test_capability_resolver_distinguishes_inventory_and_margin_modes(self) -> None:
        inventory_settings = AATSSettings.model_validate(
            {
                "smart_arbitrage_negative_basis_mode": "inventory_backed",
                "smart_arbitrage_inventory_reservation_enabled": True,
            }
        )
        margin_settings = AATSSettings.model_validate(
            {
                "smart_arbitrage_negative_basis_mode": "margin_backed",
                "smart_arbitrage_margin_short_enabled": True,
                "smart_arbitrage_margin_short_execution_ready": True,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
            }
        )
        pair = ArbitragePairDefinition(pair_id="btc_pair", spot_symbol="BTC-USDT", hedge_symbol="BTC-USDT-SWAP")

        inventory_capability = resolve_execution_capability(
            settings=inventory_settings,
            pair=pair,
            account_spot_qty=Decimal("0.5"),
        )
        margin_capability = resolve_execution_capability(
            settings=margin_settings,
            pair=pair,
            account_spot_qty=Decimal("0"),
        )

        self.assertTrue(inventory_capability.inventory_backed_spot_sell_supported)
        self.assertFalse(inventory_capability.spot_margin_short_supported)
        self.assertTrue(margin_capability.spot_margin_short_supported)
        self.assertTrue(margin_capability.margin_short_execution_ready)
        self.assertEqual(margin_capability.spot_margin_mode, "cross")

    def test_state_machine_marks_reverse_carry_recovery(self) -> None:
        state = resolve_pair_state(
            pair_id="btc_pair",
            account_spot_qty=Decimal("-0.30"),
            account_hedge_qty=Decimal("0.20"),
            sleeve_spot_qty=Decimal("-0.30"),
            sleeve_hedge_qty=Decimal("0.20"),
            basis_bps=Decimal("-50"),
            exit_threshold_bps=Decimal("6"),
        )

        self.assertEqual(state.current_direction, "reverse_carry")
        self.assertEqual(state.state_phase, "recovery")
        self.assertTrue(state.recovery_required)

    def test_cost_model_uses_granular_cost_fields_for_margin_reverse_carry(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "smart_arbitrage_cost_model_enabled": True,
                "trade_cost_spot_taker_fee_bps": 0.5,
                "trade_cost_margin_taker_fee_bps": 0.5,
                "trade_cost_derivatives_taker_fee_bps": 0.5,
                "trade_cost_spot_spread_bps": 0.0,
                "trade_cost_margin_spread_bps": 0.0,
                "trade_cost_derivatives_spread_bps": 0.0,
                "trade_cost_spot_slippage_bps": 1.5,
                "trade_cost_margin_slippage_bps": 1.5,
                "trade_cost_derivatives_slippage_bps": 1.5,
                "smart_arbitrage_funding_cost_enabled": True,
                "smart_arbitrage_estimated_funding_bps": 4.0,
                "smart_arbitrage_borrow_cost_enabled": True,
                "smart_arbitrage_estimated_borrow_bps": 5.0,
            }
        )

        cost = build_cost_breakdown(
            settings=settings,
            basis_bps=Decimal("-40"),
            execution_mode="margin_reverse_carry",
        )

        self.assertEqual(cost.ideal_open_fee_bps, Decimal("1"))
        self.assertEqual(cost.ideal_close_fee_bps, Decimal("1"))
        self.assertEqual(cost.ideal_total_fee_bps, Decimal("2"))
        self.assertEqual(cost.ideal_total_cost_bps, Decimal("11"))
        self.assertEqual(cost.executable_slippage_bps, Decimal("3"))
        self.assertEqual(cost.funding_cost_bps, Decimal("4"))
        self.assertEqual(cost.borrow_cost_bps, Decimal("5"))
        self.assertEqual(cost.expected_funding_events, 1)
        self.assertEqual(cost.borrow_hour_windows, 8)
        self.assertIn("fee_account_schedule", cost.cost_source_flags)
        self.assertIn("slippage_trade_cost_defaults", cost.cost_source_flags)
        self.assertIn("funding_configured_per_event", cost.cost_source_flags)
        self.assertIn("borrow_configured_total", cost.cost_source_flags)
        self.assertEqual(cost.estimated_total_cost_bps, Decimal("14"))
        self.assertEqual(cost.executable_total_drag_bps, Decimal("14"))
        self.assertEqual(cost.ideal_edge_bps, Decimal("29"))
        self.assertEqual(cost.net_edge_bps, Decimal("26"))
        self.assertEqual(cost.executable_edge_bps, Decimal("26"))
        self.assertEqual(cost.breakeven_basis_bps, Decimal("14"))

    def test_cost_model_projects_funding_events_from_next_boundary_crossings(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "smart_arbitrage_cost_model_enabled": True,
                "trade_cost_spot_taker_fee_bps": 0.0,
                "trade_cost_margin_taker_fee_bps": 0.0,
                "trade_cost_derivatives_taker_fee_bps": 0.0,
                "trade_cost_spot_spread_bps": 0.0,
                "trade_cost_margin_spread_bps": 0.0,
                "trade_cost_derivatives_spread_bps": 0.0,
                "trade_cost_spot_slippage_bps": 0.0,
                "trade_cost_margin_slippage_bps": 0.0,
                "trade_cost_derivatives_slippage_bps": 0.0,
                "smart_arbitrage_funding_cost_enabled": True,
                "smart_arbitrage_funding_source_mode": "configured",
                "smart_arbitrage_estimated_funding_bps": 1.5,
                "smart_arbitrage_expected_hold_hours": 16.0,
                "smart_arbitrage_funding_interval_hours": 8.0,
                "smart_arbitrage_borrow_cost_enabled": True,
                "smart_arbitrage_borrow_source_mode": "apr_window_model",
                "smart_arbitrage_estimated_borrow_apr": 21.9,
                "smart_arbitrage_borrow_interest_free_ratio": 0.5,
            }
        )

        frozen_now = datetime(2026, 3, 27, 6, 30, tzinfo=timezone.utc)
        with patch("aats.services.strategy_engines.smart_arbitrage.cost_model.utc_now", return_value=frozen_now):
            cost = build_cost_breakdown(
                settings=settings,
                basis_bps=Decimal("-100"),
                execution_mode="margin_reverse_carry",
            )

        self.assertEqual(cost.expected_funding_events, 2)
        self.assertEqual(cost.funding_cost_bps, Decimal("3.0"))
        self.assertEqual(cost.borrow_hour_windows, 16)
        self.assertEqual(cost.borrow_cost_bps, Decimal("2.0000"))
        self.assertIn("funding_configured_per_event", cost.cost_source_flags)
        self.assertIn("borrow_apr_window_model", cost.cost_source_flags)
        self.assertEqual(cost.executable_total_drag_bps, Decimal("5.0000"))
        self.assertEqual(cost.executable_edge_bps, Decimal("95.0000"))

    def test_cost_model_skips_funding_when_hold_window_does_not_reach_next_boundary(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "smart_arbitrage_cost_model_enabled": True,
                "trade_cost_spot_taker_fee_bps": 0.0,
                "trade_cost_margin_taker_fee_bps": 0.0,
                "trade_cost_derivatives_taker_fee_bps": 0.0,
                "trade_cost_spot_spread_bps": 0.0,
                "trade_cost_margin_spread_bps": 0.0,
                "trade_cost_derivatives_spread_bps": 0.0,
                "trade_cost_spot_slippage_bps": 0.0,
                "trade_cost_margin_slippage_bps": 0.0,
                "trade_cost_derivatives_slippage_bps": 0.0,
                "smart_arbitrage_funding_cost_enabled": True,
                "smart_arbitrage_funding_source_mode": "configured",
                "smart_arbitrage_estimated_funding_bps": 2.0,
                "smart_arbitrage_estimated_cost_bps": 0.0,
                "smart_arbitrage_expected_hold_hours": 6.0,
                "smart_arbitrage_funding_interval_hours": 8.0,
            }
        )

        frozen_now = datetime(2026, 3, 27, 9, 0, tzinfo=timezone.utc)
        with patch("aats.services.strategy_engines.smart_arbitrage.cost_model.utc_now", return_value=frozen_now):
            cost = build_cost_breakdown(
                settings=settings,
                basis_bps=Decimal("40"),
                execution_mode="spot_carry",
            )

        self.assertEqual(cost.expected_funding_events, 0)
        self.assertEqual(cost.funding_cost_bps, Decimal("0"))
        self.assertIn("funding_outside_projected_hold_window", cost.cost_source_flags)
        self.assertEqual(cost.executable_total_drag_bps, Decimal("0"))
        self.assertEqual(cost.executable_edge_bps, Decimal("40"))

    def test_cost_model_reads_account_proxy_funding_without_fee_schedule_mode(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "smart_arbitrage_cost_model_enabled": True,
                "smart_arbitrage_fee_source_mode": "configured",
                "smart_arbitrage_funding_cost_enabled": True,
                "smart_arbitrage_funding_source_mode": "account_proxy",
                "smart_arbitrage_expected_hold_hours": 8.0,
                "smart_arbitrage_funding_interval_hours": 8.0,
                "smart_arbitrage_estimated_cost_bps": 0.0,
                "trade_cost_spot_taker_fee_bps": 0.0,
                "trade_cost_margin_taker_fee_bps": 0.0,
                "trade_cost_derivatives_taker_fee_bps": 0.0,
                "trade_cost_spot_spread_bps": 0.0,
                "trade_cost_margin_spread_bps": 0.0,
                "trade_cost_derivatives_spread_bps": 0.0,
                "trade_cost_spot_slippage_bps": 0.0,
                "trade_cost_margin_slippage_bps": 0.0,
                "trade_cost_derivatives_slippage_bps": 0.0,
            }
        )

        class _AccountProxy:
            @staticmethod
            def funding_fee_bps_proxy(*, symbol: str | None = None) -> Decimal:
                return Decimal("1.25") if symbol == "BTC-USDT-SWAP" else Decimal("0")

        frozen_now = datetime(2026, 3, 27, 8, 0, tzinfo=timezone.utc)
        with patch("aats.services.strategy_engines.smart_arbitrage.cost_model.utc_now", return_value=frozen_now):
            cost = build_cost_breakdown(
                settings=settings,
                basis_bps=Decimal("40"),
                execution_mode="spot_carry",
                hedge_symbol="BTC-USDT-SWAP",
                account_service=_AccountProxy(),
            )

        self.assertEqual(cost.expected_funding_events, 1)
        self.assertEqual(cost.funding_cost_bps, Decimal("1.25"))
        self.assertIn("funding_account_proxy_per_event", cost.cost_source_flags)

    def test_cost_model_treats_estimated_borrow_apr_as_percentage(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "smart_arbitrage_cost_model_enabled": True,
                "smart_arbitrage_borrow_cost_enabled": True,
                "smart_arbitrage_borrow_source_mode": "apr_window_model",
                "smart_arbitrage_expected_hold_hours": 8.0,
                "smart_arbitrage_estimated_borrow_apr": 18.0,
                "smart_arbitrage_borrow_interest_free_ratio": 0.0,
            }
        )

        cost = build_cost_breakdown(
            settings=settings,
            basis_bps=Decimal("-40"),
            execution_mode="margin_reverse_carry",
        )

        self.assertEqual(cost.borrow_hour_windows, 8)
        self.assertEqual(cost.borrow_cost_bps, Decimal("1.643835616438356164383561644"))
        self.assertEqual(cost.breakeven_basis_bps, Decimal("35.64383561643835616438356164"))

    def test_leg_planner_builds_margin_reverse_carry_spot_leg_in_margin_mode(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "margin_mode": "cross",
                "default_target_leverage": 10.0,
                "max_target_leverage": 20.0,
                "smart_arbitrage_hedge_target_leverage": 3.0,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
            }
        )
        pair = ArbitragePairDefinition(pair_id="btc_pair", spot_symbol="BTC-USDT", hedge_symbol="BTC-USDT-SWAP")
        opportunity = ArbitrageOpportunity(
            pair_id="btc_pair",
            spot_symbol="BTC-USDT",
            hedge_symbol="BTC-USDT-SWAP",
            opportunity_kind="negative_basis",
            direction="negative_basis",
            execution_mode="margin_reverse_carry",
            state_phase="opening",
            desired_pair_qty=Decimal("1"),
            target_spot_qty=Decimal("-1"),
            target_hedge_qty=Decimal("1"),
            route_action="override_target",
        )

        legs = build_legs(
            settings=settings,
            pair=pair,
            opportunity=opportunity,
            account_spot_qty=Decimal("0"),
            account_hedge_qty=Decimal("0"),
            sleeve_spot_qty=Decimal("0"),
            sleeve_hedge_qty=Decimal("0"),
            spot_price=Decimal("100"),
            hedge_price=Decimal("99"),
        )

        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0].margin_mode, "cross")
        self.assertEqual(legs[0].side, "sell")
        self.assertEqual(legs[0].execution_mode, "margin_reverse_carry")
        self.assertEqual(legs[1].target_leverage, 3.0)

    def test_leg_planner_clamps_smart_arbitrage_hedge_leverage_to_runtime_max(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "margin_mode": "cross",
                "default_target_leverage": 10.0,
                "max_target_leverage": 2.5,
                "smart_arbitrage_hedge_target_leverage": 3.0,
                "smart_arbitrage_margin_short_spot_margin_mode": "cross",
            }
        )
        pair = ArbitragePairDefinition(pair_id="btc_pair", spot_symbol="BTC-USDT", hedge_symbol="BTC-USDT-SWAP")
        opportunity = ArbitrageOpportunity(
            pair_id="btc_pair",
            spot_symbol="BTC-USDT",
            hedge_symbol="BTC-USDT-SWAP",
            opportunity_kind="positive_basis",
            direction="positive_basis",
            execution_mode="spot_carry",
            state_phase="opening",
            desired_pair_qty=Decimal("1"),
            target_spot_qty=Decimal("1"),
            target_hedge_qty=Decimal("-1"),
            route_action="override_target",
        )

        legs = build_legs(
            settings=settings,
            pair=pair,
            opportunity=opportunity,
            account_spot_qty=Decimal("0"),
            account_hedge_qty=Decimal("0"),
            sleeve_spot_qty=Decimal("0"),
            sleeve_hedge_qty=Decimal("0"),
            spot_price=Decimal("100"),
            hedge_price=Decimal("99"),
        )

        self.assertEqual(legs[1].target_leverage, 2.5)

    def test_pair_registry_defaults_execution_modes_to_all_supported_live_modes(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_pair",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                ),
            }
        )

        pairs = load_pair_definitions(settings=settings, primary_symbol="BTC-USDT-SWAP")

        self.assertEqual(
            pairs[0].execution_modes,
            ("spot_carry", "inventory_reverse_carry", "margin_reverse_carry"),
        )

    def test_pair_registry_invalid_execution_modes_fail_closed(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_pair",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                        "execution_modes": ("spotcarry_typo",),
                    },
                ),
            }
        )

        pairs = load_pair_definitions(settings=settings, primary_symbol="BTC-USDT-SWAP")

        self.assertEqual(pairs[0].execution_modes, ())
        self.assertIn("smart_arbitrage_pair_execution_modes_invalid", pairs[0].metadata["configuration_error_codes"])
        self.assertEqual(pairs[0].metadata["invalid_execution_modes"], ["spotcarry_typo"])

    def test_pair_registry_renames_conflicting_pair_ids_and_marks_warning(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "duplicate_pair",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                    {
                        "pair_id": "duplicate_pair",
                        "spot_symbol": "ETH-USDT",
                        "hedge_symbol": "ETH-USDT-SWAP",
                    },
                ),
            }
        )

        pairs = load_pair_definitions(settings=settings, primary_symbol="BTC-USDT-SWAP")

        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0].pair_id, "duplicate_pair")
        self.assertEqual(pairs[1].pair_id, "duplicate_pair__scope_conflict_2")
        self.assertIn("smart_arbitrage_pair_id_conflict_renamed", pairs[1].metadata["configuration_warning_codes"])

    def test_pair_registry_marks_duplicate_scope_pairs_on_retained_pair(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "smart_arbitrage_pair_definitions": (
                    {
                        "pair_id": "btc_pair_primary",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                    {
                        "pair_id": "btc_pair_duplicate",
                        "spot_symbol": "BTC-USDT",
                        "hedge_symbol": "BTC-USDT-SWAP",
                    },
                ),
            }
        )

        pairs = load_pair_definitions(settings=settings, primary_symbol="BTC-USDT-SWAP")

        self.assertEqual(len(pairs), 1)
        self.assertIn("smart_arbitrage_duplicate_pair_scope_ignored", pairs[0].metadata["configuration_warning_codes"])
        self.assertEqual(
            pairs[0].metadata["ignored_duplicate_scope_pairs"][0]["pair_id"],
            "btc_pair_duplicate",
        )


if __name__ == "__main__":
    unittest.main()
