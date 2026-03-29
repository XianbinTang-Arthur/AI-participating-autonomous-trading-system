from __future__ import annotations

import os
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation
from aats.services.ledger.posting import Phase1LedgerMirrorService
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.ledger_repo_postgres import (
    PostgresLedgerAccountRepository,
    PostgresLedgerEntryRepository,
    PostgresLedgerJournalRepository,
    PostgresSettlementRepository,
)
from aats.storage.reservation_repo_postgres import PostgresReservationRepository
from aats.storage.session import create_database_runtime, validate_runtime_schema


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for Postgres integration tests")
class TestPhase1LedgerMirrorPostgres(unittest.TestCase):
    def test_phase1_repos_and_ledger_mirror_work_against_sql_migrations(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            validate_runtime_schema(runtime)

            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            order_history_repo = PostgresExecutionOrderHistoryRepository(runtime.session_factory)
            fill_repo = PostgresExecutionFillRepositoryV2(runtime.session_factory)
            reservation_repo = PostgresReservationRepository(runtime.session_factory)
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_journal_repo = PostgresLedgerJournalRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            settlement_repo = PostgresSettlementRepository(runtime.session_factory)
            mirror_service = Phase1LedgerMirrorService(
                reservation_repo=reservation_repo,
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=ledger_journal_repo,
                ledger_entry_repo=ledger_entry_repo,
                settlement_repo=settlement_repo,
            )

            intent = OrderIntent(
                intent_id="intent_phase1_pg",
                decision_id="decision_phase1_pg",
                symbol="BTC-USDT",
                side="buy",
                quantity=Decimal("0.010000000000000000"),
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase1_pg_order",
            )
            now = utc_now()
            order_repo.create_order(
                order_id="clphase1_pg_order",
                intent=intent,
                initial_state="CREATED",
                created_at=now,
                raw_payload={"client_order_id": "clphase1_pg_order", "source_system": "integration_test"},
            )
            order_history_repo.append_transition(
                order_id="clphase1_pg_order",
                from_state=None,
                to_state="CREATED",
                reason_code="integration_seed",
                source="test",
                source_message_id=intent.intent_id,
                payload={"status": "CREATED"},
                created_at=now,
            )
            order_repo.update_order_state(
                order_id="clphase1_pg_order",
                expected_state_version=1,
                next_state="PARTIALLY_FILLED",
                venue_order_id="ord_phase1_pg",
                last_exchange_ts=now,
                updated_at=now,
                raw_payload={"status": "PARTIALLY_FILLED", "client_order_id": "clphase1_pg_order"},
            )
            order_history_repo.append_transition(
                order_id="clphase1_pg_order",
                from_state="CREATED",
                to_state="PARTIALLY_FILLED",
                reason_code="integration_partial_fill",
                source="test",
                source_message_id=intent.intent_id,
                payload={"status": "PARTIALLY_FILLED"},
                created_at=now,
            )

            fill = FillEvent(
                fill_id="fill_phase1_pg_1",
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                client_order_id="clphase1_pg_order",
                exchange_order_id="ord_phase1_pg",
                symbol=intent.symbol,
                venue="OKX",
                side="buy",
                fill_qty=Decimal("0.004000000000000000"),
                fill_price=Decimal("100.000000000000000000"),
                fee_amount=Decimal("0"),
                fee_currency="USDT",
                product_type="spot",
                margin_mode="cash",
                liquidity_role="taker",
                exchange_timestamp=now,
                ingestion_timestamp=now,
                order_status_after_fill="PARTIALLY_FILLED",
            )
            saved = fill_repo.save_fill(
                fill=fill,
                order_id="clphase1_pg_order",
                source="okx",
                raw_payload={"venue_fill_id": "venue_fill_phase1_pg_1", "fill_event": fill.model_dump(mode="python")},
            )
            self.assertTrue(saved)

            hold_obligation = OrderObligation(
                obligation_id="obl_phase1_pg",
                client_order_id="clphase1_pg_order",
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
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
            )
            mirror_service.sync_obligation(hold_obligation, reason="reservation_hold", related_fill=None)

            partial_fill_obligation = hold_obligation.model_copy(
                update={
                    "consumed_amount": Decimal("40.000000000000000000"),
                    "status": "PARTIALLY_CONSUMED",
                    "last_update_ts": now,
                }
            )
            mirror_service.sync_obligation(
                partial_fill_obligation,
                reason="fill_settlement",
                related_fill=fill,
            )

            released_obligation = partial_fill_obligation.model_copy(
                update={
                    "released_amount": Decimal("60.000000000000000000"),
                    "status": "RELEASED",
                    "last_update_ts": now,
                }
            )
            mirror_service.sync_obligation(
                released_obligation,
                reason="reservation_release",
                related_fill=None,
            )

            shadow_order = order_repo.get_order_by_client_order_id("clphase1_pg_order")
            self.assertIsNotNone(shadow_order)
            self.assertEqual(shadow_order["state"], "PARTIALLY_FILLED")
            history = order_history_repo.history_for_order("clphase1_pg_order")
            self.assertEqual(len(history), 2)

            reservation = reservation_repo.get_by_order_id("clphase1_pg_order")
            self.assertIsNotNone(reservation)
            self.assertEqual(Decimal(str(reservation["reserved_amount"])), Decimal("100.000000000000000000"))
            self.assertEqual(Decimal(str(reservation["consumed_amount"])), Decimal("40.000000000000000000"))
            self.assertEqual(Decimal(str(reservation["released_amount"])), Decimal("60.000000000000000000"))
            self.assertEqual(str(reservation["state"]), "RELEASED")

            settlement = settlement_repo.get_by_fill_id(fill.fill_id)
            self.assertIsNotNone(settlement)
            self.assertEqual(str(settlement["state"]), "POSTED")

            available_account_id = ledger_account_repo.get_or_create_account(
                account_type="cash_available",
                currency="USDT",
                product_type="spot",
                margin_mode="cash",
                symbol=None,
                created_at=now,
            )
            reserved_account_id = str(reservation["reserve_account_id"])
            external_account_id = ledger_account_repo.get_or_create_account(
                account_type="external_clearing",
                currency="USDT",
                product_type="spot",
                margin_mode="cash",
                symbol=None,
                created_at=now,
            )
            self.assertEqual(ledger_entry_repo.balance_by_account(available_account_id), Decimal("-40.000000000000000000"))
            self.assertEqual(ledger_entry_repo.balance_by_account(reserved_account_id), Decimal("0"))
            self.assertEqual(ledger_entry_repo.balance_by_account(external_account_id), Decimal("40.000000000000000000"))

            settlement_id = Phase1LedgerMirrorService.stable_id("set", fill.fill_id)
            self.assertEqual(str(settlement["settlement_id"]), settlement_id)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    def test_phase1_rebate_attribution_keeps_cash_once_and_classifies_income(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            validate_runtime_schema(runtime)

            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            fill_repo = PostgresExecutionFillRepositoryV2(runtime.session_factory)
            reservation_repo = PostgresReservationRepository(runtime.session_factory)
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            settlement_repo = PostgresSettlementRepository(runtime.session_factory)
            mirror_service = Phase1LedgerMirrorService(
                reservation_repo=reservation_repo,
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=PostgresLedgerJournalRepository(runtime.session_factory),
                ledger_entry_repo=ledger_entry_repo,
                settlement_repo=settlement_repo,
            )
            now = utc_now()
            intent = OrderIntent(
                intent_id="intent_phase1_rebate_pg",
                decision_id="decision_phase1_rebate_pg",
                symbol="BTC-USDT",
                side="buy",
                quantity=Decimal("1"),
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase1_rebate_pg",
            )
            order_repo.create_order(
                order_id="clphase1_rebate_pg",
                intent=intent,
                initial_state="CREATED",
                created_at=now,
                raw_payload={"client_order_id": "clphase1_rebate_pg", "source_system": "integration_test"},
            )
            obligation = OrderObligation(
                obligation_id="obl_phase1_rebate_pg",
                client_order_id="clphase1_rebate_pg",
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
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
            )
            fill = FillEvent(
                fill_id="fill_phase1_rebate_pg_1",
                decision_id=obligation.decision_id,
                intent_id=obligation.intent_id,
                client_order_id=obligation.client_order_id,
                exchange_order_id="ord_phase1_rebate_pg",
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
                order_status_after_fill="FILLED",
            )
            fill_repo.save_fill(
                fill=fill,
                order_id=obligation.client_order_id,
                source="okx",
                raw_payload={
                    "venue_fill_id": "venue_fill_phase1_rebate_pg_1",
                    "fill_event": fill.model_dump(mode="python"),
                },
            )

            mirror_service.sync_obligation(obligation, reason="reservation_hold", related_fill=None)
            mirror_service.sync_obligation(
                obligation.model_copy(
                    update={
                        "consumed_amount": Decimal("99"),
                        "status": "PARTIALLY_CONSUMED",
                        "last_update_ts": now,
                    }
                ),
                reason="fill_settlement",
                related_fill=fill,
            )
            mirror_service.sync_obligation(
                obligation.model_copy(
                    update={
                        "consumed_amount": Decimal("99"),
                        "released_amount": Decimal("1"),
                        "status": "RELEASED",
                        "last_update_ts": now,
                    }
                ),
                reason="reservation_release",
                related_fill=None,
            )

            reservation = reservation_repo.get_by_order_id(obligation.client_order_id)
            self.assertIsNotNone(reservation)
            assert reservation is not None
            available_account_id = ledger_account_repo.get_or_create_account(
                account_type="cash_available",
                currency="USDT",
                product_type="spot",
                margin_mode="cash",
                symbol=None,
                created_at=now,
            )
            external_account_id = ledger_account_repo.get_or_create_account(
                account_type="external_clearing",
                currency="USDT",
                product_type="spot",
                margin_mode="cash",
                symbol=None,
                created_at=now,
            )
            fee_income_account_id = ledger_account_repo.get_or_create_account(
                account_type="fee_income",
                currency="USDT",
                product_type="spot",
                margin_mode="cash",
                symbol=None,
                created_at=now,
            )
            self.assertEqual(ledger_entry_repo.balance_by_account(available_account_id), Decimal("-99"))
            self.assertEqual(ledger_entry_repo.balance_by_account(str(reservation["reserve_account_id"])), Decimal("0"))
            self.assertEqual(ledger_entry_repo.balance_by_account(external_account_id), Decimal("100"))
            self.assertEqual(ledger_entry_repo.balance_by_account(fee_income_account_id), Decimal("-1"))

            settlement = settlement_repo.get_by_fill_id(fill.fill_id)
            self.assertIsNotNone(settlement)
            assert settlement is not None
            self.assertEqual(str(settlement["state"]), "POSTED")
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    def test_phase1_spot_sell_base_fee_consumes_reserved_base_and_classifies_expense(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            validate_runtime_schema(runtime)

            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            fill_repo = PostgresExecutionFillRepositoryV2(runtime.session_factory)
            reservation_repo = PostgresReservationRepository(runtime.session_factory)
            ledger_account_repo = PostgresLedgerAccountRepository(runtime.session_factory)
            ledger_entry_repo = PostgresLedgerEntryRepository(runtime.session_factory)
            settlement_repo = PostgresSettlementRepository(runtime.session_factory)
            mirror_service = Phase1LedgerMirrorService(
                reservation_repo=reservation_repo,
                ledger_account_repo=ledger_account_repo,
                ledger_journal_repo=PostgresLedgerJournalRepository(runtime.session_factory),
                ledger_entry_repo=ledger_entry_repo,
                settlement_repo=settlement_repo,
            )
            now = utc_now()
            intent = OrderIntent(
                intent_id="intent_phase1_sell_base_fee_pg",
                decision_id="decision_phase1_sell_base_fee_pg",
                symbol="BTC-USDT",
                side="sell",
                quantity=Decimal("1"),
                execution_style="exchange",
                order_type="market",
                urgency="medium",
                time_in_force="IOC",
                reduce_only=False,
                close_only=False,
                idempotency_key="phase1_sell_base_fee_pg",
            )
            order_repo.create_order(
                order_id="clphase1_sell_base_fee_pg",
                intent=intent,
                initial_state="CREATED",
                created_at=now,
                raw_payload={"client_order_id": "clphase1_sell_base_fee_pg", "source_system": "integration_test"},
            )
            obligation = OrderObligation(
                obligation_id="obl_phase1_sell_base_fee_pg",
                client_order_id="clphase1_sell_base_fee_pg",
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
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
            fill = FillEvent(
                fill_id="fill_phase1_sell_base_fee_pg_1",
                decision_id=obligation.decision_id,
                intent_id=obligation.intent_id,
                client_order_id=obligation.client_order_id,
                exchange_order_id="ord_phase1_sell_base_fee_pg",
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
                order_status_after_fill="FILLED",
            )
            fill_repo.save_fill(
                fill=fill,
                order_id=obligation.client_order_id,
                source="test",
                raw_payload={
                    "venue_fill_id": "venue_fill_phase1_sell_base_fee_pg_1",
                    "fill_event": fill.model_dump(mode="python"),
                },
            )

            mirror_service.sync_obligation(obligation, reason="reservation_hold", related_fill=None)
            mirror_service.sync_obligation(
                obligation.model_copy(
                    update={
                        "consumed_amount": Decimal("1.01"),
                        "status": "RELEASED",
                        "last_update_ts": now,
                    }
                ),
                reason="fill_settlement",
                related_fill=fill,
            )

            reservation = reservation_repo.get_by_order_id(obligation.client_order_id)
            self.assertIsNotNone(reservation)
            assert reservation is not None
            reserved_account_id = str(reservation["reserve_account_id"])
            external_account_id = ledger_account_repo.get_or_create_account(
                account_type="external_clearing",
                currency="BTC",
                product_type="spot",
                margin_mode="cash",
                symbol=None,
                created_at=now,
            )
            fee_expense_account_id = ledger_account_repo.get_or_create_account(
                account_type="fee_expense",
                currency="BTC",
                product_type="spot",
                margin_mode="cash",
                symbol=None,
                created_at=now,
            )
            self.assertEqual(ledger_entry_repo.balance_by_account(reserved_account_id), Decimal("0"))
            self.assertEqual(ledger_entry_repo.balance_by_account(external_account_id), Decimal("1"))
            self.assertEqual(ledger_entry_repo.balance_by_account(fee_expense_account_id), Decimal("0.01"))

            settlement = settlement_repo.get_by_fill_id(fill.fill_id)
            self.assertIsNotNone(settlement)
            assert settlement is not None
            self.assertEqual(str(settlement["state"]), "POSTED")
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    @staticmethod
    def _schema_runtime():
        base_url = make_url(os.environ["AATS_DATABASE_URL"])
        schema_name = f"aats_test_{uuid.uuid4().hex[:12]}"
        admin_engine = create_engine(base_url.render_as_string(hide_password=False), future=True)
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        query = dict(base_url.query)
        existing_options = query.get("options")
        search_path_option = f"-csearch_path={schema_name}"
        query["options"] = f"{existing_options} {search_path_option}".strip() if existing_options else search_path_option
        scoped_url = base_url.set(query=query).render_as_string(hide_password=False)
        runtime = create_database_runtime(scoped_url)
        TestPhase1LedgerMirrorPostgres._apply_migrations(runtime)
        return runtime, admin_engine, schema_name

    @staticmethod
    def _apply_migrations(runtime) -> None:
        migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
        with runtime.engine.begin() as connection:
            raw_connection = connection.connection
            with raw_connection.cursor() as cursor:
                for migration_path in sorted(migrations_dir.glob("*.sql")):
                    cursor.execute(migration_path.read_text(encoding="utf-8"))

    @staticmethod
    def _drop_schema(admin_engine, schema_name: str) -> None:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()
