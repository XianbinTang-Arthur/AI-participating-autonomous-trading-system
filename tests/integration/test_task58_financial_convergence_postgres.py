from __future__ import annotations

import os
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderState
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.services.ledger.persistent_lot_book import PersistentLotBookService
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository
from aats.storage.lot_repo_postgres import PostgresLotEventRepository, PostgresPositionLotRepository
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository
from aats.storage.session import create_database_runtime, validate_runtime_schema
from aats.storage.sqlalchemy_models import ExecutionFillModelV2, ExecutionOrderModel, FillEventModel, OrderStateModel


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for Postgres integration tests")
class TestTask58FinancialConvergencePostgres(unittest.IsolatedAsyncioTestCase):
    def test_real_postgres_dual_runtime_lock_conflict_is_rejected(self) -> None:
        runtime_a, runtime_b, admin_engine, schema_name = self._schema_runtimes()
        try:
            runtime_a.acquire_single_runtime_lock(918273)
            with self.assertRaisesRegex(RuntimeError, "database_single_runtime_lock_not_acquired"):
                runtime_b.acquire_single_runtime_lock(918273)
        finally:
            runtime_a.dispose()
            runtime_b.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_failure_injection_keeps_converged_execution_truth_without_legacy_rows(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            validate_runtime_schema(runtime)
            bus = InMemoryEventBus()

            async def exploding_handler(message: dict) -> None:
                raise RuntimeError("task58_real_pg_subscriber_boom")

            await bus.subscribe(topics.ORDER_UPDATES, exploding_handler)
            execution_repo = ConvergedPostgresExecutionRepository(
                runtime.session_factory,
                execution_order_repo=PostgresExecutionOrderRepository(runtime.session_factory),
                execution_order_history_repo=PostgresExecutionOrderHistoryRepository(runtime.session_factory),
                execution_fill_repo=PostgresExecutionFillRepositoryV2(runtime.session_factory),
            )
            publisher = PostgresExecutionOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=PostgresEventStore(runtime.session_factory),
                execution_repo=execution_repo,
                obligation_repo=PostgresExecutionObligationRepository(runtime.session_factory),
                outbox_repo=PostgresOutboxRepository(runtime.session_factory),
                bus=bus,
            )

            persisted = await publisher.persist_order_state(
                order_state=self._order_state(client_order_id="cl_task58_pg"),
                key="BTC-USDT",
            )
            saved_fill = await publisher.persist_fill(
                fill=self._fill(client_order_id="cl_task58_pg", fill_id="fill_task58_pg"),
            )

            self.assertEqual(persisted.status, "SUBMITTED")
            self.assertTrue(saved_fill)
            self.assertEqual(publisher.outbox_repo.counts()["pending"], 2)
            with runtime.session_factory() as session:
                self.assertEqual(session.scalar(select(func.count()).select_from(ExecutionOrderModel)), 1)
                self.assertEqual(session.scalar(select(func.count()).select_from(ExecutionFillModelV2)), 1)
                self.assertEqual(session.scalar(select(func.count()).select_from(OrderStateModel)), 0)
                self.assertEqual(session.scalar(select(func.count()).select_from(FillEventModel)), 0)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    def test_persistent_lots_work_against_real_postgres_migrations(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            validate_runtime_schema(runtime)
            service = PersistentLotBookService(
                position_lot_repo=PostgresPositionLotRepository(runtime.session_factory),
                lot_event_repo=PostgresLotEventRepository(runtime.session_factory),
                projection_builder=LotBasedProjectionBuilder(),
            )
            fills = [
                self._fill(client_order_id="cl_task58_lots", fill_id="fill_task58_lot_1", side="buy", qty="1", price="100"),
                self._fill(client_order_id="cl_task58_lots", fill_id="fill_task58_lot_2", side="sell", qty="1.5", price="110"),
            ]

            service.rebuild_from_fills(fills=fills, product_type="spot", margin_mode="cash")

            lots = PostgresPositionLotRepository(runtime.session_factory).lots_for_scope(
                symbol="BTC-USDT",
                product_type="spot",
                margin_mode="cash",
                open_only=True,
            )
            self.assertEqual(len(lots), 1)
            self.assertEqual(Decimal(str(lots[0]["signed_quantity_open"])), Decimal("-0.5"))
            events = PostgresLotEventRepository(runtime.session_factory).events_for_fill("fill_task58_lot_2")
            self.assertGreaterEqual(len(events), 2)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    @staticmethod
    def _order_state(*, client_order_id: str) -> OrderState:
        now = utc_now()
        return OrderState(
            decision_id=f"decision_{client_order_id}",
            intent_id=f"intent_{client_order_id}",
            symbol="BTC-USDT",
            client_order_id=client_order_id,
            venue="OKX",
            exchange_order_id=f"ord_{client_order_id}",
            status="SUBMITTED",
            submission_mode="financial_convergence",
            submitted_ts=now,
            last_update_ts=now,
            requested_qty=Decimal("0.010000000000000000"),
            filled_qty=Decimal("0"),
            remaining_qty=Decimal("0.010000000000000000"),
            average_fill_price=None,
            fees=Decimal("0"),
            product_type="spot",
            margin_mode="cash",
            exposure_side="long",
            position_intent="open_long",
            submission_payload={"instId": "BTC-USDT"},
        )

    @staticmethod
    def _fill(
        *,
        client_order_id: str,
        fill_id: str,
        side: str = "buy",
        qty: str = "0.010000000000000000",
        price: str = "100.000000000000000000",
    ) -> FillEvent:
        now = utc_now()
        return FillEvent(
            fill_id=fill_id,
            decision_id=f"decision_{client_order_id}",
            intent_id=f"intent_{client_order_id}",
            client_order_id=client_order_id,
            exchange_order_id=f"ord_{client_order_id}",
            symbol="BTC-USDT",
            venue="OKX",
            side=side,  # type: ignore[arg-type]
            fill_qty=Decimal(qty),
            fill_price=Decimal(price),
            fee_amount=Decimal("0"),
            fee_currency="USDT",
            product_type="spot",
            margin_mode="cash",
            exposure_side="long" if side == "buy" else "short",
            execution_action="enter",
            position_intent="open_long" if side == "buy" else "close_long",
            liquidity_role="taker",
            exchange_timestamp=now,
            ingestion_timestamp=now,
            order_status_after_fill="FILLED",
        )

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
        TestTask58FinancialConvergencePostgres._apply_migrations(runtime)
        return runtime, admin_engine, schema_name

    @staticmethod
    def _schema_runtimes():
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
        runtime_a = create_database_runtime(scoped_url)
        runtime_b = create_database_runtime(scoped_url)
        TestTask58FinancialConvergencePostgres._apply_migrations(runtime_a)
        return runtime_a, runtime_b, admin_engine, schema_name

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


if __name__ == "__main__":
    unittest.main()
