from __future__ import annotations

from decimal import Decimal
import unittest

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
                "smart_arbitrage_estimated_fee_bps": 2.0,
                "smart_arbitrage_estimated_slippage_bps": 3.0,
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

        self.assertEqual(cost.estimated_total_cost_bps, Decimal("14"))
        self.assertEqual(cost.net_edge_bps, Decimal("26"))

    def test_leg_planner_builds_margin_reverse_carry_spot_leg_in_margin_mode(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "margin_mode": "cross",
                "default_target_leverage": 3.0,
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
