from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.storage.event_store import InMemoryEventStore
from aats.events import topics
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeFill
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.execution_engine.exit_intent_aggregator import (
    child_exit_order_ref_from_order_state,
    create_exit_execution_intent_from_order_state,
)
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.comparator import StateComparator
from aats.services.reconciliation_service.fetcher import ExchangeStateFetcher
from aats.services.reconciliation_service.repair import ReconciliationRepairService, ReconciliationService
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.exit_execution_repo import InMemoryExitExecutionRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository


def build_fill() -> FillEvent:
    now = datetime.now(timezone.utc)
    return FillEvent(
        fill_id="fill_repair_1",
        decision_id="decision_repair_1",
        intent_id="intent_repair_1",
        client_order_id="clord_repair_1",
        exchange_order_id="ord_repair_1",
        symbol="BTC-USDT",
        venue="PAPER",
        side="buy",
        fill_qty=0.001,
        fill_price=100.0,
        fee_amount=0.0,
        fee_currency="USDT",
        liquidity_role="taker",
        exchange_timestamp=now,
        ingestion_timestamp=now,
    )


class TestReconciliationRepair(unittest.IsolatedAsyncioTestCase):
    async def test_local_only_snapshot_divergence_is_rebuilt_safely(self) -> None:
        event_store = InMemoryEventStore()
        bus = InMemoryEventBus(event_store=event_store, persistence_mode="strict")
        execution_repo = InMemoryExecutionRepository()
        fill = build_fill()
        execution_repo.save_order_state(
            OrderState(
                decision_id="decision_repair_1",
                intent_id="intent_repair_1",
                symbol="BTC-USDT",
                client_order_id="clord_repair_1",
                venue="PAPER",
                exchange_order_id="ord_repair_1",
                status="FILLED",
                submitted_ts=fill.exchange_timestamp,
                last_update_ts=fill.ingestion_timestamp,
                requested_qty=0.001,
                filled_qty=0.001,
                remaining_qty=0.0,
                average_fill_price=100.0,
                fees=0.0,
            )
        )
        execution_repo.save_fill(fill)
        portfolio_repo = InMemoryPortfolioRepository()
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=datetime.now(timezone.utc),
                decision_id="decision_repair_1",
                balances={"USDT": Decimal("10000.0")},
                positions=[],
                cost_basis={},
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_equity=Decimal("10000.0"),
                gross_exposure=Decimal("0"),
                net_exposure=Decimal("0"),
                risk_budget_usage={},
            )
        )
        reconciliation_repo = InMemoryReconciliationRepository()
        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=bus,
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=reconciliation_repo,
            execution_repo=execution_repo,
            portfolio_repo=portfolio_repo,
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda symbol: 100.0 if symbol == "BTC-USDT" else 0.0,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )

        first_report = await service.validate_now(reason="unit_repair")
        second_report = await service.validate_now(reason="unit_repair_after_rebuild")

        self.assertTrue(first_report.halt_required)
        repaired_snapshot = portfolio_repo.latest()
        self.assertIsNotNone(repaired_snapshot)
        self.assertEqual(repaired_snapshot.positions[0].symbol, "BTC-USDT")
        self.assertEqual(repaired_snapshot.positions[0].position_qty, Decimal("0.001"))
        self.assertEqual(repaired_snapshot.total_equity, Decimal("10000.0"))
        self.assertFalse(second_report.halt_required)
        self.assertEqual(second_report.severity, "CLEAN")
        self.assertEqual(event_store.count(topic=topics.PORTFOLIO_SNAPSHOTS), 1)
        self.assertEqual(event_store.count(topic=topics.RECONCILIATION_REPORTS), 2)

    async def test_accepts_local_okx_fill_on_recent_window_boundary(self) -> None:
        now = datetime.now(timezone.utc)
        service = ReconciliationService(
            settings=AATSSettings.model_validate({"okx_fill_fetch_limit": 2}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=InMemoryExecutionRepository(),
            portfolio_repo=InMemoryPortfolioRepository(),
            event_store=InMemoryEventStore(),
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 0.0,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )
        local_fill = FillEvent(
            fill_id="boundary_fill",
            decision_id="decision_boundary",
            intent_id="intent_boundary",
            client_order_id="clord_boundary",
            exchange_order_id="ord_boundary",
            symbol="BTC-USDT-SWAP",
            venue="OKX",
            side="sell",
            fill_qty=0.0009,
            fill_price=73818.2,
            fee_amount=0.03321819,
            fee_currency="USDT",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="short",
            position_intent="open_short",
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
        )
        snapshot = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=now,
            balances=[],
            positions=[],
            open_orders=[],
            fills=[
                ExchangeFill(
                    fill_id="window_oldest",
                    exchange_order_id="ord_oldest",
                    client_order_id="cl_oldest",
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    side="sell",
                    fill_qty=0.001,
                    fill_price=73818.2,
                    fee_amount=0.03,
                    fill_ts=now,
                ),
                ExchangeFill(
                    fill_id="window_newer",
                    exchange_order_id="ord_newer",
                    client_order_id="cl_newer",
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=73830.0,
                    fee_amount=0.03,
                    fill_ts=now + timedelta(seconds=1),
                ),
            ],
            instruments=[],
        )

        accepted = service._accepted_exchange_fill_ids(exchange_snapshot=snapshot, local_fills=[local_fill])

        self.assertIn("boundary_fill", accepted)

    async def test_reconciliation_refreshes_parent_exit_intent_for_aged_unknown_write(self) -> None:
        now = datetime.now(timezone.utc)
        initial_settings = AATSSettings.model_validate(
            {
                "execution_unknown_submit_review_after_seconds": 300.0,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        refresh_settings = AATSSettings.model_validate(
            {
                "execution_unknown_submit_review_after_seconds": 0.0,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        event_store = InMemoryEventStore()
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        unknown_state = OrderState(
            decision_id="decision_recon_unknown_parent",
            execution_chain_id="chain_recon_unknown_parent",
            intent_id="intent_recon_unknown_parent",
            symbol="BTC-USDT-SWAP",
            client_order_id="clord_recon_unknown_parent",
            venue="OKX",
            exchange_order_id=None,
            status="SUBMITTED",
            exchange_status="live",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=Decimal("2"),
            filled_qty=Decimal("0"),
            remaining_qty=Decimal("2"),
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=True,
            close_only=True,
            product_type="derivatives",
            margin_mode="cross",
            position_mode="long_short_mode",
            pos_side="long",
            exposure_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
            execution_error="submission_unknown_check_exchange:OKXRequestError",
        )
        execution_repo.save_order_state(unknown_state)
        parent = create_exit_execution_intent_from_order_state(unknown_state)
        exit_repo.save_exit_execution_intent(parent)
        exit_repo.save_child_exit_order_ref(
            child_exit_order_ref_from_order_state(
                parent_intent_id=parent.parent_intent_id,
                order_state=unknown_state,
                settings=initial_settings,
            )
        )
        initial_parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_recon_unknown_parent")
        self.assertIsNotNone(initial_parent)
        self.assertEqual(initial_parent.aggregate_status, "CREATED")

        service = ReconciliationService(
            settings=refresh_settings,
            bus=InMemoryEventBus(event_store=event_store, persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=execution_repo,
            portfolio_repo=InMemoryPortfolioRepository(),
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 0.0,
            exit_execution_repo=exit_repo,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )

        report = await service.validate_now(reason="aged_unknown_parent_refresh")

        refreshed_parent = exit_repo.get_exit_execution_intent_by_execution_chain("chain_recon_unknown_parent")
        self.assertIsNotNone(refreshed_parent)
        self.assertEqual(refreshed_parent.aggregate_status, "REVIEW_REQUIRED")
        self.assertTrue(refreshed_parent.operator_review_required)
        self.assertTrue(report.review_required)
        self.assertIn(
            "exit_execution_parent_review_required",
            [str(detail.get("kind")) for detail in report.unknown_state_details],
        )

    async def test_reconciliation_surfaces_parent_resume_template_missing_for_partial_exit(self) -> None:
        now = datetime.now(timezone.utc)
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        event_store = InMemoryEventStore()
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        partial_state = OrderState(
            decision_id="decision_partial_parent_resume_missing",
            execution_chain_id="chain_partial_parent_resume_missing",
            intent_id="intent_partial_parent_resume_missing",
            symbol="BTC-USDT-SWAP",
            client_order_id="clord_partial_parent_resume_missing",
            venue="OKX",
            exchange_order_id="ord_partial_parent_resume_missing",
            status="FILLED",
            exchange_status="filled",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=Decimal("2"),
            filled_qty=Decimal("2"),
            remaining_qty=Decimal("0"),
            average_fill_price=Decimal("80000"),
            fees=Decimal("0"),
            reduce_only=True,
            close_only=True,
            product_type="derivatives",
            margin_mode="cross",
            position_mode="long_short_mode",
            pos_side="long",
            exposure_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
        )
        execution_repo.save_order_state(partial_state)
        parent = create_exit_execution_intent_from_order_state(partial_state).model_copy(
            update={"target_exit_quantity": Decimal("5")}
        )
        exit_repo.save_exit_execution_intent(parent)
        exit_repo.save_child_exit_order_ref(
            child_exit_order_ref_from_order_state(
                parent_intent_id=parent.parent_intent_id,
                order_state=partial_state,
                settings=settings,
            )
        )

        service = ReconciliationService(
            settings=settings,
            bus=InMemoryEventBus(event_store=event_store, persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=execution_repo,
            portfolio_repo=InMemoryPortfolioRepository(),
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 0.0,
            exit_execution_repo=exit_repo,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )

        report = await service.validate_now(reason="partial_exit_resume_template_missing")

        self.assertTrue(report.review_required)
        self.assertIn(
            "exit_execution_resume_template_missing",
            [str(detail.get("kind")) for detail in report.unknown_state_details],
        )
        self.assertIn("exit_execution_resume_template_missing", report.mismatch_categories)

    async def test_reconciliation_surfaces_parent_resume_limit_lookup_failure(self) -> None:
        now = datetime.now(timezone.utc)
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
            }
        )
        event_store = InMemoryEventStore()
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        partial_state = OrderState(
            decision_id="decision_partial_parent_limit_issue",
            execution_chain_id="chain_partial_parent_limit_issue",
            intent_id="intent_partial_parent_limit_issue",
            symbol="BTC-USDT-SWAP",
            client_order_id="clord_partial_parent_limit_issue",
            venue="OKX",
            exchange_order_id="ord_partial_parent_limit_issue",
            status="FILLED",
            exchange_status="filled",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=Decimal("2"),
            filled_qty=Decimal("2"),
            remaining_qty=Decimal("0"),
            average_fill_price=Decimal("80000"),
            fees=Decimal("0"),
            reduce_only=True,
            close_only=True,
            product_type="derivatives",
            margin_mode="cross",
            position_mode="long_short_mode",
            pos_side="long",
            exposure_side="long",
            execution_action="exit",
            leg_action="close",
            position_intent="close_long",
        )
        execution_repo.save_order_state(partial_state)
        parent = create_exit_execution_intent_from_order_state(partial_state).model_copy(
            update={
                "target_exit_quantity": Decimal("5"),
                "metadata": {
                    "dispatch_template": {
                        "intent_id": "intent_partial_parent_limit_issue",
                        "execution_chain_id": "chain_partial_parent_limit_issue",
                        "decision_id": "decision_partial_parent_limit_issue",
                        "symbol": "BTC-USDT-SWAP",
                        "side": "sell",
                        "quantity": "5",
                        "execution_style": "exchange",
                        "order_type": "market",
                        "urgency": "medium",
                        "time_in_force": "IOC",
                        "reduce_only": True,
                        "close_only": True,
                        "position_mode": "long_short_mode",
                        "pos_side": "long",
                        "execution_action": "exit",
                        "leg_action": "close",
                        "position_intent": "close_long",
                        "product_type": "derivatives",
                        "margin_mode": "cross",
                        "exposure_side": "long",
                        "idempotency_key": "intent_partial_parent_limit_issue",
                    },
                    "resume_issue": {
                        "kind": "resume_limit_lookup_failed",
                        "operator_review_required": True,
                        "updated_at": now.isoformat(),
                        "error": "max_size_limit_unavailable",
                    },
                },
            }
        )
        exit_repo.save_exit_execution_intent(parent)
        exit_repo.save_child_exit_order_ref(
            child_exit_order_ref_from_order_state(
                parent_intent_id=parent.parent_intent_id,
                order_state=partial_state,
                settings=settings,
            )
        )

        service = ReconciliationService(
            settings=settings,
            bus=InMemoryEventBus(event_store=event_store, persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=execution_repo,
            portfolio_repo=InMemoryPortfolioRepository(),
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: 0.0,
            exit_execution_repo=exit_repo,
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
        )

        report = await service.validate_now(reason="partial_exit_limit_lookup_failed")

        self.assertTrue(report.review_required)
        self.assertIn(
            "exit_execution_resume_limit_lookup_failed",
            [str(detail.get("kind")) for detail in report.unknown_state_details],
        )
        self.assertIn("exit_execution_resume_limit_lookup_failed", report.mismatch_categories)


if __name__ == "__main__":
    unittest.main()
