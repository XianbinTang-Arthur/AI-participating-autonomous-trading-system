from __future__ import annotations

import os
import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.exchange import ExchangeAccountSnapshot, ExchangeBalance
from aats.schemas.execution import OrderIntent
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
from tests.support.postgres import temporary_postgres_runtime


async def _snapshot_loader() -> ExchangeAccountSnapshot:
    return ExchangeAccountSnapshot(
        account_source="test",
        fetched_at=utc_now(),
        balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("1000"))],
    )


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestTask43ShadowObservability(unittest.IsolatedAsyncioTestCase):
    async def test_phase1_shadow_services_publish_runtime_status_after_order_flow(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            execution_shadow = Phase1ExecutionShadowService(
                execution_order_repo=PostgresExecutionOrderRepository(runtime.session_factory),
                execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
            )
            ledger_shadow = Phase1LedgerMirrorService(
                reservation_repo=PostgresReservationRepository(runtime.session_factory),
                ledger_account_repo=PostgresLedgerAccountRepository(runtime.session_factory),
                ledger_journal_repo=PostgresLedgerJournalRepository(runtime.session_factory),
                ledger_entry_repo=PostgresLedgerEntryRepository(runtime.session_factory),
                settlement_repo=PostgresSettlementRepository(runtime.session_factory),
            )
            manager = OrderManager(
                settings=AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True}),
                bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
                adapter=PaperExecutionAdapter(price_provider=lambda _symbol: Decimal("100"), taker_fee_bps=5.0),
                execution_repo=InMemoryExecutionRepository(),
                obligation_service=ExecutionObligationService(
                    settings=AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True}),
                    obligation_repo=InMemoryExecutionObligationRepository(),
                    account_snapshot_loader=_snapshot_loader,
                    price_provider=lambda _symbol: Decimal("100"),
                ),
                shadow_execution_service=execution_shadow,
                shadow_ledger_mirror_service=ledger_shadow,
                kill_switch=KillSwitch(),
            )
            intent = OrderIntent(
                intent_id="intent_task43_shadow",
                decision_id="decision_task43_shadow",
                symbol="BTC-USDT",
                side="buy",
                quantity=Decimal("0.001"),
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="task43_shadow",
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

            execution_snapshot = execution_shadow.snapshot()
            self.assertEqual(execution_snapshot["status"], "healthy")
            self.assertGreaterEqual(int(execution_snapshot["order_success_count"]), 3)
            self.assertGreaterEqual(int(execution_snapshot["fill_success_count"]), 1)
            self.assertEqual(execution_snapshot["last_synced_order_state"], "FILLED")
            self.assertIsNotNone(execution_snapshot["last_synced_fill_id"])

            ledger_snapshot = ledger_shadow.snapshot()
            self.assertEqual(ledger_snapshot["status"], "healthy")
            self.assertGreaterEqual(int(ledger_snapshot["sync_success_count"]), 2)
            self.assertEqual(ledger_snapshot["last_reason"], "reservation_release")
            self.assertEqual(ledger_snapshot["last_obligation_status"], "RELEASED")
            self.assertEqual(ledger_snapshot["last_synced_order_id"], "cltask43_shadow")
