from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.storage.event_store import InMemoryEventStore
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeFill
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.schemas.reconciliation import ReconciliationReport
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


class CapturingComparator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def compare(self, **kwargs) -> ReconciliationReport:
        self.calls.append(kwargs)
        return ReconciliationReport(
            reconciliation_id="recon_capture",
            as_of_ts=utc_now(),
            product_type=kwargs["product_type"],
            margin_mode=kwargs["margin_mode"],
            allowed_symbols=list(kwargs["allowed_symbols"]),
            exchange_comparison_enabled=bool(kwargs["exchange_comparison_enabled"]),
            order_diff={"reconstructed": {}, "exchange": {}},
            fill_diff={"replayed": {}, "exchange": {}},
            balance_diff={"reconstructed": {}, "exchange": {}},
            position_diff={
                "stored": {},
                "reconstructed": {},
                "reconstructed_mismatches": {},
                "exchange": {},
                "exchange_mismatches": {},
            },
            mismatch_categories=[],
            mismatch_reasons=[],
            safety_impacts=[],
            severity="CLEAN",
        )


class StaticExchangeFetcher:
    def __init__(self, snapshot: ExchangeAccountSnapshot | None) -> None:
        self._snapshot = snapshot

    def fetch_snapshot(self) -> ExchangeAccountSnapshot | None:
        return self._snapshot


class TestReconciliationRepair(unittest.IsolatedAsyncioTestCase):
    def test_persist_report_invokes_stale_reconciliation_halt_clearer(self) -> None:
        event_store = InMemoryEventStore()
        calls: list[str] = []
        service = ReconciliationService(
            settings=AATSSettings.model_validate({}),
            bus=InMemoryEventBus(event_store=event_store, persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=InMemoryExecutionRepository(),
            portfolio_repo=InMemoryPortfolioRepository(),
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: Decimal("0"),
            bootstrap_portfolio_from_exchange=False,
            metrics=None,
            stale_reconciliation_halt_clearer=lambda report: calls.append(report.reconciliation_id) is None,
        )
        report = ReconciliationReport(
            reconciliation_id="recon_persist_clearer",
            as_of_ts=utc_now(),
            product_type="spot",
            margin_mode="cash",
            allowed_symbols=["BTC-USDT"],
            exchange_comparison_enabled=False,
            order_diff={"reconstructed": {}, "exchange": {}},
            fill_diff={"replayed": {}, "exchange": {}},
            balance_diff={"reconstructed": {}, "exchange": {}},
            position_diff={
                "stored": {},
                "reconstructed": {},
                "reconstructed_mismatches": {},
                "exchange": {},
                "exchange_mismatches": {},
            },
            mismatch_categories=[],
            mismatch_reasons=[],
            safety_impacts=[],
            severity="CLEAN",
        )

        service._persist_report_sync(report)

        self.assertEqual(calls, ["recon_persist_clearer"])

    def test_report_builder_does_not_trust_recovery_auto_healed_baseline(self) -> None:
        now = utc_now()
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "bootstrap_portfolio_from_exchange": True,
            }
        )
        portfolio_repo = InMemoryPortfolioRepository()
        recovery_snapshot = PortfolioSnapshot(
            snapshot_ts=now,
            decision_id="decision_recovery_snapshot",
            snapshot_origin="recovery_auto_healed",
            product_type="derivatives",
            margin_mode="cross",
            balances={"USDT": Decimal("10000.0")},
            positions=[
                Position(
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:long",
                    position_qty=Decimal("0.01"),
                    position_notional=Decimal("650.0"),
                    avg_entry_price=Decimal("65000.0"),
                    unrealized_pnl=Decimal("0"),
                    product_type="derivatives",
                    margin_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                )
            ],
            cost_basis={"BTC-USDT-SWAP:long": Decimal("65000.0")},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("10000.0"),
            gross_exposure=Decimal("650.0"),
            net_exposure=Decimal("650.0"),
            risk_budget_usage={},
        )
        portfolio_repo.save_snapshot(recovery_snapshot)
        comparator = CapturingComparator()
        service = ReconciliationService(
            settings=settings,
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            fetcher=StaticExchangeFetcher(
                ExchangeAccountSnapshot(
                    account_source="okx",
                    fetched_at=now,
                    balances=[],
                    positions=[],
                    open_orders=[],
                    fills=[],
                    instruments=[],
                )
            ),
            comparator=comparator,
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=InMemoryExecutionRepository(),
            portfolio_repo=portfolio_repo,
            event_store=InMemoryEventStore(),
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: Decimal("0"),
            bootstrap_portfolio_from_exchange=True,
            metrics=None,
        )

        service._build_report(
            decision_id=None,
            portfolio_snapshot_ref="manual_portfolio_snapshot:test",
            stored_snapshot=recovery_snapshot,
        )

        self.assertEqual(len(comparator.calls), 1)
        self.assertIs(comparator.calls[0]["trusted_exchange_portfolio_baseline"], False)
        reconstructed_snapshot = comparator.calls[0]["reconstructed_snapshot"]
        self.assertIsInstance(reconstructed_snapshot, PortfolioSnapshot)
        self.assertEqual(reconstructed_snapshot.positions, [])

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

    async def test_reconciliation_surfaces_truth_pending_parent_before_review_threshold(self) -> None:
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
        event_store = InMemoryEventStore()
        execution_repo = InMemoryExecutionRepository()
        exit_repo = InMemoryExitExecutionRepository()
        unknown_state = OrderState(
            decision_id="decision_recon_truth_pending_parent",
            execution_chain_id="chain_recon_truth_pending_parent",
            intent_id="intent_recon_truth_pending_parent",
            symbol="BTC-USDT-SWAP",
            client_order_id="clord_recon_truth_pending_parent",
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

        service = ReconciliationService(
            settings=initial_settings,
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

        report = await service.validate_now(reason="truth_pending_parent_visible")

        self.assertFalse(report.review_required)
        self.assertIn(
            "exit_execution_truth_pending",
            [str(detail.get("kind")) for detail in report.unknown_state_details],
        )
        self.assertIn("exit_execution_truth_pending", report.mismatch_categories)

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

    async def test_reconciliation_surfaces_childless_parent_issue(self) -> None:
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
            decision_id="decision_childless_parent_issue",
            execution_chain_id="chain_childless_parent_issue",
            intent_id="intent_childless_parent_issue",
            symbol="BTC-USDT-SWAP",
            client_order_id="clord_childless_parent_issue",
            venue="OKX",
            exchange_order_id="ord_childless_parent_issue",
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
        parent = create_exit_execution_intent_from_order_state(partial_state).model_copy(
            update={
                "target_exit_quantity": Decimal("5"),
                "aggregate_status": "PARTIALLY_FILLED",
                "aggregated_filled_quantity": Decimal("2"),
                "remaining_dispatchable_quantity": Decimal("3"),
                "remaining_unresolved_quantity": Decimal("3"),
                "metadata": {
                    "dispatch_template": {
                        "intent_id": "intent_childless_parent_issue",
                        "execution_chain_id": "chain_childless_parent_issue",
                        "decision_id": "decision_childless_parent_issue",
                        "symbol": "BTC-USDT-SWAP",
                    }
                },
            }
        )
        exit_repo.save_exit_execution_intent(parent)

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

        report = await service.validate_now(reason="childless_parent_visible")

        refreshed_parent = exit_repo.get_exit_execution_intent(parent.parent_intent_id)
        self.assertIsNotNone(refreshed_parent)
        assert refreshed_parent is not None
        self.assertEqual(
            refreshed_parent.metadata["resume_issue"]["kind"],
            "missing_child_refs_for_parent",
        )
        self.assertTrue(report.review_required)
        self.assertIn(
            "exit_execution_missing_child_refs_for_parent",
            [str(detail.get("kind")) for detail in report.unknown_state_details],
        )
        self.assertIn("exit_execution_missing_child_refs_for_parent", report.mismatch_categories)


class TestRepairBaselineAware(unittest.IsolatedAsyncioTestCase):
    """Regression: repair() must respect baseline when bootstrap_portfolio_from_exchange=True.

    When a baseline snapshot exists, pre-baseline fills should NOT be
    replayed.  Before the fix, repair() always did full fill replay,
    causing the 'local_repair' snapshot to include stale pre-baseline
    net positions, which then triggered a false position mismatch and
    system halt.
    """

    async def test_repair_ignores_pre_baseline_fills(self) -> None:
        """Pre-baseline fills must not influence the repaired snapshot."""
        now = datetime.now(timezone.utc)
        baseline_ts = now - timedelta(hours=1)
        pre_baseline_ts = baseline_ts - timedelta(hours=2)

        # Pre-baseline fill: 0.001 BTC buy — should be ignored by repair
        pre_baseline_fill = FillEvent(
            fill_id="fill_pre_baseline",
            decision_id="decision_pre",
            intent_id="intent_pre",
            client_order_id="clord_pre",
            exchange_order_id="ord_pre",
            symbol="BTC-USDT-SWAP",
            venue="OKX",
            side="buy",
            fill_qty=0.001,
            fill_price=50000.0,
            fee_amount=0.025,
            fee_currency="USDT",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            position_intent="open_long",
            liquidity_role="taker",
            exchange_timestamp=pre_baseline_ts,
            ingestion_timestamp=pre_baseline_ts,
        )

        # Post-baseline fill: 0.002 BTC sell — should be included
        post_baseline_fill = FillEvent(
            fill_id="fill_post_baseline",
            decision_id="decision_post",
            intent_id="intent_post",
            client_order_id="clord_post",
            exchange_order_id="ord_post",
            symbol="BTC-USDT-SWAP",
            venue="OKX",
            side="sell",
            fill_qty=0.002,
            fill_price=51000.0,
            fee_amount=0.051,
            fee_currency="USDT",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="short",
            position_intent="open_short",
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
        )

        execution_repo = InMemoryExecutionRepository()
        for fill in [pre_baseline_fill, post_baseline_fill]:
            execution_repo.save_fill(fill)
            execution_repo.save_order_state(
                OrderState(
                    decision_id=fill.decision_id,
                    intent_id=fill.intent_id,
                    symbol=fill.symbol,
                    client_order_id=fill.client_order_id,
                    venue=fill.venue,
                    exchange_order_id=fill.exchange_order_id,
                    status="FILLED",
                    submitted_ts=fill.exchange_timestamp,
                    last_update_ts=fill.ingestion_timestamp,
                    requested_qty=float(fill.fill_qty),
                    filled_qty=float(fill.fill_qty),
                    remaining_qty=0.0,
                    average_fill_price=float(fill.fill_price),
                    fees=float(fill.fee_amount),
                    product_type="derivatives",
                    margin_mode="cross",
                )
            )

        portfolio_repo = InMemoryPortfolioRepository()

        # Baseline snapshot: represents exchange state at baseline_ts
        # (clean slate, no positions — the pre-baseline fill was already
        #  accounted for in an earlier baseline cycle)
        baseline_snapshot = PortfolioSnapshot(
            snapshot_ts=baseline_ts,
            decision_id="baseline_decision",
            snapshot_origin="exchange_import",
            product_type="derivatives",
            margin_mode="cross",
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
        portfolio_repo.save_snapshot(baseline_snapshot)

        # Current running snapshot: only post-baseline fill applied
        # → position = -0.002 (short). This is CORRECT.
        current_snapshot = PortfolioSnapshot(
            snapshot_ts=now,
            decision_id="decision_post",
            snapshot_origin="fill_derived",
            source_fill_id="fill_post_baseline",
            product_type="derivatives",
            margin_mode="cross",
            balances={"USDT": Decimal("9999.949")},
            positions=[
                {
                    "symbol": "BTC-USDT-SWAP",
                    "position_qty": Decimal("-0.002"),
                    "position_notional": Decimal("102.0"),
                    "avg_entry_price": Decimal("51000.0"),
                    "unrealized_pnl": Decimal("0"),
                    "product_type": "derivatives",
                    "margin_mode": "cross",
                }
            ],
            cost_basis={},
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_equity=Decimal("9999.949"),
            gross_exposure=Decimal("102.0"),
            net_exposure=Decimal("-102.0"),
            risk_budget_usage={},
        )
        portfolio_repo.save_snapshot(current_snapshot)

        event_store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            "trading_product_type": "derivatives",
            "margin_mode": "cross",
            "bootstrap_portfolio_from_exchange": True,
        })

        service = ReconciliationService(
            settings=settings,
            bus=InMemoryEventBus(event_store=event_store, persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=execution_repo,
            portfolio_repo=portfolio_repo,
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: Decimal("51000.0"),
            bootstrap_portfolio_from_exchange=True,
            metrics=None,
        )

        # The repair service should now be baseline-aware
        repair_svc = service.repair_service
        self.assertIsNotNone(repair_svc.settings)
        self.assertTrue(repair_svc.settings.bootstrap_portfolio_from_exchange)

        # Directly test the baseline-aware rebuild
        scoped_fills = [pre_baseline_fill, post_baseline_fill]
        rebuilt = repair_svc._rebuild_snapshot_baseline_aware(fills=scoped_fills)

        # With baseline-aware rebuild: only post_baseline_fill (-0.002 sell) is applied
        # on top of the clean baseline. The pre_baseline_fill (+0.001 buy) is ignored.
        position_map = {p.symbol: p for p in rebuilt.positions}
        btc_pos = position_map.get("BTC-USDT-SWAP")
        self.assertIsNotNone(btc_pos, "BTC-USDT-SWAP position should exist")
        # Position should be -0.002 (only post-baseline short), NOT -0.001 (net of both fills)
        self.assertEqual(
            btc_pos.position_qty,
            Decimal("-0.002"),
            f"Expected -0.002 (post-baseline only), got {btc_pos.position_qty}. "
            "Pre-baseline fill was incorrectly included in repair."
        )

    async def test_repair_does_not_use_recovery_snapshot_as_trusted_baseline(self) -> None:
        now = datetime.now(timezone.utc)
        portfolio_repo = InMemoryPortfolioRepository()
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now - timedelta(minutes=10),
                decision_id="decision_recovery_auto_healed",
                snapshot_origin="recovery_auto_healed",
                product_type="derivatives",
                margin_mode="cross",
                balances={"USDT": Decimal("10000.0")},
                positions=[
                    Position(
                        symbol="BTC-USDT-SWAP",
                        position_key="BTC-USDT-SWAP:long",
                        position_qty=Decimal("0.01"),
                        position_notional=Decimal("650.0"),
                        avg_entry_price=Decimal("65000.0"),
                        unrealized_pnl=Decimal("0"),
                        product_type="derivatives",
                        margin_mode="cross",
                        position_mode="long_short_mode",
                        pos_side="long",
                    )
                ],
                cost_basis={"BTC-USDT-SWAP:long": Decimal("65000.0")},
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_equity=Decimal("10000.0"),
                gross_exposure=Decimal("650.0"),
                net_exposure=Decimal("650.0"),
                risk_budget_usage={},
            )
        )
        service = ReconciliationService(
            settings=AATSSettings.model_validate(
                {
                    "trading_product_type": "derivatives",
                    "margin_mode": "cross",
                    "default_symbol": "BTC-USDT-SWAP",
                    "allowed_symbols": ("BTC-USDT-SWAP",),
                    "bootstrap_portfolio_from_exchange": True,
                }
            ),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=InMemoryExecutionRepository(),
            portfolio_repo=portfolio_repo,
            event_store=InMemoryEventStore(),
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: Decimal("65000.0"),
            bootstrap_portfolio_from_exchange=True,
            metrics=None,
        )

        rebuilt = service.repair_service._rebuild_snapshot_baseline_aware(fills=[])

        self.assertEqual(rebuilt.positions, [])
        self.assertEqual(rebuilt.snapshot_origin, "fill_derived")

    async def test_repair_falls_back_to_full_replay_without_baseline(self) -> None:
        """Without a baseline snapshot, repair should fall back to full fill replay."""
        now = datetime.now(timezone.utc)
        fill = FillEvent(
            fill_id="fill_no_baseline",
            decision_id="decision_no_baseline",
            intent_id="intent_no_baseline",
            client_order_id="clord_no_baseline",
            exchange_order_id="ord_no_baseline",
            symbol="BTC-USDT-SWAP",
            venue="OKX",
            side="buy",
            fill_qty=0.003,
            fill_price=50000.0,
            fee_amount=0.075,
            fee_currency="USDT",
            product_type="derivatives",
            margin_mode="cross",
            exposure_side="long",
            position_intent="open_long",
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
        )

        execution_repo = InMemoryExecutionRepository()
        execution_repo.save_fill(fill)
        execution_repo.save_order_state(
            OrderState(
                decision_id=fill.decision_id,
                intent_id=fill.intent_id,
                symbol=fill.symbol,
                client_order_id=fill.client_order_id,
                venue=fill.venue,
                exchange_order_id=fill.exchange_order_id,
                status="FILLED",
                submitted_ts=fill.exchange_timestamp,
                last_update_ts=fill.ingestion_timestamp,
                requested_qty=float(fill.fill_qty),
                filled_qty=float(fill.fill_qty),
                remaining_qty=0.0,
                average_fill_price=float(fill.fill_price),
                fees=float(fill.fee_amount),
                product_type="derivatives",
                margin_mode="cross",
            )
        )

        portfolio_repo = InMemoryPortfolioRepository()
        # No baseline snapshot — only a stale fill_derived
        portfolio_repo.save_snapshot(
            PortfolioSnapshot(
                snapshot_ts=now - timedelta(hours=1),
                decision_id="old_decision",
                snapshot_origin="fill_derived",
                product_type="derivatives",
                margin_mode="cross",
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

        event_store = InMemoryEventStore()
        settings = AATSSettings.model_validate({
            "trading_product_type": "derivatives",
            "margin_mode": "cross",
            "bootstrap_portfolio_from_exchange": True,
        })

        service = ReconciliationService(
            settings=settings,
            bus=InMemoryEventBus(event_store=event_store, persistence_mode="strict"),
            fetcher=ExchangeStateFetcher(account_service=None),
            comparator=StateComparator(),
            repair_service=ReconciliationRepairService(),
            reconciliation_repo=InMemoryReconciliationRepository(),
            execution_repo=execution_repo,
            portfolio_repo=portfolio_repo,
            event_store=event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=10_000.0,
                snapshot_builder=PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator()),
            ),
            price_provider=lambda _symbol: Decimal("50000.0"),
            bootstrap_portfolio_from_exchange=True,
            metrics=None,
        )

        # No baseline → full replay should include the fill
        repair_svc = service.repair_service
        rebuilt = repair_svc._rebuild_snapshot_baseline_aware(fills=[fill])
        position_map = {p.symbol: p for p in rebuilt.positions}
        btc_pos = position_map.get("BTC-USDT-SWAP")
        self.assertIsNotNone(btc_pos)
        self.assertEqual(btc_pos.position_qty, Decimal("0.003"))


if __name__ == "__main__":
    unittest.main()
