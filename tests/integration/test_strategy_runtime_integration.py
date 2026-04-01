from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.managed_profiles import load_managed_profile_values
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope, publish_model
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.decision import DecisionContext, DecisionOutcome, HedgeOverlayDecision, PositionTarget
from aats.schemas.features import FeatureSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioSnapshot
from aats.schemas.strategy_runtime import StrategyExecutionBundle, StrategyLegIntent


class TestStrategyRuntimeIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_spot_grid_runtime_endpoint_exposes_latest_snapshot_and_system_summary(self) -> None:
        settings = self._settings(
            trading_product_type="spot",
            margin_mode="cash",
            default_symbol="BTC-USDT",
            allowed_symbols=("BTC-USDT",),
            strategy_family_active="spot_grid",
            spot_grid_enabled=True,
            spot_grid_breakout_guard_enabled=False,
            spot_grid_anchor_lookback_snapshots=4,
            spot_grid_band_bps=500.0,
            spot_grid_inventory_floor_fraction=0.5,
            spot_grid_inventory_ceiling_fraction=1.0,
            spot_grid_rebalance_min_fraction_of_max_qty=0.05,
            max_abs_position_qty=1.0,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertEqual(target.strategy_family, "spot_grid")
        self.assertEqual(target.strategy_route_action, "override_target")
        self.assertGreater(runtime.event_store.count(topic=topics.STRATEGY_COORDINATOR_SNAPSHOTS), 0)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")
            system_runtime = client.get("/system/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        self.assertEqual(system_runtime.status_code, 200)
        strategy_payload = strategy_runtime.json()
        system_payload = system_runtime.json()
        self.assertEqual(strategy_payload["summary"]["configured_active_family"], "spot_grid")
        self.assertEqual(strategy_payload["summary"]["latest_selected_family"], "spot_grid")
        self.assertIsNotNone(strategy_payload["summary"]["latest_selected_strategy_sleeve_id"])
        self.assertIsNotNone(strategy_payload["summary"]["latest_allocation_id"])
        self.assertEqual(strategy_payload["summary"]["latest_selected_route_action"], "override_target")
        self.assertEqual(strategy_payload["summary"]["latest_allocator_version"], "task74_allocator_v2_phase2")
        self.assertGreaterEqual(strategy_payload["summary"]["latest_budget_profile_count"], 1)
        self.assertGreaterEqual(strategy_payload["summary"]["latest_budget_assignment_count"], 1)
        self.assertGreaterEqual(strategy_payload["summary"]["latest_budget_snapshot_count"], 1)
        self.assertIsNotNone(strategy_payload["summary"]["latest_portfolio_requested_notional"])
        self.assertIsNotNone(strategy_payload["summary"]["latest_portfolio_approved_notional"])
        self.assertIn(strategy_payload["summary"]["latest_bundle_type"], {"single_sleeve", "multi_sleeve", "hedge_protected"})
        self.assertEqual(strategy_payload["latest_applied_target"]["strategy_family"], "spot_grid")
        self.assertIsNotNone(strategy_payload["latest_applied_target"]["strategy_sleeve_id"])
        self.assertIsNotNone(strategy_payload["latest_applied_target"]["allocation_id"])
        self.assertEqual(strategy_payload["latest_applied_target"]["strategy_route_action"], "override_target")
        self.assertTrue(strategy_payload["strategy_sleeves"])
        self.assertTrue(strategy_payload["recent_budget_profiles"])
        self.assertTrue(strategy_payload["recent_budget_assignments"])
        self.assertTrue(strategy_payload["recent_budget_snapshots"])
        self.assertIn("spot_grid", strategy_payload["configured_parameters"])
        self.assertIn("dca", strategy_payload["configured_parameters"])
        self.assertNotIn("smart_arbitrage", strategy_payload["configured_parameters"])
        self.assertNotIn(
            "short_entry_min_signal_edge_bps",
            strategy_payload["configured_parameters"]["directional"],
        )
        self.assertNotIn(
            "short_reversal_confidence_min",
            strategy_payload["configured_parameters"]["directional"],
        )
        spot_grid_candidate = next(
            item for item in strategy_payload["latest_snapshot"]["candidates"] if item["family"] == "spot_grid"
        )
        self.assertIn("current_sleeve_position_qty", spot_grid_candidate["metrics"])
        self.assertIn("target_account_position_qty", spot_grid_candidate["metrics"])
        self.assertIn("expected_cost_bps", spot_grid_candidate["metrics"])
        self.assertEqual(system_payload["strategy_family_active"], "spot_grid")
        self.assertEqual(
            system_payload["strategy_runtime_summary"]["configured_active_family"],
            "spot_grid",
        )

    async def test_spot_grid_runtime_requires_full_anchor_history_before_activation(self) -> None:
        settings = self._settings(
            trading_product_type="spot",
            margin_mode="cash",
            default_symbol="BTC-USDT",
            allowed_symbols=("BTC-USDT",),
            strategy_family_active="spot_grid",
            spot_grid_enabled=True,
            spot_grid_breakout_guard_enabled=False,
            spot_grid_anchor_lookback_snapshots=4,
            spot_grid_band_bps=500.0,
            spot_grid_inventory_floor_fraction=0.0,
            spot_grid_inventory_ceiling_fraction=1.0,
            spot_grid_rebalance_min_fraction_of_max_qty=0.05,
            max_abs_position_qty=1.0,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=1,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertEqual(target.strategy_family, "directional")

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertEqual(payload["summary"]["latest_selected_family"], "directional")
        spot_grid_candidate = next(
            item for item in payload["latest_snapshot"]["candidates"] if item["family"] == "spot_grid"
        )
        self.assertEqual(spot_grid_candidate["state"], "inactive")
        self.assertEqual(spot_grid_candidate["route_action"], "hold_current")
        self.assertIn("spot_grid_anchor_history_insufficient", spot_grid_candidate["reason_codes"])

    async def test_dca_runtime_endpoint_exposes_interval_ready_target(self) -> None:
        settings = self._settings(
            trading_product_type="spot",
            margin_mode="cash",
            default_symbol="BTC-USDT",
            allowed_symbols=("BTC-USDT",),
            strategy_family_active="dca",
            dca_enabled=True,
            dca_interval_seconds=0.0,
            dca_quote_budget_per_cycle=100.0,
            max_abs_position_qty=1.0,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertEqual(target.strategy_family, "dca")
        self.assertEqual(target.strategy_route_action, "override_target")
        self.assertGreater(target.delta_position_qty, 0)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertEqual(payload["summary"]["configured_active_family"], "dca")
        self.assertEqual(payload["summary"]["latest_selected_family"], "dca")
        self.assertIsNotNone(payload["summary"]["latest_selected_strategy_sleeve_id"])
        self.assertEqual(payload["summary"]["latest_allocator_version"], "task74_allocator_v2_phase2")
        self.assertGreaterEqual(payload["summary"]["latest_budget_profile_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_budget_assignment_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_budget_snapshot_count"], 1)
        self.assertIsNotNone(payload["summary"]["latest_portfolio_requested_notional"])
        self.assertIsNotNone(payload["summary"]["latest_portfolio_approved_notional"])
        self.assertEqual(payload["latest_applied_target"]["strategy_family"], "dca")
        self.assertIsNotNone(payload["latest_applied_target"]["strategy_sleeve_id"])
        self.assertTrue(payload["summary"]["auto_parallel_enabled"])
        self.assertTrue(payload["recent_budget_profiles"])
        self.assertTrue(payload["recent_budget_assignments"])
        self.assertTrue(payload["recent_budget_snapshots"])
        dca_candidate = next(item for item in payload["latest_snapshot"]["candidates"] if item["family"] == "dca")
        dca_control = next(item for item in payload["latest_snapshot"]["automation_decisions"] if item["family"] == "dca")
        self.assertIn("current_sleeve_position_qty", dca_candidate["metrics"])
        self.assertIn("target_account_position_qty", dca_candidate["metrics"])
        self.assertIn("auto_budget_multiplier", dca_candidate["metrics"])
        self.assertIn("expected_cost_bps", dca_candidate["metrics"])
        self.assertEqual(dca_control["automation_state"], "active")
        self.assertEqual(
            payload["configured_parameters"]["dca"]["quote_budget_per_cycle"],
            100.0,
        )

    async def test_dca_pullback_only_runtime_requires_anchor_history_before_activation(self) -> None:
        settings = self._settings(
            trading_product_type="spot",
            margin_mode="cash",
            default_symbol="BTC-USDT",
            allowed_symbols=("BTC-USDT",),
            strategy_family_active="dca",
            dca_enabled=True,
            dca_interval_seconds=0.0,
            dca_quote_budget_per_cycle=100.0,
            dca_pullback_only_enabled=True,
            dca_pullback_entry_bps=0.0,
            max_abs_position_qty=1.0,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=1,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertEqual(target.strategy_family, "directional")

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertEqual(payload["summary"]["latest_selected_family"], "directional")
        dca_candidate = next(item for item in payload["latest_snapshot"]["candidates"] if item["family"] == "dca")
        self.assertEqual(dca_candidate["state"], "inactive")
        self.assertEqual(dca_candidate["route_action"], "hold_current")
        self.assertIn("dca_pullback_anchor_history_insufficient", dca_candidate["reason_codes"])

    async def test_allocator_runtime_endpoint_exposes_combined_spot_grid_and_dca_allocation(self) -> None:
        settings = self._settings(
            trading_product_type="spot",
            margin_mode="cash",
            default_symbol="BTC-USDT",
            allowed_symbols=("BTC-USDT",),
            strategy_family_active="spot_grid",
            spot_grid_enabled=True,
            spot_grid_breakout_guard_enabled=False,
            spot_grid_anchor_lookback_snapshots=4,
            spot_grid_band_bps=500.0,
            spot_grid_inventory_floor_fraction=0.0,
            spot_grid_inventory_ceiling_fraction=1.0,
            spot_grid_rebalance_min_fraction_of_max_qty=0.05,
            dca_enabled=True,
            dca_interval_seconds=0.0,
            dca_quote_budget_per_cycle=100.0,
            max_abs_position_qty=2.0,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertIn(target.strategy_family, {"spot_grid", "dca"})
        self.assertEqual(len(target.strategy_execution_legs), 2)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertIn(payload["summary"]["latest_selected_family"], {"spot_grid", "dca"})
        self.assertEqual(payload["summary"]["latest_approved_families"], ["spot_grid", "dca"])
        self.assertTrue(payload["summary"]["latest_approved_sleeve_weights"])
        self.assertEqual(payload["summary"]["latest_allocator_version"], "task74_allocator_v2_phase2")
        self.assertGreaterEqual(payload["summary"]["latest_budget_profile_count"], 2)
        self.assertGreaterEqual(payload["summary"]["latest_budget_assignment_count"], 2)
        self.assertGreaterEqual(payload["summary"]["latest_budget_snapshot_count"], 2)
        self.assertIn(
            payload["summary"]["latest_portfolio_risk_budget_state"],
            {"normal", "contracted", "hedge_protected"},
        )
        self.assertEqual(payload["latest_allocation_decision"]["approved_families"], ["spot_grid", "dca"])
        self.assertTrue(payload["latest_allocation_decision"]["approved_sleeve_weights"])
        self.assertEqual(payload["latest_allocation_decision"]["allocator_version"], "task74_allocator_v2_phase2")
        self.assertGreater(float(payload["latest_allocation_decision"]["portfolio_requested_notional"]), 0.0)
        self.assertGreater(float(payload["latest_allocation_decision"]["portfolio_approved_notional"]), 0.0)
        self.assertTrue(payload["recent_budget_profiles"])
        self.assertTrue(payload["recent_budget_assignments"])
        self.assertTrue(payload["recent_budget_snapshots"])
        self.assertIsInstance(payload["recent_conflict_resolutions"], list)
        self.assertTrue(payload["recent_netting_decisions"])
        self.assertIn("priority_rank", payload["recent_budget_snapshots"][0])
        self.assertIn("portfolio_requested_notional", payload["recent_budget_snapshots"][0])
        self.assertIn("portfolio_approved_notional", payload["recent_budget_snapshots"][0])
        self.assertIn("portfolio_budget_cut_notional", payload["recent_budget_snapshots"][0])
        self.assertIn("net_approved_qty", payload["recent_netting_decisions"][0])
        self.assertIn("participating_sleeve_ids", payload["recent_netting_decisions"][0])
        self.assertGreaterEqual(len(payload["recent_sleeve_intents"]), 4)
        self.assertEqual(len(payload["latest_bundle"]["legs"]), 2)
        self.assertEqual(set(payload["latest_bundle"]["participating_families"]), {"spot_grid", "dca"})
        self.assertEqual(payload["latest_bundle"]["bundle_type"], "multi_sleeve")
        self.assertIsNotNone(payload["latest_bundle"]["allocation_snapshot_ref"])
        self.assertEqual(
            set(payload["latest_bundle"]["budget_snapshot_ids"]),
            set(payload["latest_allocation_decision"]["budget_snapshot_ids"]),
        )

    async def test_spot_runtime_falls_back_from_incompatible_fixed_family(self) -> None:
        settings = self._settings(
            trading_product_type="spot",
            margin_mode="cash",
            default_symbol="BTC-USDT",
            allowed_symbols=("BTC-USDT",),
            strategy_family_active="smart_arbitrage",
            strategy_family_auto_selection_enabled=False,
            smart_arbitrage_enabled=True,
            max_abs_position_qty=1.0,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertEqual(target.strategy_family, "directional")

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertEqual(payload["summary"]["configured_active_family"], "smart_arbitrage")
        self.assertEqual(payload["summary"]["latest_selected_family"], "directional")
        self.assertIn(
            "legacy_configured_strategy_directional_fallback",
            payload["summary"]["latest_selection_reason_codes"],
        )
        self.assertIn(
            "smart_arbitrage_derivatives_runtime_required",
            payload["summary"]["latest_selection_reason_codes"],
        )
        self.assertNotIn(
            "legacy_configured_strategy_family_smart_arbitrage",
            payload["summary"]["latest_selection_reason_codes"],
        )

    async def test_smart_arbitrage_runtime_endpoint_exposes_executable_bundle_snapshot(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_family_active="smart_arbitrage",
            smart_arbitrage_enabled=True,
            smart_arbitrage_basis_entry_bps=0.0,
            smart_arbitrage_estimated_cost_bps=0.0,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol="BTC-USDT",
            iterations=1,
            interval_seconds=0.0,
        )
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertEqual(target.strategy_family, "smart_arbitrage")
        self.assertIn(target.strategy_route_action, {"override_target", "hold_current"})
        if target.strategy_route_action == "override_target":
            self.assertIsNotNone(target.strategy_bundle_id)
            self.assertEqual(len(target.strategy_execution_legs), 2)
        else:
            self.assertIsNone(target.strategy_bundle_id)
            self.assertEqual(len(target.strategy_execution_legs), 0)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertEqual(payload["summary"]["configured_active_family"], "smart_arbitrage")
        self.assertEqual(payload["summary"]["latest_selected_family"], "smart_arbitrage")
        self.assertIsNotNone(payload["summary"]["latest_selected_strategy_sleeve_id"])
        self.assertIsNotNone(payload["summary"]["latest_allocation_id"])
        self.assertIn(payload["summary"]["latest_selected_route_action"], {"override_target", "hold_current"})
        self.assertIsNotNone(payload["summary"]["latest_selected_pair_id"])
        self.assertEqual(payload["summary"]["latest_selected_execution_mode"], "spot_carry")
        self.assertIn(
            payload["summary"]["latest_selected_opportunity_kind"],
            {"positive_basis", "pair_hold", "pair_exit", "pair_recovery"},
        )
        self.assertIn(
            payload["summary"]["latest_selected_state_phase"],
            {"opening", "active", "unwinding", "recovery"},
        )
        self.assertEqual(payload["summary"]["latest_allocator_version"], "task74_allocator_v2_phase2")
        self.assertGreaterEqual(payload["summary"]["latest_budget_profile_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_budget_assignment_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_budget_snapshot_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_netting_decision_count"], 0)
        self.assertTrue(payload["summary"]["automatic_selection_enabled"])
        self.assertTrue(payload["summary"]["auto_parallel_enabled"])
        self.assertEqual(payload["latest_bundle"]["status"], "submitted")
        self.assertIsNotNone(payload["latest_bundle"]["strategy_sleeve_id"])
        self.assertIsNotNone(payload["latest_bundle"]["allocation_id"])
        self.assertIsNotNone(payload["latest_applied_target"]["strategy_sleeve_id"])
        self.assertIsNotNone(payload["latest_applied_target"]["allocation_id"])
        self.assertEqual(payload["latest_bundle"]["bundle_type"], "hedge_protected")
        self.assertIsNotNone(payload["latest_bundle"]["allocation_snapshot_ref"])
        self.assertGreaterEqual(float(payload["latest_bundle"]["gross_requested_exposure"]), 0.0)
        self.assertGreaterEqual(float(payload["latest_bundle"]["net_approved_exposure"]), 0.0)
        self.assertTrue(payload["latest_bundle"]["budget_snapshot_ids"])
        smart_arbitrage_candidate = next(
            item for item in payload["latest_snapshot"]["candidates"] if item["family"] == "smart_arbitrage"
        )
        smart_arbitrage_control = next(
            item for item in payload["latest_snapshot"]["automation_decisions"] if item["family"] == "smart_arbitrage"
        )
        self.assertIn(smart_arbitrage_candidate["route_action"], {"override_target", "hold_current"})
        self.assertEqual(smart_arbitrage_candidate["pair_id"], payload["summary"]["latest_selected_pair_id"])
        self.assertEqual(smart_arbitrage_candidate["execution_mode"], "spot_carry")
        self.assertIn(smart_arbitrage_candidate["state_phase"], {"opening", "active", "unwinding", "recovery"})
        self.assertEqual(len(smart_arbitrage_candidate["legs"]), 2)
        self.assertIn("derivatives_margin_mode", smart_arbitrage_candidate["metrics"])
        self.assertEqual(
            smart_arbitrage_candidate["legs"][1]["margin_mode"],
            smart_arbitrage_candidate["metrics"]["derivatives_margin_mode"],
        )
        self.assertIn("current_sleeve_spot_qty", smart_arbitrage_candidate["metrics"])
        self.assertIn("target_account_derivatives_qty", smart_arbitrage_candidate["metrics"])
        self.assertIn("inventory_backed_available_qty", smart_arbitrage_candidate["metrics"])
        self.assertIn("pair_definitions", smart_arbitrage_candidate["metrics"])
        self.assertIn("pair_registry_warning_codes", smart_arbitrage_candidate["metrics"])
        self.assertIn("pair_registry_error_codes", smart_arbitrage_candidate["metrics"])
        self.assertEqual(smart_arbitrage_candidate["metrics"]["pair_registry_source"], "coordinator_resolved")
        self.assertEqual(smart_arbitrage_control["automation_state"], "active")
        self.assertIsNotNone(payload["summary"]["latest_hedge_protected_notional"])
        self.assertTrue(payload["recent_budget_profiles"])
        self.assertTrue(payload["recent_budget_assignments"])
        self.assertTrue(payload["recent_budget_snapshots"])
        self.assertIsInstance(payload["recent_netting_decisions"], list)
        self.assertTrue(payload["recent_conflict_resolutions"])
        self.assertEqual(
            payload["recent_conflict_resolutions"][0]["resolution_action"],
            "non_hedge_families_reduced_to_protect_smart_arbitrage",
        )
        self.assertIn("protected_notional", payload["recent_conflict_resolutions"][0])
        self.assertIn("reduced_notional", payload["recent_conflict_resolutions"][0])
        self.assertNotIn("spot_grid", payload["configured_parameters"])
        self.assertNotIn("dca", payload["configured_parameters"])
        self.assertIn("pair_definitions", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("pair_registry_warning_codes", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("pair_registry_error_codes", payload["configured_parameters"]["smart_arbitrage"])
        self.assertEqual(payload["configured_parameters"]["smart_arbitrage"]["pair_registry_source"], "coordinator_resolved")
        self.assertEqual(
            payload["configured_parameters"]["smart_arbitrage"]["pair_definitions"],
            smart_arbitrage_candidate["metrics"]["pair_definitions"],
        )
        self.assertEqual(
            payload["configured_parameters"]["smart_arbitrage"]["pair_registry_warning_codes"],
            smart_arbitrage_candidate["metrics"]["pair_registry_warning_codes"],
        )
        self.assertEqual(
            payload["configured_parameters"]["smart_arbitrage"]["pair_registry_error_codes"],
            smart_arbitrage_candidate["metrics"]["pair_registry_error_codes"],
        )
        self.assertEqual(
            payload["configured_parameters"]["smart_arbitrage"]["pair_registry_source"],
            smart_arbitrage_candidate["metrics"]["pair_registry_source"],
        )
        self.assertNotIn("v2_enabled", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("companion_spot_symbol", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("companion_derivatives_symbol", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("quote_budget_per_trade", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("max_pair_notional", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("negative_basis_mode", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("cost_model_enabled", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("funding_cost_enabled", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("borrow_cost_enabled", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("margin_short_auto_repay_enabled", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("max_concurrent_pairs", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("pair_priority_mode", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("min_inventory_backed_ratio", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("estimated_funding_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("estimated_borrow_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("fee_source_mode", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("funding_source_mode", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("borrow_source_mode", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("expected_hold_hours", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("funding_interval_hours", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("expected_funding_events", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("hedge_target_leverage", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("estimated_execution_mismatch_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("estimated_transfer_cost_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("time_decay_bps_per_hour", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("estimated_borrow_apr", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("borrow_interest_free_ratio", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("trade_costs", payload["configured_parameters"])
        self.assertIn("directional", payload["configured_parameters"])
        self.assertIn("short_bias_enabled", payload["configured_parameters"]["directional"])
        self.assertIn("runtime_shorting_blockers", payload["configured_parameters"]["directional"])
        self.assertIn("short_entry_min_signal_edge_bps", payload["configured_parameters"]["directional"])
        self.assertIn("short_reversal_confidence_min", payload["configured_parameters"]["directional"])
        self.assertIn("spot_taker_fee_bps", payload["configured_parameters"]["trade_costs"])
        self.assertIn("margin_taker_fee_bps", payload["configured_parameters"]["trade_costs"])
        self.assertIn("derivatives_taker_fee_bps", payload["configured_parameters"]["trade_costs"])
        self.assertIn("delivery_settlement_fee_bps", payload["configured_parameters"]["trade_costs"])
        self.assertIn("spot_slippage_bps", payload["configured_parameters"]["trade_costs"])
        self.assertNotIn("estimated_fee_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_slippage_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_spot_open_fee_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_spot_close_fee_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_hedge_open_fee_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_hedge_close_fee_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_spot_spread_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_hedge_spread_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_spot_slippage_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertNotIn("estimated_hedge_slippage_bps", payload["configured_parameters"]["smart_arbitrage"])
        self.assertIn("short_entry_min_signal_edge_bps", payload["configured_parameters"]["directional"])
        self.assertIn("short_reversal_confidence_min", payload["configured_parameters"]["directional"])

    async def test_derivatives_hedge_mode_directional_runtime_exposes_overlay_config_and_leg_semantics(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_short_bias_enabled=True,
            derivatives_position_mode="hedge",
            strategy_hedge_overlay_enabled=True,
            strategy_cost_guard_enabled=False,
            strategy_entry_min_signal_edge_bps=0.0,
            strategy_entry_alpha_min=0.0,
            strategy_entry_confidence_min=0.0,
        )
        runtime = await build_runtime(settings)
        runtime.portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=datetime.now(timezone.utc),
                balances={"USDT": 75_000.0},
                positions=[],
                cost_basis={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_equity=75_000.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                risk_budget_usage={},
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.MARKET_SNAPSHOTS,
                key=settings.default_symbol,
                payload_model=MarketSnapshot(
                    symbol=settings.default_symbol,
                    exchange="OKX",
                    snapshot_ts=datetime.now(timezone.utc),
                    best_bid=70_000.0,
                    best_ask=70_001.0,
                    last_price=70_000.5,
                    bid_size=1.0,
                    ask_size=1.0,
                    volume_24h=10_000_000.0,
                    kline_15m={"open": 69_900.0, "high": 70_100.0, "low": 69_800.0, "close": 70_000.5},
                    kline_1h={"open": 69_800.0, "high": 70_200.0, "low": 69_700.0, "close": 70_000.5},
                ),
                source_component="test",
            )
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.FEATURE_SNAPSHOTS,
                key=settings.default_symbol,
                payload_model=FeatureSnapshot(
                    symbol=settings.default_symbol,
                    snapshot_ts=datetime.now(timezone.utc),
                    market_snapshot_ref="evt_market",
                    trend_strength=0.8,
                    volatility_state="medium",
                    volatility_value=0.2,
                    momentum_score=12.0,
                    liquidity_score=0.85,
                    regime_indicator="trend",
                    regime_confidence=0.82,
                    multi_timeframe_alignment=0.75,
                    composite_alpha_score=0.42,
                    suggested_position_scale=1.0,
                    volatility_target_scale=1.0,
                    feature_version="test",
                ),
                source_component="test",
            )
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertGreater(target.target_position_qty, 0)
        self.assertGreaterEqual(len(target.strategy_execution_legs), 1)
        self.assertIsNotNone(target.hedge_overlay_decision)
        assert target.hedge_overlay_decision is not None
        self.assertTrue(target.hedge_overlay_decision.runtime_supported)
        self.assertEqual(target.hedge_overlay_decision.state, "disabled")
        primary_leg = target.strategy_execution_legs[0]
        self.assertEqual(primary_leg.position_mode, "long_short_mode")
        self.assertEqual(primary_leg.pos_side, "long")
        self.assertEqual(primary_leg.action, "open")

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertTrue(payload["configured_parameters"]["directional"]["hedge_overlay_enabled"])
        self.assertTrue(payload["configured_parameters"]["directional"]["hedge_overlay_runtime_supported"])
        self.assertTrue(payload["configured_parameters"]["directional"]["hedge_overlay_effective_enabled"])
        applied_target = payload["latest_applied_target"]
        self.assertIn("hedge_overlay_decision", applied_target)
        self.assertTrue(applied_target["strategy_execution_legs"])
        self.assertEqual(applied_target["strategy_execution_legs"][0]["position_mode"], "long_short_mode")
        self.assertEqual(applied_target["strategy_execution_legs"][0]["pos_side"], "long")
        self.assertEqual(applied_target["strategy_execution_legs"][0]["action"], "open")
        self.assertTrue(payload["smart_arbitrage_cost_summary"]["available"])
        self.assertIn("predicted", payload["smart_arbitrage_cost_summary"])
        self.assertIn("realized", payload["smart_arbitrage_cost_summary"])
        self.assertIn("calibration", payload["smart_arbitrage_cost_summary"])
        self.assertIn("executable_cost_bps", payload["smart_arbitrage_cost_summary"]["predicted"])
        self.assertIn("ideal_edge_bps", payload["smart_arbitrage_cost_summary"]["predicted"])
        self.assertIn("cost_source_flags", payload["smart_arbitrage_cost_summary"]["predicted"])
        self.assertIn("fill_count", payload["smart_arbitrage_cost_summary"]["realized"])
        self.assertIn(
            "predicted_vs_realized_total_drag_error_bps",
            payload["smart_arbitrage_cost_summary"]["calibration"],
        )

    async def test_derivatives_opportunistic_overlay_runtime_exposes_effective_mode_and_leg_semantics(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_short_bias_enabled=True,
            derivatives_position_mode="hedge",
            strategy_hedge_overlay_enabled=True,
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_min_safe_net_edge_bps=3.0,
            strategy_hedge_opportunistic_expected_slippage_buffer_bps=1.0,
            strategy_hedge_opportunistic_expected_execution_buffer_bps=2.0,
            strategy_hedge_opportunistic_weak_edge_execution_mode="report_only",
            strategy_hedge_opportunistic_max_acceptable_cost_bps=7.5,
            strategy_hedge_opportunistic_passive_first_enabled=True,
            strategy_cost_guard_enabled=False,
            strategy_entry_min_signal_edge_bps=0.0,
            strategy_entry_alpha_min=0.0,
            strategy_entry_confidence_min=0.0,
            strategy_reversal_min_signal_edge_bps=50.0,
            strategy_reversal_alpha_min=0.60,
            strategy_reversal_confidence_min=0.95,
            strategy_short_reversal_min_signal_edge_bps=50.0,
            strategy_short_reversal_alpha_min=0.60,
            strategy_short_reversal_confidence_min=0.95,
            strategy_edge_noise_buffer_bps=0.0,
        )
        runtime = await build_runtime(settings)
        decision_id = "dec_opportunistic_overlay"
        runtime.event_store.append(
            build_envelope(
                topic=topics.POSITION_TARGETS,
                key=settings.default_symbol,
                payload_model=PositionTarget(
                    decision_id=decision_id,
                    symbol=settings.default_symbol,
                    current_position_qty=Decimal("0.05"),
                    target_position_qty=Decimal("0.0325"),
                    delta_position_qty=Decimal("-0.0175"),
                    current_notional=Decimal("3500"),
                    target_notional=Decimal("2275"),
                    rebalance_reason="opportunistic_overlay_test",
                    urgency="medium",
                    max_slippage_tolerance_bps=20,
                    source_mix={"baseline": 1.0},
                    decision_expiry_ts=datetime.now(timezone.utc) + timedelta(minutes=5),
                    product_type="derivatives",
                    current_exposure_side="long",
                    target_exposure_side="long",
                    position_intent="reduce_long",
                    target_leverage=1.0,
                    margin_mode="cross",
                    strategy_family="directional",
                    strategy_execution_mode="opportunistic_overlay",
                    strategy_reason_codes=["opportunistic_overlay_active"],
                    strategy_execution_legs=[
                        StrategyLegIntent(
                            symbol=settings.default_symbol,
                            product_type="derivatives",
                            side="sell",
                            position_mode="long_short_mode",
                            pos_side="short",
                            action="open",
                            family="directional",
                            role="hedge",
                            margin_mode="cross",
                            target_leverage=1.0,
                            current_position_qty=Decimal("0"),
                            target_position_qty=Decimal("0.0175"),
                            delta_position_qty=Decimal("0.0175"),
                            reference_price=Decimal("70000.5"),
                            execution_compatible=True,
                            execution_mode="opportunistic_overlay",
                            overlay_mode="opportunistic",
                            hedge_ratio=Decimal("0.35"),
                            trigger_reason_codes=["opportunistic_overlay_signal_above_open_threshold"],
                            note="Directional opportunistic overlay 生成的机会腿。",
                        )
                    ],
                    hedge_overlay_decision=HedgeOverlayDecision(
                        enabled=True,
                        runtime_supported=True,
                        configured_mode="opportunistic",
                        effective_mode="opportunistic",
                        overlay_source="opportunistic",
                        active=True,
                        state="opening",
                        main_leg_signal="long",
                        hedge_leg_signal="short",
                        main_leg_current_qty=Decimal("0.05"),
                        hedge_leg_current_qty=Decimal("0"),
                        main_leg_target_qty=Decimal("0.05"),
                        hedge_leg_target_qty=Decimal("0.0175"),
                        hedge_ratio=Decimal("0.35"),
                        max_ratio=Decimal("0.35"),
                        pressure_score=0.82,
                        open_threshold=0.62,
                        close_threshold=0.46,
                        open_condition="opportunistic_score_threshold_crossed",
                        fee_drag_ratio=0.04,
                        churn_ratio=0.08,
                        reason_codes=["opportunistic_overlay_signal_above_open_threshold"],
                    ),
                ),
                source_component="test",
            )
        )

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertEqual(payload["configured_parameters"]["directional"]["hedge_overlay_mode"], "opportunistic")
        self.assertTrue(payload["configured_parameters"]["directional"]["hedge_opportunistic_enabled"])
        self.assertTrue(payload["configured_parameters"]["directional"]["hedge_overlay_mode_ready"])
        self.assertTrue(payload["configured_parameters"]["directional"]["hedge_overlay_effective_enabled"])
        self.assertEqual(payload["configured_parameters"]["directional"]["hedge_opportunistic_min_safe_net_edge_bps"], 3.0)
        self.assertEqual(payload["configured_parameters"]["directional"]["hedge_opportunistic_expected_slippage_buffer_bps"], 1.0)
        self.assertEqual(payload["configured_parameters"]["directional"]["hedge_opportunistic_expected_execution_buffer_bps"], 2.0)
        self.assertEqual(payload["configured_parameters"]["directional"]["hedge_opportunistic_weak_edge_execution_mode"], "report_only")
        self.assertEqual(payload["configured_parameters"]["directional"]["hedge_opportunistic_max_acceptable_cost_bps"], 7.5)
        self.assertTrue(payload["configured_parameters"]["directional"]["hedge_opportunistic_passive_first_enabled"])
        applied_target = payload["latest_applied_target"]
        self.assertEqual(applied_target["hedge_overlay_decision"]["effective_mode"], "opportunistic")
        self.assertEqual(applied_target["hedge_overlay_decision"]["overlay_source"], "opportunistic")
        self.assertEqual(applied_target["hedge_overlay_decision"]["state"], "opening")
        self.assertEqual(applied_target["strategy_execution_legs"][0]["position_mode"], "long_short_mode")
        self.assertEqual(applied_target["strategy_execution_legs"][0]["pos_side"], "short")
        self.assertEqual(applied_target["strategy_execution_legs"][0]["action"], "open")
        self.assertEqual(applied_target["strategy_execution_legs"][0]["execution_mode"], "opportunistic_overlay")
        self.assertEqual(applied_target["strategy_execution_legs"][0]["overlay_mode"], "opportunistic")

    async def test_managed_derivatives_live_profile_selects_independent_family_for_overlay(self) -> None:
        values = load_managed_profile_values("derivatives_live")
        settings = self._settings(
            **{
                **values,
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "max_abs_position_qty": 1.0,
            }
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertEqual(settings.derivatives_position_mode, "hedge")
        self.assertEqual(settings.strategy_family_active, "independent")
        self.assertFalse(settings.strategy_family_auto_selection_enabled)
        self.assertTrue(settings.strategy_family_independent_enabled)
        self.assertFalse(settings.strategy_family_independent_shadow_mode_enabled)
        self.assertTrue(settings.strategy_family_independent_live_execution_enabled)
        self.assertFalse(settings.strategy_family_protective_shadow_mode_enabled)
        self.assertFalse(settings.strategy_family_opportunistic_shadow_mode_enabled)
        self.assertFalse(settings.smart_arbitrage_enabled)
        self.assertEqual(settings.strategy_hedge_overlay_mode, "independent")
        self.assertFalse(settings.strategy_hedge_protective_enabled)
        self.assertFalse(settings.strategy_hedge_opportunistic_enabled)
        self.assertEqual(settings.strategy_hedge_opportunistic_rollout_stage, "dry_run")
        self.assertEqual(settings.strategy_hedge_independent_long_entry_threshold, 0.30)
        self.assertEqual(settings.strategy_hedge_independent_short_entry_threshold, 0.30)
        self.assertEqual(settings.strategy_hedge_independent_long_close_threshold, 0.24)
        self.assertEqual(settings.strategy_hedge_independent_short_close_threshold, 0.24)
        self.assertEqual(settings.strategy_hedge_independent_long_scale_in_threshold, 0.40)
        self.assertEqual(settings.strategy_hedge_independent_short_scale_in_threshold, 0.40)
        self.assertEqual(settings.strategy_hedge_independent_min_safe_net_edge_bps, 3.0)
        self.assertEqual(settings.strategy_hedge_independent_expected_slippage_buffer_bps, 1.0)
        self.assertEqual(settings.strategy_hedge_independent_expected_execution_buffer_bps, 2.0)
        self.assertEqual(settings.strategy_hedge_independent_weak_edge_execution_mode, "report_only")
        self.assertEqual(settings.strategy_hedge_independent_max_acceptable_cost_bps, 7.5)
        self.assertTrue(settings.strategy_hedge_independent_passive_first_enabled)
        self.assertEqual(settings.strategy_hedge_independent_entry_execution_mode, "passive_first")
        self.assertEqual(settings.strategy_hedge_independent_scale_in_execution_mode, "bounded_limit")
        self.assertEqual(settings.strategy_hedge_independent_de_risk_execution_mode, "bounded_taker")
        self.assertEqual(settings.strategy_hedge_independent_close_failed_thesis_execution_mode, "aggressive_bounded_taker")
        self.assertEqual(settings.strategy_hedge_independent_close_stale_execution_mode, "bounded_limit")
        self.assertEqual(settings.strategy_hedge_independent_limit_offset_bps_entry, 1.5)
        self.assertEqual(settings.strategy_hedge_independent_limit_offset_bps_scale_in, 1.0)
        self.assertEqual(settings.strategy_hedge_independent_limit_offset_bps_stale_close, 0.8)
        self.assertFalse(settings.strategy_hedge_independent_adaptive_rollout_enabled)
        self.assertFalse(settings.strategy_hedge_independent_health_enforcement_enabled)
        self.assertFalse(settings.strategy_hedge_independent_size_down_entry_enabled)
        self.assertFalse(settings.strategy_hedge_independent_long_short_asymmetry_enabled)
        self.assertEqual(settings.strategy_hedge_independent_short_asymmetry_penalty_multiplier, 0.85)
        self.assertEqual(settings.strategy_hedge_independent_entry_size_down_floor, 0.50)
        self.assertEqual(target.strategy_family, "independent")

    async def test_derivatives_independent_overlay_runtime_exposes_leg_scoped_thresholds_and_books(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_short_bias_enabled=True,
            derivatives_position_mode="hedge",
            strategy_hedge_overlay_enabled=True,
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_long_entry_threshold=0.66,
            strategy_hedge_independent_short_entry_threshold=0.64,
            strategy_hedge_independent_long_close_threshold=0.52,
            strategy_hedge_independent_short_close_threshold=0.50,
            strategy_hedge_independent_long_scale_in_threshold=0.70,
            strategy_hedge_independent_short_scale_in_threshold=0.68,
            strategy_hedge_independent_long_min_hold_seconds=300.0,
            strategy_hedge_independent_short_min_hold_seconds=420.0,
            strategy_hedge_independent_rebalance_cooldown_seconds=120.0,
            strategy_hedge_independent_trial_guard_enabled=True,
            strategy_hedge_independent_min_confirm_ticks=2,
            strategy_hedge_independent_min_score_stability_bps=2.0,
            strategy_hedge_independent_min_liquidity_quality=0.55,
            strategy_hedge_independent_require_execution_health_ok=True,
            strategy_hedge_independent_max_thesis_age_seconds=1800,
            strategy_hedge_independent_de_risk_net_edge_bps=2.0,
            strategy_hedge_independent_failed_thesis_net_edge_bps=-1.0,
            strategy_hedge_independent_execution_health_de_risk_enabled=True,
            strategy_hedge_independent_liquidity_de_risk_enabled=True,
            strategy_hedge_independent_min_safe_net_edge_bps=3.0,
            strategy_hedge_independent_expected_slippage_buffer_bps=1.0,
            strategy_hedge_independent_expected_execution_buffer_bps=2.0,
            strategy_hedge_independent_weak_edge_execution_mode="report_only",
            strategy_hedge_independent_max_acceptable_cost_bps=7.5,
            strategy_hedge_independent_passive_first_enabled=True,
            strategy_hedge_independent_entry_execution_mode="passive_first",
            strategy_hedge_independent_scale_in_execution_mode="bounded_limit",
            strategy_hedge_independent_de_risk_execution_mode="bounded_taker",
            strategy_hedge_independent_close_failed_thesis_execution_mode="aggressive_bounded_taker",
            strategy_hedge_independent_close_stale_execution_mode="bounded_limit",
            strategy_hedge_independent_limit_offset_bps_entry=1.5,
            strategy_hedge_independent_limit_offset_bps_scale_in=1.0,
            strategy_hedge_independent_limit_offset_bps_stale_close=0.8,
        )
        runtime = await build_runtime(settings)
        runtime.event_store.append(
            build_envelope(
                topic=topics.POSITION_TARGETS,
                key=settings.default_symbol,
                payload_model=PositionTarget(
                    decision_id="dec_independent_overlay",
                    symbol=settings.default_symbol,
                    current_position_qty=Decimal("0.01"),
                    target_position_qty=Decimal("0.01"),
                    delta_position_qty=Decimal("0"),
                    current_notional=Decimal("700"),
                    target_notional=Decimal("700"),
                    rebalance_reason="independent_overlay_test",
                    urgency="medium",
                    max_slippage_tolerance_bps=20,
                    source_mix={"baseline": 1.0},
                    decision_expiry_ts=datetime.now(timezone.utc) + timedelta(minutes=5),
                    product_type="derivatives",
                    current_exposure_side="long",
                    target_exposure_side="long",
                    position_intent="hold",
                    target_leverage=1.0,
                    margin_mode="cross",
                    strategy_family="directional",
                    strategy_execution_mode="independent_books",
                    strategy_reason_codes=["independent_books_active"],
                    strategy_execution_legs=[
                        StrategyLegIntent(
                            symbol=settings.default_symbol,
                            product_type="derivatives",
                            side="buy",
                            position_mode="long_short_mode",
                            pos_side="long",
                            action="open",
                            family="directional",
                            role="primary",
                            margin_mode="cross",
                            target_leverage=1.0,
                            current_position_qty=Decimal("0"),
                            target_position_qty=Decimal("0.03"),
                            delta_position_qty=Decimal("0.03"),
                            reference_price=Decimal("70000.5"),
                            execution_compatible=True,
                            execution_mode="independent_long_book",
                            overlay_mode="independent",
                            trigger_reason_codes=["independent_long_book_signal_above_entry_threshold"],
                            note="Independent long book 决策腿。",
                        ),
                        StrategyLegIntent(
                            symbol=settings.default_symbol,
                            product_type="derivatives",
                            side="buy",
                            position_mode="long_short_mode",
                            pos_side="short",
                            action="close",
                            family="directional",
                            role="primary",
                            margin_mode="cross",
                            target_leverage=1.0,
                            current_position_qty=Decimal("-0.02"),
                            target_position_qty=Decimal("-0.02"),
                            delta_position_qty=Decimal("0"),
                            reference_price=Decimal("70000.5"),
                            execution_compatible=True,
                            execution_mode="independent_short_book",
                            overlay_mode="independent",
                            trigger_reason_codes=["independent_short_book_hold_above_entry_threshold"],
                            note="Independent short book 决策腿。",
                        ),
                    ],
                    family_execution_summary={
                        "summary_mode": "multi_leg",
                        "family": "independent",
                        "route_action": "override_target",
                        "family_action": "open_independent_book",
                        "leg_count": 2,
                        "position_intents": ["open_long", "close_short"],
                        "directions": ["long", "short"],
                        "leg_actions": ["open", "close"],
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
                    hedge_overlay_decision=HedgeOverlayDecision(
                        enabled=True,
                        runtime_supported=True,
                        configured_mode="independent",
                        effective_mode="independent",
                        overlay_source="independent_books",
                        active=True,
                        state="holding",
                        main_leg_signal="long",
                        hedge_leg_signal="short",
                        main_leg_current_qty=Decimal("0.03"),
                        hedge_leg_current_qty=Decimal("0.02"),
                        main_leg_target_qty=Decimal("0.03"),
                        hedge_leg_target_qty=Decimal("0.02"),
                        hedge_ratio=Decimal("0.6667"),
                        max_ratio=Decimal("1"),
                        pressure_score=0.74,
                        open_threshold=0.66,
                        close_threshold=0.64,
                        fee_drag_ratio=0.05,
                        churn_ratio=0.08,
                        long_leg_score=0.74,
                        short_leg_score=0.68,
                        long_leg_reason_codes=["independent_long_book_signal_above_entry_threshold"],
                        short_leg_reason_codes=["independent_short_book_hold_above_entry_threshold"],
                        blocked_reasons=[],
                        reason_codes=[
                            "independent_long_book_signal_above_entry_threshold",
                            "independent_short_book_hold_above_entry_threshold",
                        ],
                    ),
                ),
                source_component="test",
            )
        )

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        directional = payload["configured_parameters"]["directional"]
        self.assertEqual(directional["hedge_overlay_mode"], "independent")
        self.assertTrue(directional["hedge_protective_enabled"])
        self.assertTrue(directional["hedge_independent_enabled"])
        self.assertTrue(directional["hedge_overlay_mode_ready"])
        self.assertTrue(directional["hedge_overlay_effective_enabled"])
        self.assertEqual(directional["hedge_independent_long_entry_threshold"], 0.66)
        self.assertEqual(directional["hedge_independent_short_entry_threshold"], 0.64)
        self.assertEqual(directional["hedge_independent_long_close_threshold"], 0.52)
        self.assertEqual(directional["hedge_independent_short_close_threshold"], 0.50)
        self.assertEqual(directional["hedge_independent_long_scale_in_threshold"], 0.70)
        self.assertEqual(directional["hedge_independent_short_scale_in_threshold"], 0.68)
        self.assertEqual(directional["hedge_independent_short_min_hold_seconds"], 420.0)
        self.assertEqual(directional["hedge_independent_min_safe_net_edge_bps"], 3.0)
        self.assertEqual(directional["hedge_independent_min_confirm_ticks"], 2)
        self.assertEqual(directional["hedge_independent_min_score_stability_bps"], 2.0)
        self.assertEqual(directional["hedge_independent_min_liquidity_quality"], 0.55)
        self.assertTrue(directional["hedge_independent_require_execution_health_ok"])
        self.assertEqual(directional["hedge_independent_max_thesis_age_seconds"], 1800)
        self.assertEqual(directional["hedge_independent_de_risk_net_edge_bps"], 2.0)
        self.assertEqual(directional["hedge_independent_failed_thesis_net_edge_bps"], -1.0)
        self.assertTrue(directional["hedge_independent_execution_health_de_risk_enabled"])
        self.assertTrue(directional["hedge_independent_liquidity_de_risk_enabled"])
        self.assertEqual(directional["hedge_independent_expected_slippage_buffer_bps"], 1.0)
        self.assertEqual(directional["hedge_independent_expected_execution_buffer_bps"], 2.0)
        self.assertEqual(directional["hedge_independent_weak_edge_execution_mode"], "report_only")
        self.assertEqual(directional["hedge_independent_max_acceptable_cost_bps"], 7.5)
        self.assertTrue(directional["hedge_independent_passive_first_enabled"])
        self.assertEqual(directional["hedge_independent_entry_execution_mode"], "passive_first")
        self.assertEqual(directional["hedge_independent_scale_in_execution_mode"], "bounded_limit")
        self.assertEqual(directional["hedge_independent_de_risk_execution_mode"], "bounded_taker")
        self.assertEqual(directional["hedge_independent_close_failed_thesis_execution_mode"], "aggressive_bounded_taker")
        self.assertEqual(directional["hedge_independent_close_stale_execution_mode"], "bounded_limit")
        self.assertEqual(directional["hedge_independent_limit_offset_bps_entry"], 1.5)
        self.assertEqual(directional["hedge_independent_limit_offset_bps_scale_in"], 1.0)
        self.assertEqual(directional["hedge_independent_limit_offset_bps_stale_close"], 0.8)
        applied_target = payload["latest_applied_target"]
        self.assertEqual(applied_target["hedge_overlay_decision"]["effective_mode"], "independent")
        self.assertEqual(applied_target["hedge_overlay_decision"]["overlay_source"], "independent_books")
        self.assertEqual(applied_target["hedge_overlay_decision"]["long_leg_score"], 0.74)
        self.assertEqual(applied_target["hedge_overlay_decision"]["short_leg_score"], 0.68)
        self.assertEqual(len(applied_target["strategy_execution_legs"]), 2)
        self.assertEqual(applied_target["strategy_execution_legs"][0]["execution_mode"], "independent_long_book")
        self.assertEqual(applied_target["strategy_execution_legs"][0]["overlay_mode"], "independent")
        self.assertEqual(applied_target["strategy_execution_legs"][1]["execution_mode"], "independent_short_book")
        self.assertEqual(applied_target["strategy_execution_legs"][1]["overlay_mode"], "independent")
        self.assertIsNotNone(applied_target["family_execution_summary"])
        self.assertEqual(applied_target["family_execution_summary"]["summary_mode"], "multi_leg")
        self.assertEqual(
            applied_target["family_execution_summary"]["position_intents"],
            ["open_long", "close_short"],
        )
        self.assertEqual(
            applied_target["family_execution_summary"]["directions"],
            ["long", "short"],
        )
        self.assertEqual(
            applied_target["family_execution_summary"]["book_expectancy_summary"]["source"],
            "independent_book",
        )
        self.assertEqual(
            applied_target["family_execution_summary"]["book_expectancy_summary"]["books"][0]["expected_net_edge_bps"],
            12.0,
        )
        self.assertEqual(
            applied_target["family_execution_summary"]["book_expectancy_summary"]["books"][1]["expected_net_edge_bps"],
            -2.0,
        )

    async def test_derivatives_overlay_runtime_exposes_rollout_stage_blockers_for_live_runtime(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            derivatives_position_mode="hedge",
            strategy_hedge_overlay_enabled=True,
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_opportunistic_enabled=True,
            strategy_hedge_opportunistic_rollout_stage="live",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_rollout_stage="dry_run",
            guarded_execution_dry_run=False,
            live_submit_enabled=True,
            okx_simulated_trading=False,
        )
        runtime = await build_runtime(settings)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        directional = payload["configured_parameters"]["directional"]
        rollout = directional["hedge_rollout"]
        self.assertEqual(rollout["runtime_stage"], "live")
        self.assertEqual(rollout["current_mode"], "independent")
        self.assertFalse(rollout["current_mode_allowed"])
        self.assertIn(
            "independent_overlay_rollout_stage_blocks_live_runtime",
            rollout["current_mode_blocking_reasons"],
        )
        self.assertEqual(rollout["opportunistic"]["configured_rollout_stage"], "live")
        self.assertTrue(rollout["opportunistic"]["runtime_allowed"])
        self.assertEqual(rollout["independent"]["configured_rollout_stage"], "dry_run")
        self.assertFalse(rollout["independent"]["runtime_allowed"])
        self.assertTrue(rollout["rollback_sequence"])

    async def test_derivatives_position_target_bus_keeps_long_short_leg_books_isolated(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            derivatives_position_mode="hedge",
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=1,
            interval_seconds=0.0,
        )

        target = PositionTarget(
            decision_id="dec_independent_leg_isolation",
            symbol=settings.default_symbol,
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.01"),
            delta_position_qty=Decimal("0.01"),
            current_notional=Decimal("0"),
            target_notional=Decimal("800"),
            rebalance_reason="independent_leg_isolation_test",
            urgency="medium",
            max_slippage_tolerance_bps=25,
            source_mix={"directional": 1.0},
            decision_expiry_ts=datetime.now(timezone.utc) + timedelta(minutes=5),
            product_type="derivatives",
            current_exposure_side="flat",
            target_exposure_side="long",
            position_intent="open_long",
            target_leverage=2.0,
            margin_mode="cross",
            strategy_family="directional",
            strategy_sleeve_id="sleeve_directional_primary",
            strategy_route_action="override_target",
            strategy_execution_mode="independent_overlay_test",
            strategy_state_phase="active",
            strategy_reason_codes=["independent_overlay_test"],
            allocation_id="alloc_independent_bundle",
            strategy_bundle_id="bundle_independent_leg_isolation",
            strategy_execution_legs=[
                StrategyLegIntent(
                    symbol=settings.default_symbol,
                    product_type="derivatives",
                    side="buy",
                    position_mode="long_short_mode",
                    pos_side="long",
                    action="open",
                    family="directional",
                    role="primary",
                    strategy_sleeve_id="sleeve_independent_long",
                    allocation_id="alloc_independent_long",
                    margin_mode="cross",
                    target_leverage=2.0,
                    current_position_qty=Decimal("0"),
                    target_position_qty=Decimal("0.02"),
                    delta_position_qty=Decimal("0.02"),
                    reference_price=Decimal("80000"),
                    execution_compatible=True,
                    execution_mode="independent_long_book",
                    state_phase="active",
                    overlay_mode="independent",
                    trigger_reason_codes=["independent_long_book_signal_above_entry_threshold"],
                ),
                StrategyLegIntent(
                    symbol=settings.default_symbol,
                    product_type="derivatives",
                    side="sell",
                    position_mode="long_short_mode",
                    pos_side="short",
                    action="open",
                    family="directional",
                    role="primary",
                    strategy_sleeve_id="sleeve_independent_short",
                    allocation_id="alloc_independent_short",
                    margin_mode="cross",
                    target_leverage=2.0,
                    current_position_qty=Decimal("0"),
                    target_position_qty=Decimal("-0.01"),
                    delta_position_qty=Decimal("-0.01"),
                    reference_price=Decimal("80000"),
                    execution_compatible=True,
                    execution_mode="independent_short_book",
                    state_phase="active",
                    overlay_mode="independent",
                    trigger_reason_codes=["independent_short_book_signal_above_entry_threshold"],
                ),
            ],
        )

        await publish_model(
            bus=runtime.bus,
            topic=topics.POSITION_TARGETS,
            key=settings.default_symbol,
            payload_model=target,
            source_component="test",
        )

        bundle_events = runtime.event_store.by_topic(topics.STRATEGY_EXECUTION_BUNDLES)
        self.assertTrue(bundle_events)
        bundle = StrategyExecutionBundle.model_validate(bundle_events[-1].payload)
        self.assertEqual(len(bundle.legs), 2)
        long_leg = next(item for item in bundle.legs if item.pos_side == "long")
        short_leg = next(item for item in bundle.legs if item.pos_side == "short")
        self.assertEqual(long_leg.current_position_qty, Decimal("0"))
        self.assertEqual(long_leg.target_position_qty, Decimal("0.02"))
        self.assertEqual(long_leg.delta_position_qty, Decimal("0.02"))
        self.assertEqual(short_leg.current_position_qty, Decimal("0"))
        self.assertEqual(short_leg.target_position_qty, Decimal("-0.01"))
        self.assertEqual(short_leg.delta_position_qty, Decimal("-0.01"))
        matching_order_states = [
            state
            for state in runtime.execution_repo.order_states()
            if str(state.strategy_bundle_id or "").strip() == bundle.bundle_id
        ]
        if matching_order_states:
            self.assertTrue(
                {"long", "short"}.issubset(
                    {state.pos_side for state in matching_order_states if state.pos_side is not None}
                )
            )
        else:
            self.assertTrue(all(leg.risk_approved is False for leg in bundle.legs))

    async def test_smart_arbitrage_runtime_cost_summary_skips_funding_without_boundary_crossing(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_family_active="smart_arbitrage",
            smart_arbitrage_enabled=True,
            smart_arbitrage_basis_entry_bps=0.0,
            smart_arbitrage_estimated_cost_bps=0.0,
            smart_arbitrage_funding_cost_enabled=True,
            smart_arbitrage_funding_source_mode="configured",
            smart_arbitrage_estimated_funding_bps=2.0,
            smart_arbitrage_expected_hold_hours=6.0,
            smart_arbitrage_funding_interval_hours=8.0,
            trade_cost_spot_taker_fee_bps=0.0,
            trade_cost_margin_taker_fee_bps=0.0,
            trade_cost_derivatives_taker_fee_bps=0.0,
            trade_cost_spot_spread_bps=0.0,
            trade_cost_margin_spread_bps=0.0,
            trade_cost_derivatives_spread_bps=0.0,
            trade_cost_spot_slippage_bps=0.0,
            trade_cost_margin_slippage_bps=0.0,
            trade_cost_derivatives_slippage_bps=0.0,
        )
        frozen_now = datetime(2026, 3, 27, 9, 0, tzinfo=timezone.utc)
        with patch("aats.services.decision_engine.context_builder.utc_now", return_value=frozen_now):
            runtime = await build_runtime(settings)
            await runtime.market_gateway.run_local_publisher(
                symbol="BTC-USDT",
                iterations=1,
                interval_seconds=0.0,
            )
            await runtime.market_gateway.run_local_publisher(
                symbol=settings.default_symbol,
                iterations=2,
                interval_seconds=0.0,
            )
            await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

            app = self._app(runtime)
            with TestClient(app) as client:
                strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        predicted = payload["smart_arbitrage_cost_summary"]["predicted"]
        self.assertEqual(predicted["expected_funding_events"], 0)
        self.assertEqual(float(predicted["funding_cost_bps"]), 0.0)
        self.assertIn("funding_outside_projected_hold_window", predicted["cost_source_flags"])

    async def test_smart_arbitrage_runtime_cost_summary_prefers_exchange_funding_schedule(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_family_active="smart_arbitrage",
            smart_arbitrage_enabled=True,
            smart_arbitrage_basis_entry_bps=0.0,
            smart_arbitrage_estimated_cost_bps=0.0,
            smart_arbitrage_funding_cost_enabled=True,
            smart_arbitrage_funding_source_mode="configured",
            smart_arbitrage_estimated_funding_bps=2.0,
            smart_arbitrage_expected_hold_hours=6.0,
            smart_arbitrage_funding_interval_hours=8.0,
            trade_cost_spot_taker_fee_bps=0.0,
            trade_cost_margin_taker_fee_bps=0.0,
            trade_cost_derivatives_taker_fee_bps=0.0,
            trade_cost_spot_spread_bps=0.0,
            trade_cost_margin_spread_bps=0.0,
            trade_cost_derivatives_spread_bps=0.0,
            trade_cost_spot_slippage_bps=0.0,
            trade_cost_margin_slippage_bps=0.0,
            trade_cost_derivatives_slippage_bps=0.0,
        )
        frozen_now = datetime(2026, 3, 27, 9, 0, tzinfo=timezone.utc)
        with patch("aats.services.decision_engine.context_builder.utc_now", return_value=frozen_now):
            runtime = await build_runtime(settings)
            runtime.account_service.funding_schedule = lambda symbol=None: {  # type: ignore[attr-defined]
                "available": symbol == "BTC-USDT-SWAP",
                "symbol": symbol,
                "funding_time": frozen_now - timedelta(hours=1),
                "next_funding_time": frozen_now + timedelta(hours=3),
                "funding_interval_hours": 4.0,
                "updated_at": frozen_now,
                "source": "okx_public_funding_rate",
            }
            await runtime.market_gateway.run_local_publisher(
                symbol="BTC-USDT",
                iterations=1,
                interval_seconds=0.0,
            )
            await runtime.market_gateway.run_local_publisher(
                symbol=settings.default_symbol,
                iterations=2,
                interval_seconds=0.0,
            )
            await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

            app = self._app(runtime)
            with TestClient(app) as client:
                strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        predicted = payload["smart_arbitrage_cost_summary"]["predicted"]
        self.assertEqual(predicted["expected_funding_events"], 1)
        self.assertEqual(float(predicted["funding_cost_bps"]), 2.0)
        self.assertIn("funding_schedule_exchange_actual", predicted["cost_source_flags"])
        self.assertNotIn("funding_schedule_projected_from_config", predicted["cost_source_flags"])

    async def test_strategy_runtime_snapshot_includes_registered_family_skeletons(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_family_active="directional",
            strategy_family_auto_selection_enabled=False,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        candidates = {item["family"]: item for item in payload["latest_snapshot"]["candidates"]}
        self.assertEqual(candidates["protective"]["state"], "disabled")
        self.assertEqual(candidates["opportunistic"]["state"], "disabled")
        self.assertEqual(candidates["independent"]["state"], "disabled")
        self.assertIn("strategy_family_protective_disabled", candidates["protective"]["reason_codes"])
        self.assertIn("strategy_family_opportunistic_disabled", candidates["opportunistic"]["reason_codes"])
        self.assertIn("strategy_family_independent_disabled", candidates["independent"]["reason_codes"])

    async def test_strategy_runtime_snapshot_surfaces_real_protective_family_candidate_when_enabled(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            derivatives_position_mode="hedge",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_family_active="directional",
            strategy_family_auto_selection_enabled=False,
            strategy_hedge_overlay_enabled=True,
            strategy_hedge_overlay_mode="protective",
            strategy_hedge_protective_enabled=True,
            strategy_family_protective_enabled=True,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        candidate = next(item for item in payload["latest_snapshot"]["candidates"] if item["family"] == "protective")
        self.assertNotEqual(candidate["state"], "disabled")
        self.assertEqual(candidate["execution_mode"], "protective_overlay")
        self.assertNotIn("strategy_family_protective_disabled", candidate["reason_codes"])
        self.assertNotIn("strategy_family_protective_placeholder_not_migrated", candidate["reason_codes"])
        self.assertFalse(bool(candidate["metrics"].get("skeleton_mode")))

    async def test_strategy_runtime_snapshot_surfaces_real_opportunistic_family_candidate_when_enabled(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            derivatives_position_mode="hedge",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_family_active="directional",
            strategy_family_auto_selection_enabled=False,
            strategy_hedge_overlay_enabled=True,
            strategy_hedge_overlay_mode="opportunistic",
            strategy_hedge_opportunistic_enabled=True,
            strategy_family_opportunistic_enabled=True,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        candidate = next(item for item in payload["latest_snapshot"]["candidates"] if item["family"] == "opportunistic")
        self.assertNotEqual(candidate["state"], "disabled")
        self.assertEqual(candidate["execution_mode"], "opportunistic_overlay")
        self.assertNotIn("strategy_family_opportunistic_disabled", candidate["reason_codes"])
        self.assertNotIn("strategy_family_opportunistic_placeholder_not_migrated", candidate["reason_codes"])
        self.assertFalse(bool(candidate["metrics"].get("skeleton_mode")))

    async def test_strategy_runtime_snapshot_surfaces_real_independent_family_candidate_when_enabled(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            derivatives_position_mode="hedge",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
            strategy_family_active="directional",
            strategy_family_auto_selection_enabled=False,
            strategy_short_bias_enabled=True,
            strategy_hedge_overlay_enabled=True,
            strategy_hedge_overlay_mode="independent",
            strategy_hedge_independent_enabled=True,
            strategy_hedge_independent_long_entry_threshold=0.60,
            strategy_hedge_independent_short_entry_threshold=0.60,
            strategy_hedge_independent_long_close_threshold=0.48,
            strategy_hedge_independent_short_close_threshold=0.48,
            strategy_family_independent_enabled=True,
        )
        runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        candidate = next(item for item in payload["latest_snapshot"]["candidates"] if item["family"] == "independent")
        self.assertIn("latest_selected_family_action", payload["summary"])
        self.assertNotEqual(candidate["state"], "disabled")
        self.assertEqual(candidate["execution_mode"], "independent_books")
        self.assertNotIn("strategy_family_independent_disabled", candidate["reason_codes"])
        self.assertNotIn("strategy_family_independent_placeholder_not_migrated", candidate["reason_codes"])
        self.assertFalse(bool(candidate["metrics"].get("skeleton_mode")))
        self.assertEqual(candidate["book_expectancy_summary"]["source"], "independent_book")
        self.assertEqual(
            [item["leg"] for item in candidate["book_expectancy_summary"]["books"]],
            ["long", "short"],
        )
        self.assertEqual(
            candidate["book_expectancy_summary"]["books"][0]["policy_reason"],
            candidate["metrics"].get("long_execution_policy_reason"),
        )
        self.assertEqual(
            candidate["book_expectancy_summary"]["books"][0]["execution_policy_urgency"],
            candidate["metrics"].get("long_execution_policy_urgency"),
        )
        self.assertEqual(
            [item["leg"] for item in candidate["book_runtime_states"]],
            ["long", "short"],
        )
        self.assertEqual(
            candidate["book_runtime_states"][0]["book_action"],
            candidate["metrics"].get("long_book_action"),
        )
        self.assertEqual(
            candidate["book_runtime_states"][0]["leg_health_summary"]["health_state"],
            candidate["metrics"].get("long_execution_health_state"),
        )
        self.assertEqual(
            candidate["book_runtime_states"][0]["threshold_snapshot"]["entry_threshold"],
            candidate["metrics"]["long_threshold_snapshot"]["entry_threshold"],
        )
        self.assertIn("adaptive_entry_threshold", candidate["book_runtime_states"][0]["threshold_snapshot"])
        self.assertIn("capital_multiplier", candidate["book_runtime_states"][0]["threshold_snapshot"])
        self.assertIn("reason_codes", candidate["book_runtime_states"][0]["threshold_snapshot"])
        self.assertIn(candidate["metrics"]["family_health_overall_state"], {"ok", "degraded", "blocked"})
        for state in candidate["book_runtime_states"]:
            if state["book_action"] in {"inactive", "hold", "blocked"}:
                self.assertIsNone(state["execution_chain_id"])
            else:
                self.assertTrue(state["execution_chain_id"])

    async def test_independent_runtime_exposes_expected_vs_realized_diagnostics_summary(self) -> None:
        settings = self._settings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime = await build_runtime(settings)
        now = datetime.now(timezone.utc)
        decision_id = "decision_independent_expected_vs_realized_runtime"
        decision_context = DecisionContext(
            decision_id=decision_id,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            as_of_ts=now,
            market_snapshot_ref="evt_market_snapshot_independent_runtime_diagnostics",
            feature_snapshot_ref="evt_feature_snapshot_independent_runtime_diagnostics",
            portfolio_snapshot_ref="evt_portfolio_snapshot_independent_runtime_diagnostics",
            health_snapshot_ref="evt_health_snapshot_independent_runtime_diagnostics",
            mode="paper_live",
            current_position_qty=Decimal("0.01"),
            product_type="derivatives",
            margin_mode="cross",
            current_exposure_side="flat",
        )
        family_execution_summary = {
            "summary_mode": "multi_leg",
            "family": "independent",
            "route_action": "override_target",
            "family_action": "rebalance_independent_books",
            "leg_count": 2,
            "position_intents": ["open_long", "close_short"],
            "directions": ["long", "short"],
            "leg_actions": ["open", "close"],
            "execution_modes": ["independent_long_book", "independent_short_book"],
            "book_runtime_states": [
                {
                    "leg": "long",
                    "current_qty": "0",
                    "target_qty": "0.01",
                    "state": "opening",
                    "book_action": "open",
                    "policy_reason": "independent_entry_strong_edge_aggressive",
                },
                {
                    "leg": "short",
                    "current_qty": "0.01",
                    "target_qty": "0",
                    "state": "closing",
                    "book_action": "close_stale_thesis",
                    "close_reason": "stale_thesis",
                    "policy_reason": "independent_stale_thesis_passive_exit",
                },
            ],
            "book_expectancy_summary": {
                "source": "independent_book",
                "books": [
                    {
                        "leg": "long",
                        "expected_gross_edge_bps": 12.0,
                        "expected_signal_edge_bps": 12.0,
                        "expected_slippage_bps": 1.0,
                        "expected_cost_bps": 4.0,
                        "expected_net_edge_bps": 8.0,
                        "passive_first_required": True,
                        "weak_edge_report_only": False,
                    },
                    {
                        "leg": "short",
                        "expected_gross_edge_bps": 3.0,
                        "expected_signal_edge_bps": 3.0,
                        "expected_slippage_bps": 1.0,
                        "expected_cost_bps": 2.0,
                        "expected_net_edge_bps": 1.0,
                        "close_reason": "stale_thesis",
                    },
                ],
            },
        }
        decision_outcome = DecisionOutcome(
            decision_id=decision_id,
            symbol="BTC-USDT-SWAP",
            decision_source="baseline",
            decision_authority="reference_only",
            finalized=True,
            final_direction="flat",
            final_action="exit",
            final_target_qty=Decimal("0"),
            selected_strategy_family="independent",
            selected_strategy_family_action="rebalance_independent_books",
            selected_strategy_route_action="override_target",
            family_execution_summary=family_execution_summary,
        )
        position_target = PositionTarget(
            decision_id=decision_id,
            symbol="BTC-USDT-SWAP",
            current_position_qty=Decimal("0.01"),
            target_position_qty=Decimal("0.01"),
            delta_position_qty=Decimal("0"),
            current_notional=Decimal("100"),
            target_notional=Decimal("100"),
            rebalance_reason="independent_expected_vs_realized_runtime_surface",
            urgency="medium",
            max_slippage_tolerance_bps=25,
            source_mix={"independent": 1.0},
            decision_expiry_ts=now + timedelta(minutes=5),
            product_type="derivatives",
            current_exposure_side="flat",
            target_exposure_side="flat",
            position_intent="hold",
            target_leverage=2.0,
            margin_mode="cross",
            strategy_family="independent",
            strategy_family_action="rebalance_independent_books",
            strategy_route_action="override_target",
            family_execution_summary=family_execution_summary,
            decision_outcome=decision_outcome,
        )
        context_event = build_envelope(
            topic=topics.DECISION_CONTEXTS,
            key="BTC-USDT-SWAP",
            payload_model=decision_context,
            source_component="test",
        )
        target_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT-SWAP",
            payload_model=position_target,
            source_component="test",
        )
        runtime.event_store.append(context_event)
        runtime.event_store.append(target_event)
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id=decision_id,
                decision_context_ref=context_event.event_id,
                position_target_ref=target_event.event_id,
            )
        )
        runtime.fill_outcome_repo.save_outcome(
            FillOutcomeRecord(
                fill_id="fill_independent_runtime_long",
                decision_id=decision_id,
                order_id="order_independent_runtime_long",
                symbol="BTC-USDT-SWAP",
                venue="PAPER",
                side="buy",
                fill_qty=Decimal("1"),
                fill_price=Decimal("100"),
                fill_notional=Decimal("100"),
                fee_amount=Decimal("0.10"),
                fee_currency="USDT",
                liquidity_role="maker",
                exchange_timestamp=now,
                ingestion_timestamp=now,
                order_status_after_fill="FILLED",
                strategy_family="independent",
                strategy_sleeve_id="sleeve_independent",
                allocation_id="alloc_independent",
                strategy_bundle_id="bundle_independent",
                strategy_leg_role="primary",
                target_leverage=2.0,
                exposure_side="long",
                execution_action="open_long",
                position_intent="open_long",
                position_mode="long_short_mode",
                pos_side="long",
                instrument_family="BTC-USDT",
                settle_currency="USDT",
                starting_position_qty=Decimal("0"),
                ending_position_qty=Decimal("1"),
                realized_pnl_delta=Decimal("1.90"),
                fee_delta=Decimal("-0.10"),
                product_type="derivatives",
                margin_mode="cross",
                created_at=now,
            )
        )
        runtime.fill_outcome_repo.save_outcome(
            FillOutcomeRecord(
                fill_id="fill_independent_runtime_short",
                decision_id=decision_id,
                order_id="order_independent_runtime_short",
                symbol="BTC-USDT-SWAP",
                venue="PAPER",
                side="buy",
                fill_qty=Decimal("1"),
                fill_price=Decimal("100"),
                fill_notional=Decimal("100"),
                fee_amount=Decimal("0.05"),
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=now + timedelta(seconds=5),
                ingestion_timestamp=now + timedelta(seconds=5),
                order_status_after_fill="FILLED",
                strategy_family="independent",
                strategy_sleeve_id="sleeve_independent",
                allocation_id="alloc_independent",
                strategy_bundle_id="bundle_independent",
                strategy_leg_role="hedge",
                target_leverage=2.0,
                exposure_side="short",
                execution_action="close_short",
                position_intent="close_short",
                position_mode="long_short_mode",
                pos_side="short",
                instrument_family="BTC-USDT",
                settle_currency="USDT",
                starting_position_qty=Decimal("1"),
                ending_position_qty=Decimal("0"),
                realized_pnl_delta=Decimal("0.45"),
                fee_delta=Decimal("-0.05"),
                product_type="derivatives",
                margin_mode="cross",
                created_at=now + timedelta(seconds=5),
            )
        )

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        directional = payload["configured_parameters"]["directional"]
        self.assertTrue(directional["hedge_independent_emit_book_level_metrics"])
        self.assertTrue(directional["hedge_independent_emit_expected_vs_realized_metrics"])
        self.assertTrue(directional["hedge_independent_emit_close_reason_metrics"])
        self.assertTrue(directional["hedge_independent_emit_execution_policy_metrics"])
        diagnostics = payload["independent_expected_vs_realized_summary"]
        self.assertIsNotNone(diagnostics)
        self.assertEqual(diagnostics["family"], "independent")
        self.assertEqual(diagnostics["sample_count"], 2)
        self.assertEqual(diagnostics["entry_count"], 1)
        self.assertEqual(diagnostics["close_count"], 1)
        self.assertEqual(diagnostics["weak_edge_entry_count"], 0)
        self.assertEqual(diagnostics["close_reason_distribution"][0]["reason"], "stale_thesis")
        self.assertEqual(len(diagnostics["book_breakdown"]), 2)
        self.assertIsNotNone(diagnostics["attempt_diagnostics"])
        self.assertIn("attempt_count", diagnostics["attempt_diagnostics"])
        self.assertEqual(diagnostics["book_breakdown"][0]["leg"], "long")
        self.assertIsNotNone(diagnostics["avg_expected_net_edge_bps"])
        self.assertIsNotNone(diagnostics["avg_realized_net_bps"])
        self.assertIsNotNone(diagnostics["passive_first_usage_ratio"])
        self.assertEqual(payload["summary"]["latest_independent_expected_vs_realized_sample_count"], 2)
        self.assertIsNotNone(payload["summary"]["latest_independent_expected_vs_realized_net_bps"])
        self.assertIsNotNone(payload["latest_applied_target"]["independent_expected_vs_realized_summary"])
        self.assertEqual(payload["latest_applied_target"]["book_expectancy_summary"]["source"], "independent_book")
        self.assertEqual(
            [item["leg"] for item in payload["latest_applied_target"]["book_runtime_states"]],
            ["long", "short"],
        )
        self.assertTrue(payload["latest_applied_target"]["diagnostic_metric_flags"]["emit_expected_vs_realized_metrics"])
        self.assertEqual(
            payload["latest_applied_target"]["independent_expected_vs_realized_summary"]["close_reason_distribution"][0]["reason"],
            "stale_thesis",
        )

    @staticmethod
    def _settings(**overrides) -> AATSSettings:
        return AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                **overrides,
            }
        )

    @staticmethod
    def _app(runtime) -> FastAPI:
        app = FastAPI()
        app.include_router(router)
        app.state.runtime = runtime
        return app


if __name__ == "__main__":
    unittest.main()
