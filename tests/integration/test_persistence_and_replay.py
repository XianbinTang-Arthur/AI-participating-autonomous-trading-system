from __future__ import annotations

import os
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from aats.bootstrap.config import build_runtime, build_storage_backends
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope, publish_model
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.schemas.decision import BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.exchange import AccountBaselineSnapshot
from aats.schemas.execution import ExecutionPlan, FillEvent, OrderIntent, OrderState
from aats.schemas.governance import PolicyDecision, RiskDecision
from aats.schemas.market import MarketSnapshot
from aats.schemas.operator import OperatorActionRecord
from aats.schemas.portfolio import FillOutcomeRecord, FundingFeeRecord
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.strategy_runtime import StrategySleeveRecord
from aats.schemas.system import HealthSnapshot
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.replay import ReplayEngine
from aats.services.runtime_scope import runtime_state_scope
from aats.storage.audit_repo import InMemoryAuditRepository
from aats.storage.event_store import InMemoryEventStore
from tests.support.postgres import temporary_postgres_url


class TestPersistenceAndReplay(unittest.IsolatedAsyncioTestCase):
    async def test_sqlalchemy_event_store_persists_and_queries_events(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            fresh_storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                audit_records = runtime.audit_repo.all()
                self.assertTrue(audit_records)

                event_store = runtime.event_store
                self.assertGreater(event_store.count(), 0)
                self.assertEqual(event_store.count(topic=topics.MARKET_SNAPSHOTS), 4)
                self.assertTrue(event_store.by_decision(audit_records[0].decision_id))

                fresh_storage = build_storage_backends(settings)
                self.assertEqual(fresh_storage.event_store.count(), event_store.count())
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if fresh_storage is not None and fresh_storage.database_runtime is not None:
                    fresh_storage.database_runtime.dispose()

    async def test_replay_reconstructs_same_portfolio_state(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                storage = build_storage_backends(settings)
                replay = ReplayEngine(
                    event_store=storage.event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                    audit_repo=storage.audit_repo,
                    portfolio_repo=storage.portfolio_repo,
                ).replay()

                self.assertEqual(replay.divergence_count, 0)
                self.assertEqual(replay.portfolio_issues, [])
                self.assertEqual(replay.decision_chain_issues, [])
                self.assertEqual(replay.execution_chain_issues, [])
                self.assertEqual(replay.audit_issues, [])
                self.assertIsNotNone(replay.final_reconstructed_snapshot)
                self.assertIsNotNone(replay.final_stored_snapshot)
                self.assertEqual(
                    self._snapshot_signature(replay.final_stored_snapshot),
                    self._snapshot_signature(runtime.portfolio_repo.latest()),
                )
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_postgres_event_store_archive_and_incremental_replay_persist_offset(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                storage = build_storage_backends(settings)
                scope = runtime_state_scope(settings)
                replay_engine = ReplayEngine(
                    event_store=storage.event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                    audit_repo=storage.audit_repo,
                    portfolio_repo=storage.portfolio_repo,
                    reconciliation_repo=storage.reconciliation_repo,
                    scope=scope,
                )
                first = replay_engine.replay()
                first_offset = storage.event_store.latest_replay_offset(
                    projection_key="portfolio_replay",
                    scope=scope,
                )
                self.assertIsNotNone(first_offset)
                self.assertEqual(first.replay_offset_id, first_offset.offset_id)

                latest_market_event = storage.event_store.latest_by_topic_scoped(
                    topic=topics.MARKET_SNAPSHOTS,
                    scope=scope,
                )
                self.assertIsNotNone(latest_market_event)
                archived = storage.event_store.archive_before(before_ts=latest_market_event.event_timestamp)
                archive_summary = storage.event_store.archive_summary()
                self.assertGreater(archived["archived_event_count"], 0)
                self.assertGreater(archive_summary["archive_event_count"], 0)
                self.assertGreater(archive_summary["replay_offset_count"], 0)

                second = replay_engine.replay()
                second_offset = storage.event_store.latest_replay_offset(
                    projection_key="portfolio_replay",
                    scope=scope,
                )
                self.assertEqual(second.divergence_count, 0)
                self.assertIsNotNone(second.incremental_window_start_at)
                self.assertIsNotNone(second_offset)
                self.assertEqual(second.replay_offset_id, second_offset.offset_id)
                self.assertEqual(second_offset.last_event_id, first_offset.last_event_id)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_smart_arbitrage_postgres_replay_keeps_bundle_and_dual_leg_chain_consistent(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(
                database_url,
                trading_product_type="derivatives",
                margin_mode="cross",
                default_symbol="BTC-USDT-SWAP",
                allowed_symbols=("BTC-USDT-SWAP",),
                smart_arbitrage_enabled=True,
                smart_arbitrage_basis_entry_bps=0.0,
                smart_arbitrage_estimated_cost_bps=0.0,
                smart_arbitrage_quote_budget_per_trade=100.0,
                smart_arbitrage_max_pair_notional=100.0,
                account_backend="disabled",
                account_read_enabled=False,
                execution_backend="paper",
                market_data_backend="demo",
            )
            runtime = await build_runtime(settings)
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol="BTC-USDT",
                    iterations=3,
                    interval_seconds=0.0,
                )
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=3,
                    interval_seconds=0.0,
                )

                target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)
                self.assertEqual(target.strategy_family, "smart_arbitrage")
                self.assertIsNotNone(target.strategy_sleeve_id)
                self.assertIsNotNone(target.allocation_id)
                self.assertIsNotNone(target.strategy_bundle_id)
                self.assertEqual(len(target.strategy_execution_legs), 2)

                storage = build_storage_backends(settings)
                bundle_record = next(
                    record
                    for record in storage.audit_repo.all()
                    if record.decision_id == target.decision_id
                    and record.strategy_execution_bundle_ref is not None
                    and len(record.execution_plan_refs) >= 2
                )
                replay = ReplayEngine(
                    event_store=storage.event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                    audit_repo=storage.audit_repo,
                    portfolio_repo=storage.portfolio_repo,
                ).replay(decision_id=bundle_record.decision_id)

                self.assertEqual(replay.divergence_count, 0)
                self.assertEqual(replay.portfolio_issues, [])
                self.assertEqual(replay.decision_chain_issues, [])
                self.assertEqual(replay.execution_chain_issues, [])
                self.assertEqual(replay.audit_issues, [])
                self.assertEqual(bundle_record.selected_strategy_sleeve_id, target.strategy_sleeve_id)
                self.assertEqual(bundle_record.allocation_id, target.allocation_id)
                self.assertGreaterEqual(len(bundle_record.execution_plan_refs), 2)
                self.assertGreaterEqual(len(bundle_record.order_intent_refs), 2)
                self.assertGreaterEqual(len(bundle_record.fill_event_refs), 2)
                self.assertIsNotNone(storage.event_store.get(bundle_record.strategy_execution_bundle_ref))
                self.assertTrue(
                    any(
                        sleeve.sleeve_id == target.strategy_sleeve_id
                        for sleeve in storage.strategy_sleeve_repo.list_sleeves()
                    )
                )
                runtime_bundles = storage.strategy_runtime_repo.recent_execution_bundles(
                    product_type="derivatives",
                    margin_mode="cross",
                    symbol="BTC-USDT-SWAP",
                    limit=5,
                )
                self.assertTrue(runtime_bundles)
                self.assertEqual(runtime_bundles[0].allocation_id, target.allocation_id)
                self.assertEqual(runtime_bundles[0].bundle_type, "hedge_protected")
                self.assertIsNotNone(runtime_bundles[0].allocation_snapshot_ref)
                runtime_allocation = storage.strategy_runtime_repo.latest_allocation_decision(
                    product_type="derivatives",
                    margin_mode="cross",
                    symbol="BTC-USDT-SWAP",
                )
                self.assertIsNotNone(runtime_allocation)
                self.assertEqual(runtime_allocation.allocation_id, target.allocation_id)
                self.assertEqual(runtime_allocation.allocator_version, "task74_allocator_v2_phase2")
                self.assertEqual(
                    runtime_bundles[0].gross_requested_exposure,
                    runtime_allocation.portfolio_requested_notional,
                )
                self.assertEqual(
                    runtime_bundles[0].net_approved_exposure,
                    runtime_allocation.portfolio_approved_notional,
                )
                self.assertEqual(
                    set(runtime_bundles[0].budget_snapshot_ids),
                    set(runtime_allocation.budget_snapshot_ids),
                )
                runtime_intents = storage.strategy_runtime_repo.list_sleeve_intents(
                    product_type="derivatives",
                    margin_mode="cross",
                    limit=10,
                )
                self.assertGreaterEqual(len(runtime_intents), 4)
                self.assertIsNotNone(storage.position_lot_repo)
                if storage.position_lot_repo is not None:
                    spot_lots = storage.position_lot_repo.lots_for_scope(
                        symbol="BTC-USDT",
                        product_type="spot",
                        margin_mode="cash",
                    )
                    derivatives_lots = storage.position_lot_repo.lots_for_scope(
                        symbol="BTC-USDT-SWAP",
                        product_type="derivatives",
                        margin_mode="cross",
                    )
                    self.assertTrue(spot_lots)
                    self.assertTrue(derivatives_lots)
                    self.assertTrue(
                        any(lot["strategy_sleeve_id"] == target.strategy_sleeve_id for lot in spot_lots)
                    )
                    self.assertTrue(
                        any(lot["strategy_sleeve_id"] == target.strategy_sleeve_id for lot in derivatives_lots)
                    )
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_high_frequency_fill_projection_keeps_snapshot_lots_sleeve_pnl_and_replay_aligned(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            storage = None
            try:
                scope = runtime_state_scope(settings)
                base_ts = utc_now()
                shared_ingestion = base_ts + timedelta(seconds=5)
                fills = [
                    FillEvent(
                        fill_id="z_highfreq_buy_100",
                        decision_id="decision_highfreq_fill",
                        intent_id="intent_highfreq_fill",
                        client_order_id="clord_highfreq_fill",
                        exchange_order_id="ord_highfreq_fill",
                        symbol=settings.default_symbol,
                        venue="PAPER",
                        side="buy",
                        fill_qty=Decimal("1"),
                        fill_price=Decimal("100"),
                        fee_amount=Decimal("0"),
                        fee_currency="USDT",
                        liquidity_role="taker",
                        exchange_timestamp=base_ts,
                        ingestion_timestamp=shared_ingestion,
                        order_status_after_fill="FILLED",
                        strategy_family="directional",
                        strategy_sleeve_id="sleeve_highfreq",
                        allocation_id="alloc_highfreq",
                    ),
                    FillEvent(
                        fill_id="y_highfreq_buy_120",
                        decision_id="decision_highfreq_fill",
                        intent_id="intent_highfreq_fill",
                        client_order_id="clord_highfreq_fill",
                        exchange_order_id="ord_highfreq_fill",
                        symbol=settings.default_symbol,
                        venue="PAPER",
                        side="buy",
                        fill_qty=Decimal("1"),
                        fill_price=Decimal("120"),
                        fee_amount=Decimal("0"),
                        fee_currency="USDT",
                        liquidity_role="taker",
                        exchange_timestamp=base_ts + timedelta(milliseconds=1),
                        ingestion_timestamp=shared_ingestion,
                        order_status_after_fill="FILLED",
                        strategy_family="directional",
                        strategy_sleeve_id="sleeve_highfreq",
                        allocation_id="alloc_highfreq",
                    ),
                    FillEvent(
                        fill_id="x_highfreq_sell_110",
                        decision_id="decision_highfreq_fill",
                        intent_id="intent_highfreq_fill",
                        client_order_id="clord_highfreq_fill",
                        exchange_order_id="ord_highfreq_fill",
                        symbol=settings.default_symbol,
                        venue="PAPER",
                        side="sell",
                        fill_qty=Decimal("1"),
                        fill_price=Decimal("110"),
                        fee_amount=Decimal("0"),
                        fee_currency="USDT",
                        liquidity_role="taker",
                        exchange_timestamp=base_ts + timedelta(milliseconds=2),
                        ingestion_timestamp=shared_ingestion,
                        order_status_after_fill="FILLED",
                        strategy_family="directional",
                        strategy_sleeve_id="sleeve_highfreq",
                        allocation_id="alloc_highfreq",
                    ),
                ]

                for fill in fills:
                    self.assertTrue(runtime.execution_repo.save_fill(fill))
                    await runtime.portfolio_service.handle_fill_event(
                        {
                            "topic": topics.FILL_EVENTS,
                            "key": fill.symbol,
                            "payload": build_envelope(
                                topic=topics.FILL_EVENTS,
                                key=fill.symbol,
                                payload_model=fill,
                                source_component="test",
                            ).model_dump(mode="json"),
                        }
                    )

                latest_snapshot = runtime.portfolio_repo.latest()
                self.assertIsNotNone(latest_snapshot)
                assert latest_snapshot is not None
                self.assertEqual(latest_snapshot.positions[0].position_qty, Decimal("1"))
                self.assertEqual(latest_snapshot.positions[0].avg_entry_price, Decimal("110"))
                self.assertEqual(latest_snapshot.realized_pnl, Decimal("0"))

                storage = build_storage_backends(settings)
                self.assertIsNotNone(storage.position_lot_repo)
                assert storage.position_lot_repo is not None
                open_lots = storage.position_lot_repo.lots_for_scope(
                    symbol=settings.default_symbol,
                    product_type="spot",
                    margin_mode="cash",
                    open_only=True,
                )
                self.assertEqual(len(open_lots), 1)
                self.assertEqual(open_lots[0]["signed_quantity_open"], Decimal("1"))
                self.assertEqual(open_lots[0]["entry_price"], Decimal("120"))
                self.assertEqual(open_lots[0]["strategy_sleeve_id"], "sleeve_highfreq")
                close_events = storage.lot_event_repo.events_for_fill("x_highfreq_sell_110")
                self.assertEqual(len(close_events), 1)
                self.assertEqual(close_events[0]["realized_pnl_delta"], Decimal("10"))

                outcomes = storage.fill_outcome_repo.outcomes_for_scope(scope=scope)
                self.assertEqual(len(outcomes), 3)
                sell_outcome = next(item for item in outcomes if item.fill_id == "x_highfreq_sell_110")
                self.assertEqual(sell_outcome.realized_pnl_delta, Decimal("0"))

                sleeve_records = storage.sleeve_pnl_repo.records_for_scope(scope=scope)
                self.assertEqual(len(sleeve_records), 3)
                self.assertEqual(
                    sum((Decimal(str(item.realized_pnl)) for item in sleeve_records), start=Decimal("0")),
                    Decimal("0"),
                )

                replay = ReplayEngine(
                    event_store=runtime.event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                    audit_repo=storage.audit_repo,
                    portfolio_repo=storage.portfolio_repo,
                    fill_outcome_repo=storage.fill_outcome_repo,
                    funding_fee_repo=storage.funding_fee_repo,
                    sleeve_pnl_repo=storage.sleeve_pnl_repo,
                    scope=scope,
                )
                reconstructed = replay._rebuild_snapshot(
                    fills=fills,
                    baseline_snapshot=None,
                    price_provider=lambda _symbol: Decimal("0"),
                )

                self.assertEqual(reconstructed.balances, latest_snapshot.balances)
                self.assertEqual(
                    {position.symbol: position.position_qty for position in reconstructed.positions},
                    {position.symbol: position.position_qty for position in latest_snapshot.positions},
                )
                self.assertEqual(reconstructed.cost_basis, latest_snapshot.cost_basis)
                self.assertEqual(reconstructed.realized_pnl, latest_snapshot.realized_pnl)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_allocator_postgres_replay_keeps_multi_sleeve_spot_bundle_consistent(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(
                database_url,
                trading_product_type="spot",
                margin_mode="cash",
                default_symbol="BTC-USDT",
                allowed_symbols=("BTC-USDT",),
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
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                target = await runtime.decision_engine.run_cycle(settings.default_symbol, settings.primary_timeframe)
                self.assertEqual(target.strategy_family, "spot_grid")
                self.assertEqual(len(target.strategy_execution_legs), 2)

                storage = build_storage_backends(settings)
                bundle_record = next(
                    record
                    for record in storage.audit_repo.all()
                    if record.decision_id == target.decision_id
                    and record.strategy_execution_bundle_ref is not None
                    and len(record.order_intent_refs) >= 2
                )
                replay = ReplayEngine(
                    event_store=storage.event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                    audit_repo=storage.audit_repo,
                    portfolio_repo=storage.portfolio_repo,
                ).replay(decision_id=bundle_record.decision_id)

                self.assertEqual(replay.divergence_count, 0)
                self.assertEqual(replay.portfolio_issues, [])
                self.assertEqual(replay.decision_chain_issues, [])
                self.assertEqual(replay.execution_chain_issues, [])
                self.assertEqual(replay.audit_issues, [])
                self.assertIsNotNone(bundle_record.strategy_coordinator_snapshot_ref)
                self.assertIsNotNone(bundle_record.portfolio_allocation_decision_ref)
                self.assertGreaterEqual(len(bundle_record.strategy_sleeve_intent_refs), 4)
                runtime_allocation = storage.strategy_runtime_repo.latest_allocation_decision(
                    product_type="spot",
                    margin_mode="cash",
                    symbol="BTC-USDT",
                )
                self.assertIsNotNone(runtime_allocation)
                self.assertEqual(runtime_allocation.allocator_version, "task74_allocator_v2_phase2")
                runtime_bundles = storage.strategy_runtime_repo.recent_execution_bundles(
                    product_type="spot",
                    margin_mode="cash",
                    symbol="BTC-USDT",
                    limit=5,
                )
                self.assertTrue(runtime_bundles)
                self.assertEqual(runtime_bundles[0].bundle_type, "multi_sleeve")
                self.assertIsNotNone(runtime_bundles[0].allocation_snapshot_ref)
                self.assertEqual(
                    runtime_bundles[0].gross_requested_exposure,
                    runtime_allocation.portfolio_requested_notional,
                )
                self.assertEqual(
                    runtime_bundles[0].net_approved_exposure,
                    runtime_allocation.portfolio_approved_notional,
                )
                self.assertEqual(
                    set(runtime_bundles[0].budget_snapshot_ids),
                    set(runtime_allocation.budget_snapshot_ids),
                )
                runtime_intents = storage.strategy_runtime_repo.list_sleeve_intents(
                    product_type="spot",
                    margin_mode="cash",
                    limit=10,
                )
                self.assertGreaterEqual(len(runtime_intents), 4)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_postgres_replay_validates_sleeve_pnl_projection_against_fill_and_funding_truth(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(
                database_url,
                trading_product_type="derivatives",
                margin_mode="cross",
                default_symbol="BTC-USDT-SWAP",
                allowed_symbols=("BTC-USDT-SWAP",),
                account_backend="disabled",
                account_read_enabled=False,
                execution_backend="paper",
                market_data_backend="demo",
            )
            runtime = await build_runtime(settings)
            storage = None
            try:
                now = utc_now()
                scope = runtime_state_scope(settings)
                runtime.strategy_sleeve_repo.save_sleeve(
                    StrategySleeveRecord(
                        sleeve_id="directional_btc_core",
                        family="directional",
                        name="directional_btc_core",
                        product_scope="derivatives",
                        margin_scope="cross",
                        symbol_scope=("BTC-USDT-SWAP",),
                    )
                )
                runtime.execution_repo.save_fill(
                    FillEvent(
                        fill_id="fill_sleeve_truth",
                        decision_id="decision_sleeve_truth",
                        intent_id="intent_sleeve_truth",
                        client_order_id="order_sleeve_truth",
                        exchange_order_id="venue_sleeve_truth",
                        symbol="BTC-USDT-SWAP",
                        venue="OKX",
                        side="buy",
                        fill_qty=Decimal("1"),
                        fill_price=Decimal("100"),
                        fee_amount=Decimal("0.10"),
                        fee_currency="USDT",
                        liquidity_role="taker",
                        exchange_timestamp=now - timedelta(minutes=8),
                        ingestion_timestamp=now - timedelta(minutes=8),
                        order_status_after_fill="FILLED",
                        strategy_family="directional",
                        strategy_sleeve_id="directional_btc_core",
                        allocation_id="alloc_directional_btc",
                        strategy_bundle_id="bundle_directional_btc",
                        strategy_leg_role="primary",
                        target_leverage=3.0,
                        exposure_side="long",
                        execution_action="enter",
                        position_intent="open_long",
                        product_type="derivatives",
                        margin_mode="cross",
                    )
                )
                runtime.fill_outcome_repo.save_outcome(
                    FillOutcomeRecord(
                        fill_id="fill_sleeve_truth",
                        decision_id="decision_sleeve_truth",
                        intent_id="intent_sleeve_truth",
                        order_id="order_sleeve_truth",
                        symbol="BTC-USDT-SWAP",
                        venue="OKX",
                        side="buy",
                        fill_qty=Decimal("1"),
                        fill_price=Decimal("100"),
                        fill_notional=Decimal("100"),
                        fee_amount=Decimal("0.10"),
                        fee_currency="USDT",
                        liquidity_role="taker",
                        exchange_timestamp=now - timedelta(minutes=8),
                        ingestion_timestamp=now - timedelta(minutes=8),
                        order_status_after_fill="FILLED",
                        strategy_family="directional",
                        strategy_sleeve_id="directional_btc_core",
                        allocation_id="alloc_directional_btc",
                        strategy_bundle_id="bundle_directional_btc",
                        strategy_leg_role="primary",
                        target_leverage=3.0,
                        exposure_side="long",
                        execution_action="open_long",
                        position_intent="open_long",
                        starting_position_qty=Decimal("0"),
                        ending_position_qty=Decimal("1"),
                        realized_pnl_delta=Decimal("0"),
                        fee_delta=Decimal("-0.10"),
                        product_type="derivatives",
                        margin_mode="cross",
                        created_at=now - timedelta(minutes=8),
                    )
                )
                runtime.funding_fee_repo.save_record(
                    FundingFeeRecord(
                        bill_id="bill_sleeve_truth",
                        symbol="BTC-USDT-SWAP",
                        currency="USDT",
                        amount=Decimal("-1.25"),
                        bill_type="8",
                        sub_type="173",
                        type_label="funding_fee",
                        sub_type_label="funding_fee_expense",
                        funding_direction="expense",
                        bill_ts=now - timedelta(minutes=2),
                        ledger_posting_state="POSTED",
                        product_type="derivatives",
                        margin_mode="cross",
                    )
                )

                runtime.sleeve_pnl_projection_service.rebuild_scope(scope=scope)
                storage = build_storage_backends(settings)
                records = storage.sleeve_pnl_repo.records_for_scope(scope=scope)
                self.assertEqual(len(records), 2)
                self.assertTrue(any(record.fill_id == "fill_sleeve_truth" for record in records))
                self.assertTrue(any(record.funding_fee_id == "bill_sleeve_truth" for record in records))

                replay = ReplayEngine(
                    event_store=storage.event_store,
                    reconstruction_service=PortfolioReconstructionService(
                        initial_usdt_balance=settings.initial_usdt_balance,
                        snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                    ),
                    audit_repo=storage.audit_repo,
                    portfolio_repo=storage.portfolio_repo,
                    fill_outcome_repo=storage.fill_outcome_repo,
                    funding_fee_repo=storage.funding_fee_repo,
                    sleeve_pnl_repo=storage.sleeve_pnl_repo,
                    scope=scope,
                ).replay()

                self.assertEqual(replay.execution_chain_issues, [])
                self.assertEqual(replay.divergence_count, 0)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_audit_records_reference_persisted_events(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = self._postgres_settings(database_url)
            runtime = await build_runtime(settings)
            storage = None
            try:
                await runtime.market_gateway.run_local_publisher(
                    symbol=settings.default_symbol,
                    iterations=4,
                    interval_seconds=0.0,
                )

                storage = build_storage_backends(settings)
                event_store = storage.event_store
                for record in storage.audit_repo.all():
                    context_event = event_store.get(record.decision_context_ref)
                    self.assertIsNotNone(context_event)
                    if context_event is not None:
                        health_snapshot_ref = context_event.payload.get("health_snapshot_ref")
                        self.assertIsInstance(health_snapshot_ref, str)
                        health_event = event_store.get(health_snapshot_ref)
                        self.assertIsNotNone(health_event)
                        if health_event is not None:
                            self.assertEqual(health_event.topic, topics.HEALTH_SNAPSHOTS)
                            self.assertEqual(health_event.payload.get("decision_id"), record.decision_id)
                    if record.baseline_assessment_ref is not None:
                        self.assertIsNotNone(event_store.get(record.baseline_assessment_ref))
                    if record.ai_decision_brief_ref is not None:
                        self.assertIsNotNone(event_store.get(record.ai_decision_brief_ref))
                    if record.ai_market_assessment_ref is not None:
                        self.assertIsNotNone(event_store.get(record.ai_market_assessment_ref))
                    if record.position_target_ref is not None:
                        self.assertIsNotNone(event_store.get(record.position_target_ref))
                    if record.strategy_coordinator_snapshot_ref is not None:
                        snapshot_event = event_store.get(record.strategy_coordinator_snapshot_ref)
                        self.assertIsNotNone(snapshot_event)
                        if snapshot_event is not None:
                            self.assertEqual(snapshot_event.topic, topics.STRATEGY_COORDINATOR_SNAPSHOTS)
                    if record.portfolio_allocation_decision_ref is not None:
                        allocation_event = event_store.get(record.portfolio_allocation_decision_ref)
                        self.assertIsNotNone(allocation_event)
                        if allocation_event is not None:
                            self.assertEqual(allocation_event.topic, topics.PORTFOLIO_ALLOCATION_DECISIONS)
                    if record.decision_outcome_ref is not None:
                        outcome_event = event_store.get(record.decision_outcome_ref)
                        self.assertIsNotNone(outcome_event)
                        if outcome_event is not None:
                            self.assertEqual(outcome_event.topic, topics.DECISION_OUTCOMES)
                            self.assertTrue(outcome_event.payload.get("finalized"))
                    if record.policy_decision_ref is not None:
                        self.assertIsNotNone(event_store.get(record.policy_decision_ref))
                    if record.risk_decision_ref is not None:
                        self.assertIsNotNone(event_store.get(record.risk_decision_ref))
                    if record.execution_plan_ref is not None:
                        execution_plan_event = event_store.get(record.execution_plan_ref)
                        self.assertIsNotNone(execution_plan_event)
                        if execution_plan_event is not None:
                            self.assertEqual(execution_plan_event.topic, topics.EXECUTION_PLANS)
                    for ref in record.execution_plan_refs:
                        execution_plan_event = event_store.get(ref)
                        self.assertIsNotNone(execution_plan_event)
                        if execution_plan_event is not None:
                            self.assertEqual(execution_plan_event.topic, topics.EXECUTION_PLANS)
                    if record.strategy_execution_bundle_ref is not None:
                        bundle_event = event_store.get(record.strategy_execution_bundle_ref)
                        self.assertIsNotNone(bundle_event)
                        if bundle_event is not None:
                            self.assertEqual(bundle_event.topic, topics.STRATEGY_EXECUTION_BUNDLES)
                    if record.portfolio_delta_ref is not None:
                        snapshot_event = event_store.get(record.portfolio_delta_ref)
                        self.assertIsNotNone(snapshot_event)
                        self.assertEqual(snapshot_event.payload.get("decision_id"), record.decision_id)
                    for ref in (
                        record.ai_shadow_decision_refs
                        + record.ai_shadow_evaluation_refs
                        + record.strategy_sleeve_intent_refs
                        + record.order_intent_refs
                        + record.order_state_refs
                        + record.fill_event_refs
                        + record.reconciliation_refs
                    ):
                        event = event_store.get(ref)
                        self.assertIsNotNone(event)
                        if event is not None and "decision_id" in event.payload:
                            self.assertEqual(event.payload.get("decision_id"), record.decision_id)
                    if record.order_intent_refs:
                        self.assertIsNotNone(record.execution_plan_ref)
                        self.assertTrue(record.order_state_refs)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
                if storage is not None and storage.database_runtime is not None:
                    storage.database_runtime.dispose()

    async def test_replay_can_filter_by_decision_id(self) -> None:
        settings = self._paper_runtime_settings()
        runtime = await build_runtime(settings)
        try:
            await runtime.market_gateway.run_local_publisher(
                symbol=settings.default_symbol,
                iterations=4,
                interval_seconds=0.0,
            )

            executed_record = next(record for record in runtime.audit_repo.all() if record.fill_event_refs)
            replay = ReplayEngine(
                event_store=runtime.event_store,
                reconstruction_service=PortfolioReconstructionService(
                    initial_usdt_balance=settings.initial_usdt_balance,
                    snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                ),
                audit_repo=runtime.audit_repo,
                portfolio_repo=runtime.portfolio_repo,
            ).replay(decision_id=executed_record.decision_id)

            self.assertEqual(replay.selected_decision_id, executed_record.decision_id)
            self.assertEqual(set(replay.decision_chains.keys()), {executed_record.decision_id})
            self.assertEqual(replay.decision_chain_issues, [])
            self.assertEqual(replay.audit_issues, [])
            self.assertGreater(replay.replayed_event_count, 0)
        finally:
            if runtime.database_runtime is not None:
                runtime.database_runtime.dispose()

    async def test_replay_can_filter_by_time_range(self) -> None:
        settings = self._paper_runtime_settings()
        runtime = await build_runtime(settings)
        try:
            await runtime.market_gateway.run_local_publisher(
                symbol=settings.default_symbol,
                iterations=2,
                interval_seconds=0.0,
            )

            decision_id = runtime.audit_repo.all()[0].decision_id
            decision_events = runtime.event_store.by_decision(decision_id)
            start_at = min(event.event_timestamp for event in decision_events) - timedelta(seconds=1)
            end_at = max(event.event_timestamp for event in decision_events) + timedelta(seconds=1)
            replay = ReplayEngine(
                event_store=runtime.event_store,
                reconstruction_service=PortfolioReconstructionService(
                    initial_usdt_balance=settings.initial_usdt_balance,
                    snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
                ),
                audit_repo=runtime.audit_repo,
                portfolio_repo=runtime.portfolio_repo,
            ).replay(start_at=start_at, end_at=end_at)

            self.assertEqual(replay.start_at, start_at)
            self.assertEqual(replay.end_at, end_at)
            self.assertGreater(replay.replayed_event_count, 0)
            self.assertIn(decision_id, replay.decision_chains)
            self.assertEqual(replay.divergence_count, 0)
        finally:
            if runtime.database_runtime is not None:
                runtime.database_runtime.dispose()

    async def test_replay_detects_missing_chain_reference(self) -> None:
        event_store = InMemoryEventStore()
        audit_repo = InMemoryAuditRepository()
        decision_id = "decision_missing_baseline"
        context = DecisionContext(
            decision_id=decision_id,
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=utc_now(),
            market_snapshot_ref="evt_market_1",
            feature_snapshot_ref="evt_feature_1",
            portfolio_snapshot_ref="evt_portfolio_1",
            health_snapshot_ref="TODO_health_snapshot_ref",
            mode="paper_live",
            current_position_qty=0.0,
        )
        target = PositionTarget(
            decision_id=decision_id,
            symbol="BTC-USDT",
            current_position_qty=0.0,
            target_position_qty=0.001,
            delta_position_qty=0.001,
            current_notional=0.0,
            target_notional=100.0,
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=utc_now(),
        )
        policy = PolicyDecision(
            decision_id=decision_id,
            mode="paper_live",
            allowed=True,
            requires_human_approval=False,
            allowed_symbols=["BTC-USDT"],
            allowed_execution_styles=["market"],
        )
        risk = RiskDecision(
            decision_id=decision_id,
            approved=True,
            modified=False,
            capped_target_position_qty=0.001,
            risk_score=0.1,
        )
        context_event = build_envelope(
            topic=topics.DECISION_CONTEXTS,
            key="BTC-USDT",
            payload_model=context,
            source_component="test",
        )
        target_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT",
            payload_model=target,
            source_component="test",
        )
        policy_event = build_envelope(
            topic=topics.POLICY_DECISIONS,
            key="BTC-USDT",
            payload_model=policy,
            source_component="test",
        )
        risk_event = build_envelope(
            topic=topics.RISK_DECISIONS,
            key="BTC-USDT",
            payload_model=risk,
            source_component="test",
        )
        for envelope in (context_event, target_event, policy_event, risk_event):
            event_store.append(envelope)

        record = DecisionAuditRecord(
            decision_id=decision_id,
            decision_context_ref=context_event.event_id,
            baseline_assessment_ref="evt_missing_baseline",
            position_target_ref=target_event.event_id,
            policy_decision_ref=policy_event.event_id,
            risk_decision_ref=risk_event.event_id,
        )
        audit_repo.upsert(record)
        event_store.append(
            build_envelope(
                topic=topics.AUDIT_RECORDS,
                key=decision_id,
                payload_model=record,
                source_component="test",
            )
        )

        replay = ReplayEngine(
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=audit_repo,
        ).replay()

        self.assertGreater(replay.divergence_count, 0)
        self.assertTrue(
            any("baseline_assessment_ref" in issue for issue in replay.decision_chain_issues)
        )
        self.assertTrue(
            any("missing_health_snapshot" in issue for issue in replay.decision_chain_issues)
        )

    async def test_replay_detects_execution_semantic_mismatch(self) -> None:
        event_store = InMemoryEventStore()
        audit_repo = InMemoryAuditRepository()
        intent = OrderIntent(
            intent_id="intent_semantic_mismatch",
            decision_id="decision_semantic_mismatch",
            symbol="BTC-USDT-SWAP",
            side="sell",
            quantity=Decimal("0.01"),
            execution_style="exchange",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=True,
            close_only=True,
            td_mode="cross",
            position_mode="long_short_mode",
            pos_side="long",
            reduce_only_reason="position_intent_close_path",
            close_only_reason="position_intent_close_path",
            instrument_family="BTC-USDT",
            settle_currency="USDT",
            idempotency_key="intent_semantic_mismatch",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="flat",
            execution_action="exit",
            position_intent="close_long",
        )
        order_state = OrderState(
            decision_id="decision_semantic_mismatch",
            intent_id="intent_semantic_mismatch",
            symbol="BTC-USDT-SWAP",
            client_order_id="clord_semantic_mismatch",
            venue="OKX",
            exchange_order_id="ord_semantic_mismatch",
            status="SUBMITTED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=utc_now(),
            last_update_ts=utc_now(),
            requested_qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            remaining_qty=Decimal("0.01"),
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=True,
            close_only=True,
            td_mode="isolated",
            position_mode="long_short_mode",
            pos_side="long",
            reduce_only_reason="position_intent_close_path",
            close_only_reason="position_intent_close_path",
            instrument_family="BTC-USDT",
            settle_currency="USDT",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="flat",
            execution_action="exit",
            position_intent="close_long",
            submission_payload={"tdMode": "isolated", "posSide": "long"},
        )
        event_store.append(
            build_envelope(
                topic=topics.ORDER_INTENTS,
                key=intent.symbol,
                payload_model=intent,
                source_component="test",
            )
        )
        event_store.append(
            build_envelope(
                topic=topics.ORDER_UPDATES,
                key=order_state.symbol,
                payload_model=order_state,
                source_component="test",
            )
        )

        replay = ReplayEngine(
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=audit_repo,
        ).replay()

        self.assertTrue(
            any(
                "execution_chain_order_state_semantic_mismatch" in issue and "field=td_mode" in issue
                for issue in replay.execution_chain_issues
            )
        )

    async def test_duplicate_fill_does_not_mutate_portfolio_twice_and_replay_stays_reconstructable(self) -> None:
        settings = self._paper_runtime_settings()
        runtime = await build_runtime(settings)

        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        original_snapshot = runtime.portfolio_repo.latest()
        self.assertIsNotNone(original_snapshot)
        original_fill = runtime.execution_repo.fills()[0]

        await publish_model(
            bus=runtime.bus,
            topic=topics.FILL_EVENTS,
            key=original_fill.symbol,
            payload_model=original_fill,
            source_component="test_duplicate_fill",
        )

        duplicate_snapshot = runtime.portfolio_repo.latest()
        self.assertIsNotNone(duplicate_snapshot)
        self.assertEqual(
            self._snapshot_signature(duplicate_snapshot),
            self._snapshot_signature(original_snapshot),
        )

        replay = ReplayEngine(
            event_store=runtime.event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=settings.initial_usdt_balance,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=runtime.audit_repo,
            portfolio_repo=runtime.portfolio_repo,
        ).replay()

        self.assertEqual(
            self._snapshot_signature(replay.final_stored_snapshot),
            self._snapshot_signature(duplicate_snapshot),
        )
        self.assertTrue(
            any("execution_chain_duplicate_fill_events" in issue for issue in replay.execution_chain_issues)
        )

    async def test_replay_detects_missing_execution_plan_for_emitted_intent(self) -> None:
        event_store = InMemoryEventStore()
        audit_repo = InMemoryAuditRepository()
        decision_id = "decision_missing_execution_plan"
        health_snapshot = HealthSnapshot(
            decision_id=decision_id,
            mode="paper_live",
            operating_state="real_market_paper",
            status="ok",
            halted=False,
            blockers=[],
            components=[],
        )
        health_event = build_envelope(
            topic=topics.HEALTH_SNAPSHOTS,
            key="BTC-USDT",
            payload_model=health_snapshot,
            source_component="test",
        )
        context = DecisionContext(
            decision_id=decision_id,
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=utc_now(),
            market_snapshot_ref="evt_market_1",
            feature_snapshot_ref="evt_feature_1",
            portfolio_snapshot_ref="evt_portfolio_1",
            health_snapshot_ref=health_event.event_id,
            mode="paper_live",
            current_position_qty=0.0,
        )
        baseline = BaselineAssessment(
            decision_id=decision_id,
            symbol="BTC-USDT",
            regime="trend",
            direction_bias="long",
            trend_strength=0.8,
            volatility_state="medium",
            confidence=0.7,
            holding_horizon="15m",
            invalidation_conditions=[],
            reason_codes=["test"],
            engine_version="test",
        )
        target = PositionTarget(
            decision_id=decision_id,
            symbol="BTC-USDT",
            current_position_qty=0.0,
            target_position_qty=0.001,
            delta_position_qty=0.001,
            current_notional=0.0,
            target_notional=100.0,
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=utc_now(),
        )
        policy = PolicyDecision(
            decision_id=decision_id,
            mode="paper_live",
            allowed=True,
            execution_allowed=True,
            submission_allowed=False,
            dry_run_only=False,
            requires_human_approval=False,
            allowed_symbols=["BTC-USDT"],
            allowed_execution_styles=["market"],
        )
        risk = RiskDecision(
            decision_id=decision_id,
            approved=True,
            modified=False,
            capped_target_position_qty=0.001,
            risk_score=0.1,
        )
        intent = OrderIntent(
            intent_id="intent_missing_plan",
            decision_id=decision_id,
            symbol="BTC-USDT",
            side="buy",
            quantity=0.001,
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="intent_missing_plan",
        )
        context_event = build_envelope(
            topic=topics.DECISION_CONTEXTS,
            key="BTC-USDT",
            payload_model=context,
            source_component="test",
        )
        baseline_event = build_envelope(
            topic=topics.BASELINE_ASSESSMENTS,
            key="BTC-USDT",
            payload_model=baseline,
            source_component="test",
        )
        target_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT",
            payload_model=target,
            source_component="test",
        )
        policy_event = build_envelope(
            topic=topics.POLICY_DECISIONS,
            key="BTC-USDT",
            payload_model=policy,
            source_component="test",
        )
        risk_event = build_envelope(
            topic=topics.RISK_DECISIONS,
            key="BTC-USDT",
            payload_model=risk,
            source_component="test",
        )
        intent_event = build_envelope(
            topic=topics.ORDER_INTENTS,
            key="BTC-USDT",
            payload_model=intent,
            source_component="test",
        )
        for envelope in (
            health_event,
            context_event,
            baseline_event,
            target_event,
            policy_event,
            risk_event,
            intent_event,
        ):
            event_store.append(envelope)

        record = DecisionAuditRecord(
            decision_id=decision_id,
            decision_context_ref=context_event.event_id,
            baseline_assessment_ref=baseline_event.event_id,
            position_target_ref=target_event.event_id,
            policy_decision_ref=policy_event.event_id,
            risk_decision_ref=risk_event.event_id,
            order_intent_refs=[intent_event.event_id],
        )
        audit_repo.upsert(record)
        event_store.append(
            build_envelope(
                topic=topics.AUDIT_RECORDS,
                key=decision_id,
                payload_model=record,
                source_component="test",
            )
        )

        replay = ReplayEngine(
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=audit_repo,
        ).replay()

        self.assertGreater(replay.divergence_count, 0)
        self.assertTrue(
            any("missing_execution_plan" in issue for issue in replay.decision_chain_issues)
        )

    async def test_replay_reconstructs_partial_fill_then_cancel_execution_chain(self) -> None:
        event_store = InMemoryEventStore()
        audit_repo = InMemoryAuditRepository()
        decision_id = "decision_partial_cancel"
        now = utc_now()
        market_snapshot = MarketSnapshot(
            symbol="BTC-USDT",
            exchange="OKX",
            snapshot_ts=now,
            best_bid=67_995.0,
            best_ask=68_005.0,
            last_price=68_000.0,
            bid_size=1.0,
            ask_size=1.0,
            volume_24h=1_000_000.0,
            kline_15m={"open": 67_900.0, "high": 68_100.0, "low": 67_800.0, "close": 68_000.0, "volume": 100.0},
            kline_1h={"open": 67_800.0, "high": 68_150.0, "low": 67_700.0, "close": 68_000.0, "volume": 400.0},
            recent_trades=[],
            orderbook_depth={"bids": [], "asks": []},
        )

        health_snapshot = HealthSnapshot(
            decision_id=decision_id,
            mode="guarded_live",
            operating_state="guarded_simulated_submit_enabled",
            status="ok",
            halted=False,
            blockers=[],
            components=[],
        )
        context = DecisionContext(
            decision_id=decision_id,
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=now,
            market_snapshot_ref="evt_market_partial_cancel",
            feature_snapshot_ref="evt_feature_partial_cancel",
            portfolio_snapshot_ref="evt_portfolio_partial_cancel",
            health_snapshot_ref="",
            mode="guarded_live",
            current_position_qty=0.0,
        )
        baseline = BaselineAssessment(
            decision_id=decision_id,
            symbol="BTC-USDT",
            regime="trend",
            direction_bias="long",
            trend_strength=0.8,
            volatility_state="medium",
            confidence=0.7,
            holding_horizon="15m",
            invalidation_conditions=[],
            reason_codes=["test"],
            engine_version="test",
        )
        target = PositionTarget(
            decision_id=decision_id,
            symbol="BTC-USDT",
            current_position_qty=0.0,
            target_position_qty=0.001,
            delta_position_qty=0.001,
            current_notional=0.0,
            target_notional=68.0,
            rebalance_reason="test_partial_cancel",
            urgency="medium",
            max_slippage_tolerance_bps=20,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=now,
        )
        policy = PolicyDecision(
            decision_id=decision_id,
            mode="guarded_live",
            allowed=True,
            execution_allowed=True,
            submission_allowed=True,
            dry_run_only=False,
            requires_human_approval=False,
            allowed_symbols=["BTC-USDT"],
            allowed_execution_styles=["market"],
        )
        risk = RiskDecision(
            decision_id=decision_id,
            approved=True,
            modified=False,
            capped_target_position_qty=0.001,
            risk_score=0.1,
        )
        plan = ExecutionPlan(
            plan_id="plan_partial_cancel",
            decision_id=decision_id,
            symbol="BTC-USDT",
            current_position_qty=0.0,
            target_position_qty=0.001,
            approved_target_position_qty=0.001,
            delta_qty=0.001,
            side="buy",
            execution_style="taker",
            order_type="market",
            urgency="medium",
            max_slippage_tolerance_bps=20,
        )
        intent = OrderIntent(
            intent_id="intent_partial_cancel",
            decision_id=decision_id,
            symbol="BTC-USDT",
            side="buy",
            quantity=0.001,
            execution_style="taker",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            idempotency_key="intent_partial_cancel",
        )
        created_state = OrderState(
            decision_id=decision_id,
            intent_id=intent.intent_id,
            symbol="BTC-USDT",
            client_order_id="clord_partial_cancel",
            venue="OKX",
            exchange_order_id="ord_partial_cancel",
            status="CREATED",
            submission_mode="guarded_simulated_submit",
            exchange_status="created",
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=0.001,
            filled_qty=0.0,
            remaining_qty=0.001,
            average_fill_price=None,
            fees=0.0,
            submission_payload={"instId": "BTC-USDT"},
        )
        submitting_state = created_state.model_copy(
            update={
                "status": "SUBMITTING",
                "exchange_status": "submitting",
                "last_update_ts": now + timedelta(milliseconds=500),
                "last_exchange_update_ts": now + timedelta(milliseconds=500),
            }
        )
        submitted_state = created_state.model_copy(
            update={
                "status": "SUBMITTED",
                "exchange_status": "live",
                "last_update_ts": now + timedelta(seconds=1),
                "last_exchange_update_ts": now + timedelta(seconds=1),
            }
        )
        partial_state = created_state.model_copy(
            update={
                "status": "PARTIALLY_FILLED",
                "exchange_status": "partially_filled",
                "last_update_ts": now + timedelta(seconds=2),
                "last_exchange_update_ts": now + timedelta(seconds=2),
                "filled_qty": Decimal("0.0004"),
                "remaining_qty": Decimal("0.0006"),
                "average_fill_price": Decimal("68000"),
            }
        )
        cancel_pending_state = partial_state.model_copy(
            update={
                "status": "CANCEL_PENDING",
                "exchange_status": "cancel_pending",
                "last_update_ts": now + timedelta(seconds=3),
                "last_exchange_update_ts": now + timedelta(seconds=3),
            }
        )
        canceled_state = cancel_pending_state.model_copy(
            update={
                "status": "CANCELED",
                "exchange_status": "canceled",
                "last_update_ts": now + timedelta(seconds=4),
                "last_exchange_update_ts": now + timedelta(seconds=4),
                "canceled_ts": now + timedelta(seconds=4),
            }
        )
        partial_fill = FillEvent(
            fill_id="fill_partial_cancel_1",
            decision_id=decision_id,
            intent_id=intent.intent_id,
            client_order_id="clord_partial_cancel",
            exchange_order_id="ord_partial_cancel",
            symbol="BTC-USDT",
            venue="OKX",
            side="buy",
            fill_qty=Decimal("0.0004"),
            fill_price=Decimal("68000"),
            fee_amount=Decimal("0.0272"),
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
            order_status_after_fill="PARTIALLY_FILLED",
        )

        health_event = build_envelope(
            topic=topics.HEALTH_SNAPSHOTS,
            key="BTC-USDT",
            payload_model=health_snapshot,
            source_component="test",
        )
        context = context.model_copy(update={"health_snapshot_ref": health_event.event_id})
        portfolio_state = PortfolioReconstructionService(
            initial_usdt_balance=10_000.0,
            snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
        ).rebuild_snapshot(
            fills=[partial_fill],
            price_provider=lambda _symbol: 68_000.0,
        ).model_copy(
            update={
                "decision_id": decision_id,
                "source_intent_id": intent.intent_id,
                "source_fill_id": partial_fill.fill_id,
            }
        )
        context_event = build_envelope(
            topic=topics.DECISION_CONTEXTS,
            key="BTC-USDT",
            payload_model=context,
            source_component="test",
        )
        baseline_event = build_envelope(
            topic=topics.BASELINE_ASSESSMENTS,
            key="BTC-USDT",
            payload_model=baseline,
            source_component="test",
        )
        target_event = build_envelope(
            topic=topics.POSITION_TARGETS,
            key="BTC-USDT",
            payload_model=target,
            source_component="test",
        )
        policy_event = build_envelope(
            topic=topics.POLICY_DECISIONS,
            key="BTC-USDT",
            payload_model=policy,
            source_component="test",
        )
        risk_event = build_envelope(
            topic=topics.RISK_DECISIONS,
            key="BTC-USDT",
            payload_model=risk,
            source_component="test",
        )
        plan_event = build_envelope(
            topic=topics.EXECUTION_PLANS,
            key="BTC-USDT",
            payload_model=plan,
            source_component="test",
        )
        intent_event = build_envelope(
            topic=topics.ORDER_INTENTS,
            key="BTC-USDT",
            payload_model=intent,
            source_component="test",
        )
        created_event = build_envelope(
            topic=topics.ORDER_UPDATES,
            key="BTC-USDT",
            payload_model=created_state,
            source_component="test",
        )
        submitting_event = build_envelope(
            topic=topics.ORDER_UPDATES,
            key="BTC-USDT",
            payload_model=submitting_state,
            source_component="test",
        )
        submitted_event = build_envelope(
            topic=topics.ORDER_UPDATES,
            key="BTC-USDT",
            payload_model=submitted_state,
            source_component="test",
        )
        partial_state_event = build_envelope(
            topic=topics.ORDER_UPDATES,
            key="BTC-USDT",
            payload_model=partial_state,
            source_component="test",
        )
        cancel_pending_event = build_envelope(
            topic=topics.ORDER_UPDATES,
            key="BTC-USDT",
            payload_model=cancel_pending_state,
            source_component="test",
        )
        canceled_state_event = build_envelope(
            topic=topics.ORDER_UPDATES,
            key="BTC-USDT",
            payload_model=canceled_state,
            source_component="test",
        )
        fill_event = build_envelope(
            topic=topics.FILL_EVENTS,
            key="BTC-USDT",
            payload_model=partial_fill,
            source_component="test",
        )
        portfolio_event = build_envelope(
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=portfolio_state,
            source_component="test",
        )
        reconciliation = ReconciliationReport(
            reconciliation_id="recon_partial_cancel",
            decision_id=decision_id,
            portfolio_snapshot_ref=portfolio_event.event_id,
            as_of_ts=now,
            order_diff={},
            fill_diff={},
            balance_diff={},
            position_diff={"stored": {}, "reconstructed": {}, "mismatches": {}},
            mismatch_reasons=[],
            safety_impacts=[],
            severity="CLEAN",
            remediation_action=None,
            halt_required=False,
        )
        reconciliation_event = build_envelope(
            topic=topics.RECONCILIATION_REPORTS,
            key="BTC-USDT",
            payload_model=reconciliation,
            source_component="test",
        )

        record = DecisionAuditRecord(
            decision_id=decision_id,
            decision_context_ref=context_event.event_id,
            baseline_assessment_ref=baseline_event.event_id,
            position_target_ref=target_event.event_id,
            policy_decision_ref=policy_event.event_id,
            risk_decision_ref=risk_event.event_id,
            execution_plan_ref=plan_event.event_id,
            order_intent_refs=[intent_event.event_id],
            order_state_refs=[
                created_event.event_id,
                submitting_event.event_id,
                submitted_event.event_id,
                partial_state_event.event_id,
                cancel_pending_event.event_id,
                canceled_state_event.event_id,
            ],
            fill_event_refs=[fill_event.event_id],
            portfolio_delta_ref=portfolio_event.event_id,
            reconciliation_refs=[reconciliation_event.event_id],
        )
        audit_repo.upsert(record)
        audit_event = build_envelope(
            topic=topics.AUDIT_RECORDS,
            key=decision_id,
            payload_model=record,
            source_component="test",
        )

        for envelope in (
            build_envelope(
                topic=topics.MARKET_SNAPSHOTS,
                key="BTC-USDT",
                payload_model=market_snapshot,
                source_component="test",
            ),
            health_event,
            context_event,
            baseline_event,
            target_event,
            policy_event,
            risk_event,
            plan_event,
            intent_event,
            created_event,
            submitting_event,
            submitted_event,
            partial_state_event,
            cancel_pending_event,
            canceled_state_event,
            fill_event,
            portfolio_event,
            reconciliation_event,
            audit_event,
        ):
            event_store.append(envelope)

        replay = ReplayEngine(
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=audit_repo,
        ).replay()

        self.assertEqual(replay.divergence_count, 0)
        self.assertEqual(replay.execution_chain_issues, [])
        self.assertEqual(replay.decision_chain_issues, [])
        self.assertEqual(replay.audit_issues, [])
        self.assertEqual(
            self._snapshot_signature(replay.final_reconstructed_snapshot),
            self._snapshot_signature(portfolio_state),
        )

    async def test_replay_tracks_baseline_switches_without_false_divergence(self) -> None:
        event_store = InMemoryEventStore()
        audit_repo = InMemoryAuditRepository()
        now = utc_now()
        startup_baseline = AccountBaselineSnapshot(
            account_source="okx",
            exchange_snapshot_ts=now,
            imported_at=now,
            baseline_status="baseline_imported",
            baseline_kind="startup_import",
            reason_codes=["clean_account_balances_only"],
        )
        startup_baseline_event = build_envelope(
            topic=topics.ACCOUNT_BASELINES,
            key="okx",
            payload_model=startup_baseline,
            source_component="test",
        )
        startup_snapshot = self._baseline_portfolio_snapshot(snapshot_ts=now)
        startup_snapshot_event = build_envelope(
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=startup_snapshot,
            source_component="test",
        )
        rebaseline_action = OperatorActionRecord(
            action="rebaseline",
            actor_role="admin",
            reason="accept_exchange_state",
            status="rebaseline_completed",
            recovery_state_before="review_required",
            recovery_state_after="rebaseline_completed",
        )
        rebaseline_action_event = build_envelope(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=rebaseline_action,
            source_component="test",
        )
        switched_baseline = AccountBaselineSnapshot(
            account_source="okx",
            exchange_snapshot_ts=now + timedelta(seconds=10),
            imported_at=now + timedelta(seconds=10),
            baseline_status="rebaseline_completed",
            baseline_kind="operator_rebaseline",
            previous_baseline_ref=startup_baseline_event.event_id,
            operator_action_ref=rebaseline_action_event.event_id,
            trigger_reason="accept_exchange_state",
            reason_codes=["operator_rebaseline_confirmed", "historical_fills_imported"],
        )
        switched_baseline_event = build_envelope(
            topic=topics.ACCOUNT_BASELINES,
            key="okx",
            payload_model=switched_baseline,
            source_component="test",
        )
        switched_snapshot = self._baseline_portfolio_snapshot(
            snapshot_ts=now + timedelta(seconds=10),
            balances={"USDT": 9950.0, "BTC": 0.001},
            position_qty=0.001,
            avg_entry_price=70000.0,
        )
        switched_snapshot_event = build_envelope(
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=switched_snapshot,
            source_component="test",
        )
        post_rebaseline_fill = FillEvent(
            fill_id="fill_after_switch",
            decision_id="decision_after_switch",
            intent_id="intent_after_switch",
            client_order_id="cl_after_switch",
            exchange_order_id="ord_after_switch",
            symbol="BTC-USDT",
            venue="OKX",
            side="buy",
            fill_qty=0.001,
            fill_price=71000.0,
            fee_amount=0.1,
            liquidity_role="taker",
            exchange_timestamp=now + timedelta(seconds=20),
            ingestion_timestamp=now + timedelta(seconds=20),
            order_status_after_fill="FILLED",
        )
        fill_event = build_envelope(
            topic=topics.FILL_EVENTS,
            key="BTC-USDT",
            payload_model=post_rebaseline_fill,
            source_component="test",
        )
        state = PortfolioState(initial_usdt_balance=10_000.0)
        state.load_portfolio_snapshot(switched_snapshot)
        state.apply_fill(post_rebaseline_fill)
        final_snapshot = PortfolioSnapshotBuilder(
            pnl_calculator=PortfolioPnLCalculator()
        ).build(
            state=state,
            price_provider=lambda _symbol: 70_500.0,
        ).model_copy(
            update={
                "decision_id": "decision_after_switch",
                "source_intent_id": "intent_after_switch",
                "source_fill_id": "fill_after_switch",
                "snapshot_ts": now + timedelta(seconds=21),
            }
        )
        final_snapshot_event = build_envelope(
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=final_snapshot,
            source_component="test",
        )

        for envelope in (
            startup_baseline_event,
            startup_snapshot_event,
            rebaseline_action_event,
            switched_baseline_event,
            switched_snapshot_event,
            fill_event,
            final_snapshot_event,
        ):
            event_store.append(envelope)

        replay = ReplayEngine(
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            audit_repo=audit_repo,
        ).replay()

        self.assertEqual(replay.portfolio_issues, [])
        self.assertEqual(replay.baseline_switch_count, 2)
        self.assertEqual(replay.baseline_switch_issues, [])

    @staticmethod
    def _baseline_portfolio_snapshot(
        *,
        snapshot_ts,
        balances: dict[str, float] | None = None,
        position_qty: float = 0.0,
        avg_entry_price: float = 0.0,
    ):
        from aats.schemas.portfolio import PortfolioSnapshot, Position

        balances = balances or {"USDT": 10_000.0}
        positions = []
        if abs(position_qty) > 1e-12:
            positions.append(
                Position(
                    symbol="BTC-USDT",
                    position_qty=position_qty,
                    position_notional=position_qty * avg_entry_price,
                    avg_entry_price=avg_entry_price,
                    unrealized_pnl=0.0,
                )
            )
        return PortfolioSnapshot(
            snapshot_ts=snapshot_ts,
            balances=balances,
            positions=positions,
            cost_basis={"BTC-USDT": position_qty * avg_entry_price} if positions else {},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_equity=Decimal(str(balances.get("USDT", 0.0))) + sum(
                (position.position_notional for position in positions),
                start=Decimal("0"),
            ),
            gross_exposure=sum((abs(position.position_notional) for position in positions), start=Decimal("0")),
            net_exposure=sum((position.position_notional for position in positions), start=Decimal("0")),
        )

    @staticmethod
    def _postgres_settings(database_url: str, **overrides) -> AATSSettings:
        return AATSSettings.model_validate(
            {
                "storage_mode": "postgres",
                "database_url": database_url,
                "database_auto_create_schema": True,
                "database_single_runtime_guard_enabled": False,
                "local_publish_iterations": 4,
                "local_publish_interval_seconds": 0.0,
                **overrides,
            }
        )

    @staticmethod
    def _paper_runtime_settings() -> AATSSettings:
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
            }
        )

    @staticmethod
    def _snapshot_signature(snapshot) -> dict:
        return {
            "balances": snapshot.balances,
            "positions": {position.symbol: position.position_qty for position in snapshot.positions},
            "cost_basis": snapshot.cost_basis,
            "realized_pnl": snapshot.realized_pnl,
            "unrealized_pnl": snapshot.unrealized_pnl,
            "total_equity": snapshot.total_equity,
            "gross_exposure": snapshot.gross_exposure,
            "net_exposure": snapshot.net_exposure,
        }


if __name__ == "__main__":
    unittest.main()
