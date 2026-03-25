from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events import topics


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
        self.assertEqual(strategy_payload["summary"]["latest_allocator_version"], "task76_allocator_v2_phase1")
        self.assertGreaterEqual(strategy_payload["summary"]["latest_budget_profile_count"], 1)
        self.assertGreaterEqual(strategy_payload["summary"]["latest_budget_assignment_count"], 1)
        self.assertGreaterEqual(strategy_payload["summary"]["latest_budget_snapshot_count"], 1)
        self.assertEqual(strategy_payload["latest_applied_target"]["strategy_family"], "spot_grid")
        self.assertIsNotNone(strategy_payload["latest_applied_target"]["strategy_sleeve_id"])
        self.assertIsNotNone(strategy_payload["latest_applied_target"]["allocation_id"])
        self.assertEqual(strategy_payload["latest_applied_target"]["strategy_route_action"], "override_target")
        self.assertTrue(strategy_payload["strategy_sleeves"])
        self.assertTrue(strategy_payload["recent_budget_profiles"])
        self.assertTrue(strategy_payload["recent_budget_assignments"])
        self.assertTrue(strategy_payload["recent_budget_snapshots"])
        spot_grid_candidate = next(
            item for item in strategy_payload["latest_snapshot"]["candidates"] if item["family"] == "spot_grid"
        )
        self.assertIn("current_sleeve_position_qty", spot_grid_candidate["metrics"])
        self.assertIn("target_account_position_qty", spot_grid_candidate["metrics"])
        self.assertEqual(system_payload["strategy_family_active"], "spot_grid")
        self.assertEqual(
            system_payload["strategy_runtime_summary"]["configured_active_family"],
            "spot_grid",
        )

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
        self.assertEqual(payload["summary"]["latest_allocator_version"], "task76_allocator_v2_phase1")
        self.assertGreaterEqual(payload["summary"]["latest_budget_profile_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_budget_assignment_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_budget_snapshot_count"], 1)
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
        self.assertEqual(dca_control["automation_state"], "active")
        self.assertEqual(
            payload["configured_parameters"]["dca"]["quote_budget_per_cycle"],
            100.0,
        )

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
        self.assertEqual(payload["summary"]["latest_allocator_version"], "task76_allocator_v2_phase1")
        self.assertGreaterEqual(payload["summary"]["latest_budget_profile_count"], 2)
        self.assertGreaterEqual(payload["summary"]["latest_budget_assignment_count"], 2)
        self.assertGreaterEqual(payload["summary"]["latest_budget_snapshot_count"], 2)
        self.assertIn(
            payload["summary"]["latest_portfolio_risk_budget_state"],
            {"normal", "contracted", "hedge_protected"},
        )
        self.assertEqual(payload["latest_allocation_decision"]["approved_families"], ["spot_grid", "dca"])
        self.assertTrue(payload["latest_allocation_decision"]["approved_sleeve_weights"])
        self.assertEqual(payload["latest_allocation_decision"]["allocator_version"], "task76_allocator_v2_phase1")
        self.assertTrue(payload["recent_budget_profiles"])
        self.assertTrue(payload["recent_budget_assignments"])
        self.assertTrue(payload["recent_budget_snapshots"])
        self.assertIsInstance(payload["recent_conflict_resolutions"], list)
        self.assertTrue(payload["recent_netting_decisions"])
        self.assertGreaterEqual(len(payload["recent_sleeve_intents"]), 4)
        self.assertEqual(len(payload["latest_bundle"]["legs"]), 2)
        self.assertEqual(set(payload["latest_bundle"]["participating_families"]), {"spot_grid", "dca"})

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
            iterations=2,
            interval_seconds=0.0,
        )
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)

        self.assertEqual(target.strategy_family, "smart_arbitrage")
        self.assertEqual(target.strategy_route_action, "override_target")
        self.assertIsNotNone(target.strategy_bundle_id)
        self.assertEqual(len(target.strategy_execution_legs), 2)

        app = self._app(runtime)
        with TestClient(app) as client:
            strategy_runtime = client.get("/strategy/runtime")

        self.assertEqual(strategy_runtime.status_code, 200)
        payload = strategy_runtime.json()
        self.assertEqual(payload["summary"]["configured_active_family"], "smart_arbitrage")
        self.assertEqual(payload["summary"]["latest_selected_family"], "smart_arbitrage")
        self.assertIsNotNone(payload["summary"]["latest_selected_strategy_sleeve_id"])
        self.assertIsNotNone(payload["summary"]["latest_allocation_id"])
        self.assertEqual(payload["summary"]["latest_selected_route_action"], "override_target")
        self.assertEqual(payload["summary"]["latest_allocator_version"], "task76_allocator_v2_phase1")
        self.assertGreaterEqual(payload["summary"]["latest_budget_profile_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_budget_assignment_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_budget_snapshot_count"], 1)
        self.assertGreaterEqual(payload["summary"]["latest_netting_decision_count"], 1)
        self.assertTrue(payload["summary"]["automatic_selection_enabled"])
        self.assertTrue(payload["summary"]["auto_parallel_enabled"])
        self.assertEqual(payload["latest_bundle"]["status"], "submitted")
        self.assertEqual(
            payload["latest_bundle"]["strategy_sleeve_id"],
            payload["latest_applied_target"]["strategy_sleeve_id"],
        )
        self.assertEqual(
            payload["latest_bundle"]["allocation_id"],
            payload["latest_applied_target"]["allocation_id"],
        )
        smart_arbitrage_candidate = next(
            item for item in payload["latest_snapshot"]["candidates"] if item["family"] == "smart_arbitrage"
        )
        smart_arbitrage_control = next(
            item for item in payload["latest_snapshot"]["automation_decisions"] if item["family"] == "smart_arbitrage"
        )
        self.assertEqual(smart_arbitrage_candidate["route_action"], "override_target")
        self.assertEqual(len(smart_arbitrage_candidate["legs"]), 2)
        self.assertIn("current_sleeve_spot_qty", smart_arbitrage_candidate["metrics"])
        self.assertIn("target_account_derivatives_qty", smart_arbitrage_candidate["metrics"])
        self.assertEqual(smart_arbitrage_control["automation_state"], "active")
        self.assertIsNotNone(payload["summary"]["latest_hedge_protected_notional"])
        self.assertTrue(payload["recent_budget_profiles"])
        self.assertTrue(payload["recent_budget_assignments"])
        self.assertTrue(payload["recent_budget_snapshots"])
        self.assertTrue(payload["recent_netting_decisions"])

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
