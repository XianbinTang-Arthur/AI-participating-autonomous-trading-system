from __future__ import annotations

import os
import unittest
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError as SAOperationalError

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance
from aats.schemas.execution import OrderIntent, OrderObligation, OrderState
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.execution_engine.obligations import ExecutionObligationService
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.ledger.posting import Phase1LedgerMirrorService
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.ledger_repo_postgres import (
    PostgresLedgerAccountRepository,
    PostgresLedgerEntryRepository,
    PostgresLedgerJournalRepository,
    PostgresSettlementRepository,
)
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository
from aats.storage.reservation_repo_postgres import PostgresReservationRepository
from aats.storage.sqlalchemy_models import LedgerAccountModel, LedgerJournalModel, SettlementModel
from tests.support.postgres import temporary_postgres_runtime


async def _snapshot_loader() -> ExchangeAccountSnapshot:
    return ExchangeAccountSnapshot(
        account_source="test",
        fetched_at=utc_now(),
        balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"))],
    )


class _PreviewingFailingAdapter:
    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return f"cl{intent.idempotency_key}"

    async def submit(self, intent: OrderIntent):
        now = utc_now()
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=f"cl{intent.idempotency_key}",
            venue="OKX",
            exchange_order_id=None,
            status="FAILED",
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
            position_intent=intent.position_intent,
            execution_error="simulated_failure",
            submission_payload={},
        )
        return state, []

    async def sync(self, open_order_states):
        return [], []

    async def cancel(self, order_state: OrderState):
        return order_state, []

    def readiness(self):
        return {"backend": "okx", "exchange_submit_allowed": False, "submit_blocked_reasons": ["simulated_failure"]}


class _ExplodingLedgerEntryRepository(PostgresLedgerEntryRepository):
    def append_entries_in_session(self, session, *, entries: list[dict]) -> None:  # type: ignore[override]
        raise RuntimeError("ledger_entry_append_failed")


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestTask40LedgerMirror(unittest.IsolatedAsyncioTestCase):
    async def test_fill_consumption_is_mirrored_to_reservation_settlement_and_ledger(self) -> None:
        try:
            with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                settings = AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True})
                obligation_repo = InMemoryExecutionObligationRepository()
                ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
                ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
                phase1_ledger_mirror_service = Phase1LedgerMirrorService(
                    reservation_repo=PostgresReservationRepository(runtime.session_factory),
                    ledger_account_repo=ledger_account_repo,
                    ledger_journal_repo=PostgresLedgerJournalRepository(runtime.session_factory),
                    ledger_entry_repo=ledger_entry_repo,
                    settlement_repo=PostgresSettlementRepository(runtime.session_factory),
                )
                phase1_execution_shadow_service = Phase1ExecutionShadowService(
                    execution_order_repo=PostgresExecutionOrderRepository(runtime.session_factory),
                    execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                    execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
                )
                manager = OrderManager(
                    settings=settings,
                    bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
                    adapter=PaperExecutionAdapter(price_provider=lambda _symbol: Decimal("100"), taker_fee_bps=5.0),
                    execution_repo=InMemoryExecutionRepository(),
                    obligation_service=ExecutionObligationService(
                        settings=settings,
                        obligation_repo=obligation_repo,
                        account_snapshot_loader=_snapshot_loader,
                        price_provider=lambda _symbol: Decimal("100"),
                    ),
                    shadow_execution_service=phase1_execution_shadow_service,
                    shadow_ledger_mirror_service=phase1_ledger_mirror_service,
                    kill_switch=KillSwitch(),
                )
                intent = OrderIntent(
                    intent_id="intent_task40_fill",
                    decision_id="decision_task40_fill",
                    symbol="BTC-USDT",
                    side="buy",
                    quantity=Decimal("0.001"),
                    execution_style="exchange",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=False,
                    close_only=False,
                    idempotency_key="task40_fill",
                )

                await manager.handle_order_intent(
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

                reservation_repo = PostgresReservationRepository(runtime.session_factory)
                reservation = reservation_repo.get_by_order_id("cltask40_fill")
                self.assertIsNotNone(reservation)
                self.assertEqual(Decimal(str(reservation["reserved_amount"])), Decimal("0.100100000000000000"))
                self.assertEqual(Decimal(str(reservation["consumed_amount"])), Decimal("0.100050000000000000"))
                self.assertEqual(Decimal(str(reservation["released_amount"])), Decimal("0.000050000000000000"))
                self.assertEqual(reservation["state"], "RELEASED")

                settlement_repo = PostgresSettlementRepository(runtime.session_factory)
                fill_row = PostgresExecutionFillRepositoryV2(runtime.session_factory).fills_for_order("cltask40_fill")[0]
                settlement = settlement_repo.get_by_fill_id(fill_row["fill_id"])
                self.assertIsNotNone(settlement)
                self.assertEqual(settlement["state"], "POSTED")

                available_account_id = ledger_account_repo.get_or_create_account(
                    account_type="cash_available",
                    currency="USDT",
                    product_type="spot",
                    margin_mode="cash",
                    symbol=None,
                    created_at=utc_now(),
                )
                external_account_id = ledger_account_repo.get_or_create_account(
                    account_type="external_clearing",
                    currency="USDT",
                    product_type="spot",
                    margin_mode="cash",
                    symbol=None,
                    created_at=utc_now(),
                )
                fee_expense_account_id = ledger_account_repo.get_or_create_account(
                    account_type="fee_expense",
                    currency="USDT",
                    product_type="spot",
                    margin_mode="cash",
                    symbol=None,
                    created_at=utc_now(),
                )
                self.assertEqual(ledger_entry_repo.balance_by_account(str(reservation["reserve_account_id"])), Decimal("0"))
                self.assertEqual(
                    ledger_entry_repo.balance_by_account(available_account_id),
                    Decimal("-0.100050000000000000"),
                )
                self.assertEqual(
                    ledger_entry_repo.balance_by_account(external_account_id),
                    Decimal("0.100000000000000000"),
                )
                self.assertEqual(
                    ledger_entry_repo.balance_by_account(fee_expense_account_id),
                    Decimal("0.000050000000000000"),
                )

                with runtime.session_factory() as session:
                    journal_count = session.scalar(select(func.count()).select_from(LedgerJournalModel))
                    settlement_count = session.scalar(select(func.count()).select_from(SettlementModel))
                self.assertEqual(journal_count, 4)
                self.assertEqual(settlement_count, 1)
        except SAOperationalError:
            self.skipTest("Postgres 不可达")

    async def test_failed_terminal_order_releases_shadow_reservation_back_to_available(self) -> None:
        try:
            with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                settings = AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True})
                obligation_repo = InMemoryExecutionObligationRepository()
                ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
                ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
                phase1_ledger_mirror_service = Phase1LedgerMirrorService(
                    reservation_repo=PostgresReservationRepository(runtime.session_factory),
                    ledger_account_repo=ledger_account_repo,
                    ledger_journal_repo=PostgresLedgerJournalRepository(runtime.session_factory),
                    ledger_entry_repo=ledger_entry_repo,
                    settlement_repo=PostgresSettlementRepository(runtime.session_factory),
                )
                phase1_execution_shadow_service = Phase1ExecutionShadowService(
                    execution_order_repo=PostgresExecutionOrderRepository(runtime.session_factory),
                    execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                    execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
                )
                manager = OrderManager(
                    settings=settings,
                    bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
                    adapter=_PreviewingFailingAdapter(),
                    execution_repo=InMemoryExecutionRepository(),
                    obligation_service=ExecutionObligationService(
                        settings=settings,
                        obligation_repo=obligation_repo,
                        account_snapshot_loader=_snapshot_loader,
                        price_provider=lambda _symbol: Decimal("100"),
                    ),
                    shadow_execution_service=phase1_execution_shadow_service,
                    shadow_ledger_mirror_service=phase1_ledger_mirror_service,
                    kill_switch=KillSwitch(),
                )
                intent = OrderIntent(
                    intent_id="intent_task40_release",
                    decision_id="decision_task40_release",
                    symbol="BTC-USDT",
                    side="buy",
                    quantity=Decimal("0.001"),
                    execution_style="exchange",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=False,
                    close_only=False,
                    idempotency_key="task40_release",
                )

                await manager.handle_order_intent(
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

                reservation = PostgresReservationRepository(runtime.session_factory).get_by_order_id("cltask40_release")
                self.assertIsNotNone(reservation)
                self.assertEqual(Decimal(str(reservation["consumed_amount"])), Decimal("0"))
                self.assertEqual(Decimal(str(reservation["released_amount"])), Decimal("0.100100000000000000"))
                self.assertEqual(reservation["state"], "FAILED")

                available_account_id = ledger_account_repo.get_or_create_account(
                    account_type="cash_available",
                    currency="USDT",
                    product_type="spot",
                    margin_mode="cash",
                    symbol=None,
                    created_at=utc_now(),
                )
                self.assertEqual(ledger_entry_repo.balance_by_account(str(reservation["reserve_account_id"])), Decimal("0"))
                self.assertEqual(ledger_entry_repo.balance_by_account(available_account_id), Decimal("0"))

                with runtime.session_factory() as session:
                    journal_count = session.scalar(select(func.count()).select_from(LedgerJournalModel))
                    settlement_count = session.scalar(select(func.count()).select_from(SettlementModel))
                self.assertEqual(journal_count, 2)
                self.assertEqual(settlement_count, 0)
        except SAOperationalError:
            self.skipTest("Postgres 不可达")

    def test_ledger_mirror_rolls_back_reservation_and_journal_when_posting_fails(self) -> None:
        try:
            with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                now = utc_now()
                order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
                ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
                mirror_service = Phase1LedgerMirrorService(
                    reservation_repo=PostgresReservationRepository(runtime.session_factory),
                    ledger_account_repo=ledger_account_repo,
                    ledger_journal_repo=PostgresLedgerJournalRepository(runtime.session_factory),
                    ledger_entry_repo=_ExplodingLedgerEntryRepository(runtime.session_factory),
                    settlement_repo=PostgresSettlementRepository(runtime.session_factory),
                )
                order_repo.create_order(
                    order_id="cltask40_atomicity",
                    intent=OrderIntent(
                        intent_id="intent_task40_atomicity",
                        decision_id="decision_task40_atomicity",
                        symbol="BTC-USDT",
                        side="buy",
                        quantity=Decimal("0.001"),
                        execution_style="exchange",
                        order_type="market",
                        urgency="medium",
                        time_in_force="IOC",
                        reduce_only=False,
                        close_only=False,
                        idempotency_key="task40_atomicity",
                    ),
                    initial_state="CREATED",
                    created_at=now,
                    raw_payload={"client_order_id": "cltask40_atomicity", "source_system": "test"},
                )

                with self.assertRaisesRegex(RuntimeError, "ledger_entry_append_failed"):
                    mirror_service.sync_obligation(
                        OrderObligation(
                            obligation_id="obl_task40_atomicity",
                            client_order_id="cltask40_atomicity",
                            decision_id="decision_task40_atomicity",
                            intent_id="intent_task40_atomicity",
                            symbol="BTC-USDT",
                            side="buy",
                            reserve_currency="USDT",
                            reserved_amount=Decimal("100.000000000000000000"),
                            consumed_amount=Decimal("0"),
                            released_amount=Decimal("0"),
                            status="ACTIVE",
                            product_type="spot",
                            margin_mode="cash",
                            reference_price=Decimal("100"),
                            last_update_ts=now,
                            created_at=now,
                        ),
                        reason="reservation_hold",
                        related_fill=None,
                    )

                reservation_repo = PostgresReservationRepository(runtime.session_factory)
                self.assertIsNone(reservation_repo.get_by_order_id("cltask40_atomicity"))
                with runtime.session_factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(LedgerAccountModel)), 0)
                    self.assertEqual(session.scalar(select(func.count()).select_from(LedgerJournalModel)), 0)
                    self.assertEqual(session.scalar(select(func.count()).select_from(SettlementModel)), 0)
        except SAOperationalError:
            self.skipTest("Postgres 不可达")

    def test_reservation_repo_rejects_over_consume_and_over_release(self) -> None:
        try:
            with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
                now = utc_now()
                order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
                order_repo.create_order(
                    order_id="cltask40_invariants",
                    intent=OrderIntent(
                        intent_id="intent_task40_invariants",
                        decision_id="decision_task40_invariants",
                        symbol="BTC-USDT",
                        side="buy",
                        quantity=Decimal("0.001"),
                        execution_style="exchange",
                        order_type="market",
                        urgency="medium",
                        time_in_force="IOC",
                        reduce_only=False,
                        close_only=False,
                        idempotency_key="task40_invariants",
                    ),
                    initial_state="CREATED",
                    created_at=now,
                    raw_payload={"client_order_id": "cltask40_invariants", "source_system": "test"},
                )
                ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
                reserve_account_id = ledger_account_repo.get_or_create_account(
                    account_type="cash_reserved",
                    currency="USDT",
                    product_type="spot",
                    margin_mode="cash",
                    symbol=None,
                    created_at=now,
                )
                reservation_repo = PostgresReservationRepository(runtime.session_factory)
                reservation_repo.create_reservation(
                    reservation_id="rsv_task40_invariants",
                    order_id="cltask40_invariants",
                    reserve_account_id=reserve_account_id,
                    reserved_amount=Decimal("100"),
                    state="ACTIVE",
                    created_at=now,
                )
                reservation_repo.consume(
                    reservation_id="rsv_task40_invariants",
                    amount=Decimal("60"),
                    updated_at=now,
                )

                with self.assertRaisesRegex(ValueError, "reservation_over_consume"):
                    reservation_repo.consume(
                        reservation_id="rsv_task40_invariants",
                        amount=Decimal("50"),
                        updated_at=now,
                    )

                with self.assertRaisesRegex(ValueError, "reservation_over_release"):
                    reservation_repo.release(
                        reservation_id="rsv_task40_invariants",
                        amount=Decimal("41"),
                        next_state="RELEASED",
                        updated_at=now,
                    )
        except SAOperationalError:
            self.skipTest("Postgres 不可达")
