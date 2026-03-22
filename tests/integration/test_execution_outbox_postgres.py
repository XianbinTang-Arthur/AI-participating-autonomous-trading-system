from __future__ import annotations

import os
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderObligation, OrderState
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository
from aats.storage.session import create_database_runtime, create_schema, validate_runtime_schema


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for Postgres integration tests")
class TestExecutionOutboxPostgres(unittest.IsolatedAsyncioTestCase):
    async def test_persist_order_state_commits_and_publishes_without_duplicate_event_store_write(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            event_store = PostgresEventStore(runtime.session_factory)
            execution_repo = PostgresExecutionRepository(runtime.session_factory)
            obligation_repo = PostgresExecutionObligationRepository(runtime.session_factory)
            outbox_repo = PostgresOutboxRepository(runtime.session_factory)
            bus = InMemoryEventBus()
            received: list[dict] = []

            async def handler(message: dict) -> None:
                received.append(message)

            await bus.subscribe(topics.ORDER_UPDATES, handler)
            publisher = PostgresExecutionOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=event_store,
                execution_repo=execution_repo,
                obligation_repo=obligation_repo,
                outbox_repo=outbox_repo,
                bus=bus,
            )
            state = self._order_state(client_order_id="clord_outbox_ok", status="SUBMITTED")

            persisted = await publisher.persist_order_state(order_state=state, key=state.symbol)

            self.assertEqual(persisted.status, "SUBMITTED")
            stored = execution_repo.get_order_state(state.client_order_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "SUBMITTED")
            self.assertEqual(event_store.count(topic=topics.ORDER_UPDATES), 1)
            self.assertEqual(outbox_repo.counts(), {"pending": 0, "published": 1, "failed": 0})
            self.assertEqual(len(received), 1)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_subscriber_failure_leaves_outbox_pending_after_database_commit(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            event_store = PostgresEventStore(runtime.session_factory)
            execution_repo = PostgresExecutionRepository(runtime.session_factory)
            obligation_repo = PostgresExecutionObligationRepository(runtime.session_factory)
            outbox_repo = PostgresOutboxRepository(runtime.session_factory)
            bus = InMemoryEventBus()

            async def exploding_handler(message: dict) -> None:
                raise RuntimeError("subscriber_boom")

            await bus.subscribe(topics.ORDER_UPDATES, exploding_handler)
            publisher = PostgresExecutionOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=event_store,
                execution_repo=execution_repo,
                obligation_repo=obligation_repo,
                outbox_repo=outbox_repo,
                bus=bus,
            )
            state = self._order_state(client_order_id="clord_outbox_pending", status="SUBMITTED")

            await publisher.persist_order_state(order_state=state, key=state.symbol)

            stored = execution_repo.get_order_state(state.client_order_id)
            self.assertIsNotNone(stored)
            self.assertEqual(event_store.count(topic=topics.ORDER_UPDATES), 1)
            self.assertEqual(outbox_repo.counts(), {"pending": 1, "published": 0, "failed": 0})
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_poisoned_outbox_row_is_failed_after_retry_budget_and_later_rows_still_publish(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            event_store = PostgresEventStore(runtime.session_factory)
            execution_repo = PostgresExecutionRepository(runtime.session_factory)
            obligation_repo = PostgresExecutionObligationRepository(runtime.session_factory)
            outbox_repo = PostgresOutboxRepository(runtime.session_factory)
            bus = InMemoryEventBus()
            received: list[str] = []

            async def selective_handler(message: dict) -> None:
                client_order_id = message["payload"]["payload"]["client_order_id"]
                if client_order_id == "clord_outbox_poison":
                    raise RuntimeError("subscriber_boom")
                received.append(client_order_id)

            await bus.subscribe(topics.ORDER_UPDATES, selective_handler)
            publisher = PostgresExecutionOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=event_store,
                execution_repo=execution_repo,
                obligation_repo=obligation_repo,
                outbox_repo=outbox_repo,
                bus=bus,
            )

            await publisher.persist_order_state(
                order_state=self._order_state(client_order_id="clord_outbox_poison", status="SUBMITTED"),
                key="BTC-USDT",
            )
            await publisher.flush_pending()
            await publisher.flush_pending()

            await publisher.persist_order_state(
                order_state=self._order_state(client_order_id="clord_outbox_ok_after_poison", status="SUBMITTED"),
                key="BTC-USDT",
            )

            self.assertEqual(outbox_repo.counts(), {"pending": 0, "published": 1, "failed": 1})
            self.assertEqual(received, ["clord_outbox_ok_after_poison"])
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_persist_fill_updates_obligation_in_same_commit(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            event_store = PostgresEventStore(runtime.session_factory)
            execution_repo = PostgresExecutionRepository(runtime.session_factory)
            obligation_repo = PostgresExecutionObligationRepository(runtime.session_factory)
            outbox_repo = PostgresOutboxRepository(runtime.session_factory)
            bus = InMemoryEventBus()
            publisher = PostgresExecutionOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=event_store,
                execution_repo=execution_repo,
                obligation_repo=obligation_repo,
                outbox_repo=outbox_repo,
                bus=bus,
            )
            base_obligation = OrderObligation(
                client_order_id="clord_fill_atomic",
                decision_id="decision_fill_atomic",
                intent_id="intent_fill_atomic",
                symbol="BTC-USDT",
                side="buy",
                reserve_currency="USDT",
                reserved_amount=Decimal("60.0"),
                status="ACTIVE",
                product_type="spot",
                margin_mode="cash",
                reference_price=Decimal("60000.0"),
                last_update_ts=utc_now(),
            )
            obligation_repo.save_obligation(base_obligation)
            fill = FillEvent(
                fill_id="fill_atomic_1",
                decision_id="decision_fill_atomic",
                intent_id="intent_fill_atomic",
                client_order_id="clord_fill_atomic",
                exchange_order_id="ord_fill_atomic",
                symbol="BTC-USDT",
                venue="OKX",
                side="buy",
                fill_qty=Decimal("0.001"),
                fill_price=Decimal("60000.0"),
                fee_amount=Decimal("0.0"),
                fee_currency="USDT",
                product_type="spot",
                margin_mode="cash",
                liquidity_role="taker",
                exchange_timestamp=utc_now(),
                ingestion_timestamp=utc_now(),
                order_status_after_fill="FILLED",
            )
            updated_obligation = base_obligation.model_copy(
                update={
                    "consumed_amount": Decimal("60.0"),
                    "status": "RELEASED",
                    "last_update_ts": utc_now(),
                }
            )

            saved = await publisher.persist_fill(fill=fill, obligation=updated_obligation)

            self.assertTrue(saved)
            self.assertEqual(len(execution_repo.fills_for_order("clord_fill_atomic")), 1)
            stored_obligation = obligation_repo.get_obligation("clord_fill_atomic")
            self.assertIsNotNone(stored_obligation)
            self.assertEqual(stored_obligation.consumed_amount, Decimal("60.0"))
            self.assertEqual(stored_obligation.status, "RELEASED")
            self.assertEqual(event_store.count(topic=topics.FILL_EVENTS), 1)
            self.assertEqual(outbox_repo.counts(), {"pending": 0, "published": 1, "failed": 0})
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    def test_sql_migrations_create_numeric_financial_columns(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime(use_migrations=True)
        try:
            validate_runtime_schema(runtime)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    def test_runtime_schema_validation_rejects_float_financial_columns(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            with runtime.engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS order_obligations"))
                connection.execute(
                    text(
                        """
                        CREATE TABLE order_obligations (
                            client_order_id VARCHAR(64) PRIMARY KEY,
                            obligation_id VARCHAR(64) NOT NULL,
                            decision_id VARCHAR(64) NOT NULL,
                            intent_id VARCHAR(64) NOT NULL,
                            symbol VARCHAR(32) NOT NULL,
                            reserve_currency VARCHAR(16) NOT NULL,
                            status VARCHAR(32) NOT NULL,
                            reserved_amount DOUBLE PRECISION NOT NULL,
                            consumed_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                            released_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                            product_type VARCHAR(16) NULL,
                            margin_mode VARCHAR(16) NULL,
                            last_update_ts TIMESTAMPTZ NULL,
                            created_at TIMESTAMPTZ NOT NULL,
                            payload JSONB NOT NULL
                        )
                        """
                    )
                )
            with self.assertRaisesRegex(RuntimeError, "database_schema_validation_failed"):
                validate_runtime_schema(runtime)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    @staticmethod
    def _order_state(*, client_order_id: str, status: str) -> OrderState:
        now = utc_now()
        return OrderState(
            decision_id=f"decision_{client_order_id}",
            intent_id=f"intent_{client_order_id}",
            symbol="BTC-USDT",
            client_order_id=client_order_id,
            venue="OKX",
            exchange_order_id=f"ord_{client_order_id}",
            status=status,
            submission_mode="guarded_simulated_submit",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=Decimal("0.001"),
            filled_qty=Decimal("0.0"),
            remaining_qty=Decimal("0.001"),
            average_fill_price=None,
            fees=Decimal("0.0"),
            product_type="spot",
            margin_mode="cash",
            submission_payload={"instId": "BTC-USDT"},
        )

    @staticmethod
    def _schema_runtime(*, use_migrations: bool = False):
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
        if use_migrations:
            TestExecutionOutboxPostgres._apply_migrations(runtime)
        else:
            create_schema(runtime)
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
