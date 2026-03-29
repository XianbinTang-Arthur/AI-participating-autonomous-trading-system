from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest.mock import Mock

from sqlalchemy import func, select

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository
from aats.storage.sqlalchemy_models import (
    ExecutionFillModelV2,
    ExecutionOrderModel,
    FillEventModel,
    OrderStateModel,
)
from tests.support.postgres import temporary_postgres_runtime, temporary_postgres_url


def _order_state(*, client_order_id: str, status: str = "SUBMITTED") -> OrderState:
    now = utc_now()
    return OrderState(
        decision_id=f"decision_{client_order_id}",
        intent_id=f"intent_{client_order_id}",
        symbol="BTC-USDT-SWAP",
        client_order_id=client_order_id,
        venue="OKX",
        exchange_order_id=f"ord_{client_order_id}",
        status=status,  # type: ignore[arg-type]
        submission_mode="financial_convergence",
        submitted_ts=now,
        last_update_ts=now,
        requested_qty=Decimal("0.010000000000000000"),
        filled_qty=Decimal("0"),
        remaining_qty=Decimal("0.010000000000000000"),
        average_fill_price=None,
        fees=Decimal("0"),
        reduce_only=True,
        close_only=True,
        td_mode="cross",
        position_mode="long_short_mode",
        pos_side="long",
        reduce_only_reason="position_intent_close_path",
        close_only_reason="position_intent_close_path",
        instrument_family="BTC-USDT",
        settle_currency="USDT",
        product_type="derivatives",
        margin_mode="cross",
        exposure_side="flat",
        position_intent="close_long",
        submission_payload={"instId": "BTC-USDT-SWAP", "tdMode": "cross", "posSide": "long"},
    )


def _fill(*, client_order_id: str, fill_id: str) -> FillEvent:
    now = utc_now()
    return FillEvent(
        fill_id=fill_id,
        decision_id=f"decision_{client_order_id}",
        intent_id=f"intent_{client_order_id}",
        client_order_id=client_order_id,
        exchange_order_id=f"ord_{client_order_id}",
        symbol="BTC-USDT-SWAP",
        venue="OKX",
        side="sell",
        fill_qty=Decimal("0.010000000000000000"),
        fill_price=Decimal("100.000000000000000000"),
        fee_amount=Decimal("0"),
        fee_currency="USDT",
        reduce_only=True,
        close_only=True,
        td_mode="cross",
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
        liquidity_role="taker",
        exchange_timestamp=now,
        ingestion_timestamp=now,
        order_status_after_fill="FILLED",
    )


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestTask58ConvergedExecutionTruth(unittest.IsolatedAsyncioTestCase):
    async def test_financial_convergence_mode_builds_runtime_with_converged_execution_repo(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            runtime = await build_runtime(
                AATSSettings.model_validate(
                    {
                        "config_profile": "local_demo",
                        "mode": "paper_live",
                        "market_data_backend": "demo",
                        "execution_backend": "paper",
                        "account_backend": "disabled",
                        "account_read_enabled": False,
                        "storage_mode": "postgres",
                        "database_url": database_url,
                        "database_auto_create_schema": True,
                        "database_single_runtime_guard_enabled": True,
                        "database_runtime_lock_key": 42_420_581,
                        "event_persistence_mode": "strict",
                        "execution_command_flow_enabled": True,
                        "portfolio_ledger_truth_enabled": True,
                        "recovery_reconciliation_execution_ledger_enabled": True,
                        "operator_control_plane_execution_ledger_enabled": True,
                        "financial_convergence_mode_enabled": True,
                    }
                )
            )
            try:
                self.assertIsInstance(runtime.execution_repo, ConvergedPostgresExecutionRepository)
                self.assertIsNone(runtime.phase1_execution_shadow_service)
                self.assertIsNotNone(runtime.phase1_ledger_mirror_service)
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

    async def test_financial_convergence_mode_keeps_reservations_and_settlements_in_sync(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            runtime = await build_runtime(
                AATSSettings.model_validate(
                    {
                        "config_profile": "local_demo",
                        "mode": "paper_live",
                        "market_data_backend": "demo",
                        "execution_backend": "paper",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "storage_mode": "postgres",
                        "database_url": database_url,
                        "database_auto_create_schema": True,
                        "database_single_runtime_guard_enabled": True,
                        "database_runtime_lock_key": 42_420_582,
                        "event_persistence_mode": "strict",
                        "execution_command_flow_enabled": True,
                        "portfolio_ledger_truth_enabled": True,
                        "recovery_reconciliation_execution_ledger_enabled": True,
                        "operator_control_plane_execution_ledger_enabled": True,
                        "financial_convergence_mode_enabled": True,
                    }
                )
            )
            try:
                runtime.account_service._latest_snapshot = ExchangeAccountSnapshot(
                    account_source="okx",
                    fetched_at=utc_now(),
                    balances=[
                        ExchangeBalance(
                            currency="USDT",
                            total=Decimal("1000"),
                            available=Decimal("1000"),
                            frozen=Decimal("0"),
                        )
                    ],
                )
                seeded_snapshot = runtime.market_gateway.normalizer.normalize(
                    runtime.market_gateway._build_local_payload(runtime.settings.default_symbol)  # type: ignore[attr-defined]
                )
                runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = seeded_snapshot  # type: ignore[attr-defined]
                runtime.market_gateway._latest_received_at[runtime.settings.default_symbol] = utc_now()  # type: ignore[attr-defined]
                intent = OrderIntent(
                    intent_id="intent_task58_flow",
                    decision_id="decision_task58_flow",
                    symbol="BTC-USDT",
                    side="buy",
                    quantity=Decimal("0.001"),
                    execution_style="exchange",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=False,
                    close_only=False,
                    idempotency_key="task58_flow",
                )
                runtime.audit_repo.upsert(
                    DecisionAuditRecord(
                        decision_id=intent.decision_id,
                        decision_context_ref="evt_task58_flow",
                    )
                )

                await runtime.order_manager.handle_order_intent(
                    {
                        "topic": topics.ORDER_INTENTS,
                        "key": intent.symbol,
                        "payload": build_envelope(
                            topic=topics.ORDER_INTENTS,
                            key=intent.symbol,
                            payload_model=intent,
                            source_component="test",
                        ).model_dump(mode="json"),
                    }
                )
                self.assertIsNotNone(runtime.execution_command_processor)
                await runtime.execution_command_processor.process_pending()

                reservation = runtime.reservation_repo_v2.get_by_order_id("cltask58_flow")
                self.assertIsNotNone(reservation)
                self.assertEqual(str(reservation["state"]), "RELEASED")
                fills = runtime.execution_repo.fills_for_order("cltask58_flow")
                self.assertEqual(len(fills), 1)
                settlement = runtime.settlement_repo.get_by_fill_id(fills[0].fill_id)
                self.assertIsNotNone(settlement)
                self.assertEqual(str(settlement["state"]), "POSTED")
            finally:
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()

    def test_converged_repo_persists_new_execution_truth_without_legacy_tables(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                repo = ConvergedPostgresExecutionRepository(
                    runtime.session_factory,
                    execution_order_repo=PostgresExecutionOrderRepository(runtime.session_factory),
                    execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                    execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
                )

                state = _order_state(client_order_id="cl_task58_repo")
                saved_state = repo.save_order_state(state)
                saved_fill = repo.save_fill(_fill(client_order_id=state.client_order_id, fill_id="fill_task58_repo"))

                self.assertEqual(saved_state.status, "SUBMITTED")
                self.assertTrue(saved_fill)
                hydrated_state = repo.get_order_state(state.client_order_id)
                self.assertIsNotNone(hydrated_state)
                self.assertEqual(hydrated_state.td_mode, "cross")
                self.assertEqual(hydrated_state.position_mode, "long_short_mode")
                self.assertEqual(hydrated_state.pos_side, "long")
                self.assertTrue(hydrated_state.reduce_only)
                self.assertEqual(hydrated_state.instrument_family, "BTC-USDT")
                hydrated_fills = repo.fills_for_order(state.client_order_id)
                self.assertEqual(len(hydrated_fills), 1)
                self.assertEqual(hydrated_fills[0].settle_currency, "USDT")
                self.assertTrue(hydrated_fills[0].close_only)

                with runtime.session_factory() as session:
                    stored_order = session.get(ExecutionOrderModel, state.client_order_id)
                    stored_fill = session.get(ExecutionFillModelV2, "fill_task58_repo")
                    self.assertIsNotNone(stored_order)
                    self.assertIsNotNone(stored_fill)
                    self.assertEqual(stored_order.td_mode, "cross")
                    self.assertEqual(stored_order.position_mode, "long_short_mode")
                    self.assertEqual(stored_order.pos_side, "long")
                    self.assertEqual(stored_order.instrument_family, "BTC-USDT")
                    self.assertEqual(stored_fill.settle_currency, "USDT")
                    self.assertTrue(stored_fill.reduce_only)
                    self.assertEqual(session.scalar(select(func.count()).select_from(ExecutionOrderModel)), 1)
                    self.assertEqual(session.scalar(select(func.count()).select_from(ExecutionFillModelV2)), 1)
                    self.assertEqual(session.scalar(select(func.count()).select_from(OrderStateModel)), 0)
                    self.assertEqual(session.scalar(select(func.count()).select_from(FillEventModel)), 0)

    def test_converged_repo_uses_in_session_order_lookup_during_transactional_writes(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                execution_order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
                execution_order_repo.get_order_by_client_order_id = Mock(  # type: ignore[method-assign]
                    side_effect=AssertionError("cross_session_lookup_used")
                )
                repo = ConvergedPostgresExecutionRepository(
                    runtime.session_factory,
                    execution_order_repo=execution_order_repo,
                    execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                    execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
                )

                state = repo.save_order_state(_order_state(client_order_id="cl_task58_in_session"))
                saved_fill = repo.save_fill(_fill(client_order_id=state.client_order_id, fill_id="fill_task58_in_session"))

                self.assertEqual(state.status, "SUBMITTED")
                self.assertTrue(saved_fill)

    def test_converged_repo_fill_backfill_aggregates_fill_truth_into_order_state(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                repo = ConvergedPostgresExecutionRepository(
                    runtime.session_factory,
                    execution_order_repo=PostgresExecutionOrderRepository(runtime.session_factory),
                    execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                    execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
                )

                first_fill = _fill(client_order_id="cl_task58_fill_first", fill_id="fill_task58_fill_first_1").model_copy(
                    update={
                        "fill_qty": Decimal("0.004000000000000000"),
                        "order_status_after_fill": "PARTIALLY_FILLED",
                    }
                )
                second_fill = _fill(client_order_id="cl_task58_fill_first", fill_id="fill_task58_fill_first_2").model_copy(
                    update={
                        "fill_qty": Decimal("0.006000000000000000"),
                        "order_status_after_fill": "FILLED",
                    }
                )

                self.assertTrue(repo.save_fill(first_fill))
                self.assertTrue(repo.save_fill(second_fill))

                hydrated_state = repo.get_order_state("cl_task58_fill_first")
                self.assertIsNotNone(hydrated_state)
                assert hydrated_state is not None
                self.assertEqual(hydrated_state.status, "FILLED")
                self.assertEqual(hydrated_state.filled_qty, Decimal("0.010000000000000000"))
                self.assertEqual(hydrated_state.remaining_qty, Decimal("0"))
                self.assertEqual(hydrated_state.average_fill_price, Decimal("100.000000000000000000"))
                self.assertEqual(hydrated_state.fees, Decimal("0"))

    def test_converged_repo_fill_backfill_converts_base_fee_into_quote_cost_for_order_state(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                repo = ConvergedPostgresExecutionRepository(
                    runtime.session_factory,
                    execution_order_repo=PostgresExecutionOrderRepository(runtime.session_factory),
                    execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                    execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
                )

                fill = _fill(client_order_id="cl_task58_quote_fee", fill_id="fill_task58_quote_fee").model_copy(
                    update={
                        "symbol": "BTC-USDT",
                        "side": "buy",
                        "fill_qty": Decimal("1"),
                        "fill_price": Decimal("100"),
                        "fee_amount": Decimal("0.001"),
                        "fee_currency": "BTC",
                        "product_type": "spot",
                        "margin_mode": "cash",
                        "settle_currency": None,
                    }
                )

                self.assertTrue(repo.save_fill(fill))

                hydrated_state = repo.get_order_state("cl_task58_quote_fee")
                self.assertIsNotNone(hydrated_state)
                assert hydrated_state is not None
                self.assertEqual(hydrated_state.fees, Decimal("0.100"))

    async def test_outbox_publisher_failure_injection_keeps_new_truth_and_leaves_pending_outbox(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                execution_repo = ConvergedPostgresExecutionRepository(
                    runtime.session_factory,
                    execution_order_repo=PostgresExecutionOrderRepository(runtime.session_factory),
                    execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                    execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
                )
                bus = InMemoryEventBus()

                async def exploding_handler(message: dict) -> None:
                    raise RuntimeError("task58_subscriber_boom")

                await bus.subscribe(topics.ORDER_UPDATES, exploding_handler)
                publisher = PostgresExecutionOutboxPublisher(
                    session_factory=runtime.session_factory,
                    event_store=PostgresEventStore(runtime.session_factory),
                    execution_repo=execution_repo,
                    obligation_repo=PostgresExecutionObligationRepository(runtime.session_factory),
                    outbox_repo=PostgresOutboxRepository(runtime.session_factory),
                    bus=bus,
                )

                persisted = await publisher.persist_order_state(
                    order_state=_order_state(client_order_id="cl_task58_outbox"),
                    key="BTC-USDT",
                )

                self.assertEqual(persisted.status, "SUBMITTED")
                self.assertEqual(publisher.outbox_repo.counts(), {"pending": 1, "published": 0, "failed": 0})
                with runtime.session_factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(ExecutionOrderModel)), 1)
                    self.assertEqual(session.scalar(select(func.count()).select_from(OrderStateModel)), 0)


if __name__ == "__main__":
    unittest.main()
