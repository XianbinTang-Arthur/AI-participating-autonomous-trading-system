from __future__ import annotations

import os
import unittest
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation
from aats.services.execution_engine.obligations import ExecutionObligationService
from aats.services.ledger.settlement_posting import FillSettlementProjection, LedgerSettlementPostingService
from aats.storage.execution_order_repo_postgres import PostgresExecutionOrderRepository
from aats.storage.ledger_repo_postgres import (
    PostgresLedgerAccountRepository,
    PostgresLedgerEntryRepository,
    PostgresLedgerJournalRepository,
)
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository
from aats.storage.reservation_repo_postgres import PostgresReservationRepository
from tests.support.postgres import temporary_postgres_runtime


class TestTask109ExecutionObligationRebate(unittest.TestCase):
    def test_quote_backed_spot_buy_rebate_reduces_reserved_consumption(self) -> None:
        obligation_repo = InMemoryExecutionObligationRepository()
        service = ExecutionObligationService(
            settings=AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True}),
            obligation_repo=obligation_repo,
        )
        now = utc_now()
        obligation = OrderObligation(
            obligation_id="obl_task109_quote_rebate",
            client_order_id="cltask109_quote_rebate",
            decision_id="decision_task109_quote_rebate",
            intent_id="intent_task109_quote_rebate",
            symbol="BTC-USDT",
            side="buy",
            reserve_currency="USDT",
            reserved_amount=Decimal("100.5"),
            consumed_amount=Decimal("0"),
            released_amount=Decimal("0"),
            status="ACTIVE",
            product_type="spot",
            margin_mode="cash",
            reference_price=Decimal("100"),
            last_update_ts=now,
            created_at=now,
        )
        obligation_repo.save_obligation(obligation)
        fill = FillEvent(
            fill_id="fill_task109_quote_rebate",
            decision_id=obligation.decision_id,
            intent_id=obligation.intent_id,
            client_order_id=obligation.client_order_id,
            exchange_order_id="ord_task109_quote_rebate",
            symbol=obligation.symbol,
            venue="OKX",
            side="buy",
            fill_qty=Decimal("1"),
            fill_price=Decimal("100"),
            fee_amount=Decimal("-1"),
            fee_currency="USDT",
            product_type="spot",
            margin_mode="cash",
            liquidity_role="maker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
        )

        updated = service.preview_obligation_for_fill(fill)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.consumed_amount, Decimal("99"))
        self.assertEqual(updated.status, "PARTIALLY_CONSUMED")
        self.assertEqual(updated.consumed_fill_ids, [fill.fill_id])

    def test_base_backed_spot_sell_base_fee_consumes_reserved_inventory(self) -> None:
        obligation_repo = InMemoryExecutionObligationRepository()
        service = ExecutionObligationService(
            settings=AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True}),
            obligation_repo=obligation_repo,
        )
        now = utc_now()
        obligation = OrderObligation(
            obligation_id="obl_task109_base_fee_sell",
            client_order_id="cltask109_base_fee_sell",
            decision_id="decision_task109_base_fee_sell",
            intent_id="intent_task109_base_fee_sell",
            symbol="BTC-USDT",
            side="sell",
            reserve_currency="BTC",
            reserved_amount=Decimal("1.01"),
            consumed_amount=Decimal("0"),
            released_amount=Decimal("0"),
            status="ACTIVE",
            product_type="spot",
            margin_mode="cash",
            reference_price=Decimal("100"),
            last_update_ts=now,
            created_at=now,
        )
        obligation_repo.save_obligation(obligation)
        fill = FillEvent(
            fill_id="fill_task109_base_fee_sell",
            decision_id=obligation.decision_id,
            intent_id=obligation.intent_id,
            client_order_id=obligation.client_order_id,
            exchange_order_id="ord_task109_base_fee_sell",
            symbol=obligation.symbol,
            venue="TEST",
            side="sell",
            fill_qty=Decimal("1"),
            fill_price=Decimal("100"),
            fee_amount=Decimal("0.01"),
            fee_currency="BTC",
            product_type="spot",
            margin_mode="cash",
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
        )

        updated = service.preview_obligation_for_fill(fill)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.consumed_amount, Decimal("1.01"))
        self.assertEqual(updated.status, "RELEASED")

    def test_okx_spot_sell_missing_fee_currency_defaults_to_quote_and_does_not_overconsume_base(self) -> None:
        obligation_repo = InMemoryExecutionObligationRepository()
        service = ExecutionObligationService(
            settings=AATSSettings.model_validate({"account_backend": "okx", "account_read_enabled": True}),
            obligation_repo=obligation_repo,
        )
        now = utc_now()
        obligation = OrderObligation(
            obligation_id="obl_task109_sell_missing_fee_ccy",
            client_order_id="cltask109_sell_missing_fee_ccy",
            decision_id="decision_task109_sell_missing_fee_ccy",
            intent_id="intent_task109_sell_missing_fee_ccy",
            symbol="BTC-USDT",
            side="sell",
            reserve_currency="BTC",
            reserved_amount=Decimal("1.01"),
            consumed_amount=Decimal("0"),
            released_amount=Decimal("0"),
            status="ACTIVE",
            product_type="spot",
            margin_mode="cash",
            reference_price=Decimal("100"),
            last_update_ts=now,
            created_at=now,
        )
        obligation_repo.save_obligation(obligation)
        fill = FillEvent(
            fill_id="fill_task109_sell_missing_fee_ccy",
            decision_id=obligation.decision_id,
            intent_id=obligation.intent_id,
            client_order_id=obligation.client_order_id,
            exchange_order_id="ord_task109_sell_missing_fee_ccy",
            symbol=obligation.symbol,
            venue="OKX",
            side="sell",
            fill_qty=Decimal("1"),
            fill_price=Decimal("100"),
            fee_amount=Decimal("0.5"),
            fee_currency=None,
            product_type="spot",
            margin_mode="cash",
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
        )

        updated = service.preview_obligation_for_fill(fill)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.consumed_amount, Decimal("1"))
        self.assertEqual(updated.status, "PARTIALLY_CONSUMED")


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for PostgreSQL-backed tests")
class TestTask109SettlementPostingRebate(unittest.TestCase):
    def test_derivatives_rebate_posts_income_and_increases_available_balance(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_journal_repo = PostgresLedgerJournalRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            service = LedgerSettlementPostingService(
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=ledger_journal_repo,
                ledger_entry_repo=ledger_entry_repo,
            )
            service.ensure_initial_balance(
                currency="USDT",
                amount=Decimal("1000"),
                product_type="derivatives",
                margin_mode="cross",
            )
            now = utc_now()
            fill = FillEvent(
                fill_id="fill_task109_derivatives_rebate",
                decision_id="decision_task109_derivatives_rebate",
                intent_id="intent_task109_derivatives_rebate",
                client_order_id="cltask109_derivatives_rebate",
                exchange_order_id="ord_task109_derivatives_rebate",
                symbol="BTC-USDT-SWAP",
                venue="OKX",
                side="sell",
                fill_qty=Decimal("0.001"),
                fill_price=Decimal("100000"),
                fee_amount=Decimal("-0.2"),
                fee_currency="USDT",
                product_type="derivatives",
                target_leverage=5,
                margin_mode="cross",
                liquidity_role="maker",
                exchange_timestamp=now,
                ingestion_timestamp=now,
            )
            projection = FillSettlementProjection(
                base_currency="BTC",
                quote_currency="USDT",
                starting_quantity=Decimal("0"),
                ending_quantity=Decimal("-0.001"),
                realized_pnl_delta=Decimal("10"),
                fee_delta=Decimal("-0.2"),
            )

            service.post_fill_effects(fill=fill, projection=projection)

            self.assertEqual(
                service.available_balances(product_type="derivatives", margin_mode="cross")["USDT"],
                Decimal("1010.2"),
            )
            fee_source_id = service._stable_id("fill_fee", fill.fill_id, "USDT")
            journal = ledger_journal_repo.get_by_source("fill_fee", fee_source_id)
            self.assertIsNotNone(journal)
            assert journal is not None
            self.assertEqual(journal["journal_type"], "fill_fee_rebate")
            fee_income_account_id = ledger_account_repo.get_or_create_account(
                account_type="fee_income",
                currency="USDT",
                product_type="derivatives",
                margin_mode="cross",
                symbol=None,
                created_at=now,
            )
            self.assertEqual(ledger_entry_repo.balance_by_account(fee_income_account_id), Decimal("-0.2"))

    def test_okx_spot_buy_missing_fee_currency_defaults_to_base_for_fee_journal(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_journal_repo = PostgresLedgerJournalRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            service = LedgerSettlementPostingService(
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=ledger_journal_repo,
                ledger_entry_repo=ledger_entry_repo,
            )
            now = utc_now()
            fill = FillEvent(
                fill_id="fill_task109_missing_fee_currency_buy",
                decision_id="decision_task109_missing_fee_currency_buy",
                intent_id="intent_task109_missing_fee_currency_buy",
                client_order_id="cltask109_missing_fee_currency_buy",
                exchange_order_id="ord_task109_missing_fee_currency_buy",
                symbol="BTC-USDT",
                venue="OKX",
                side="buy",
                fill_qty=Decimal("1"),
                fill_price=Decimal("100"),
                fee_amount=Decimal("0.001"),
                fee_currency=None,
                product_type="spot",
                margin_mode="cash",
                liquidity_role="taker",
                exchange_timestamp=now,
                ingestion_timestamp=now,
            )
            projection = FillSettlementProjection(
                base_currency="BTC",
                quote_currency="USDT",
                starting_quantity=Decimal("0"),
                ending_quantity=Decimal("1"),
                realized_pnl_delta=Decimal("0"),
                fee_delta=Decimal("0.1"),
            )

            service.post_fill_effects(fill=fill, projection=projection)

            balances = service.available_balances(product_type="spot", margin_mode="cash")
            self.assertEqual(balances["BTC"], Decimal("0.999"))
            self.assertEqual(balances["USDT"], Decimal("-100"))
            fee_source_id = service._stable_id("fill_fee", fill.fill_id, "BTC")
            journal = ledger_journal_repo.get_by_source("fill_fee", fee_source_id)
            self.assertIsNotNone(journal)
            assert journal is not None
            self.assertEqual(journal["journal_type"], "fill_fee")

    def test_reservation_backed_spot_sell_base_fee_is_marked_as_reservation_covered(self) -> None:
        with temporary_postgres_runtime() as (runtime, _admin_engine, _schema_name):
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_journal_repo = PostgresLedgerJournalRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            reservation_repo = PostgresReservationRepository(runtime.session_factory)
            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            service = LedgerSettlementPostingService(
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=ledger_journal_repo,
                ledger_entry_repo=ledger_entry_repo,
                reservation_repo=reservation_repo,
            )
            now = utc_now()
            intent = OrderIntent(
                intent_id="intent_task109_sell_reservation_covered",
                decision_id="decision_task109_sell_reservation_covered",
                symbol="BTC-USDT",
                side="sell",
                quantity=Decimal("1"),
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="task109_sell_reservation_covered",
            )
            order_repo.create_order(
                order_id="cltask109_sell_reservation_covered",
                intent=intent,
                initial_state="CREATED",
                created_at=now,
                raw_payload={"client_order_id": "cltask109_sell_reservation_covered", "source_system": "test"},
            )
            reserve_account_id = ledger_account_repo.get_or_create_account(
                account_type="cash_reserved",
                currency="BTC",
                product_type="spot",
                margin_mode="cash",
                symbol=None,
                created_at=now,
            )
            reservation_repo.create_reservation(
                reservation_id="res_task109_sell_reservation_covered",
                order_id="cltask109_sell_reservation_covered",
                reserve_account_id=reserve_account_id,
                reserved_amount=Decimal("1.01"),
                state="ACTIVE",
                created_at=now,
            )
            fill = FillEvent(
                fill_id="fill_task109_sell_reservation_covered",
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                client_order_id="cltask109_sell_reservation_covered",
                exchange_order_id="ord_task109_sell_reservation_covered",
                symbol="BTC-USDT",
                venue="TEST",
                side="sell",
                fill_qty=Decimal("1"),
                fill_price=Decimal("100"),
                fee_amount=Decimal("0.01"),
                fee_currency="BTC",
                product_type="spot",
                margin_mode="cash",
                liquidity_role="taker",
                exchange_timestamp=now,
                ingestion_timestamp=now,
            )
            projection = FillSettlementProjection(
                base_currency="BTC",
                quote_currency="USDT",
                starting_quantity=Decimal("1.01"),
                ending_quantity=Decimal("0"),
                realized_pnl_delta=Decimal("0"),
                fee_delta=Decimal("1"),
            )

            service.post_fill_effects(fill=fill, projection=projection)

            fee_source_id = service._stable_id("fill_fee", fill.fill_id, "BTC")
            journal = ledger_journal_repo.get_by_source("fill_fee", fee_source_id)
            self.assertIsNone(journal)
