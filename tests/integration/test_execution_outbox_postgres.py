from __future__ import annotations

import asyncio
import logging
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
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation, OrderState
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.execution_engine.order_state_cache import OrderStateHotCache, _order_state_key
from aats.storage.execution_command_repo_postgres import PostgresExecutionCommandRepository
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.hot_state_store import InMemoryHotStateStore
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
            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
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
                execution_order_repo=order_repo,
            )
            state = self._order_state(client_order_id="clord_outbox_ok", status="SUBMITTED")

            persisted = await publisher.persist_order_state(order_state=state, key=state.symbol)

            self.assertEqual(persisted.status, "SUBMITTED")
            stored = execution_repo.get_order_state(state.client_order_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "SUBMITTED")
            self.assertEqual(stored.td_mode, "cross")
            self.assertEqual(stored.position_mode, "long_short_mode")
            self.assertEqual(stored.pos_side, "long")
            self.assertTrue(stored.reduce_only)
            stored_row = order_repo.get_order(state.client_order_id)
            self.assertIsNotNone(stored_row)
            self.assertEqual(stored_row["execution_attempt_id"], "execution_attempt:clord_outbox_ok")
            self.assertEqual(event_store.count(topic=topics.ORDER_UPDATES), 1)
            self.assertEqual(outbox_repo.counts(), {"pending": 0, "published": 1, "failed": 0})
            self.assertEqual(len(received), 1)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_persist_order_state_can_sync_existing_execution_order_truth_for_repair(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            event_store = PostgresEventStore(runtime.session_factory)
            execution_repo = PostgresExecutionRepository(runtime.session_factory)
            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            order_history_repo = PostgresExecutionOrderHistoryRepository(runtime.session_factory)
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
                execution_order_repo=order_repo,
                execution_order_history_repo=order_history_repo,
            )
            initial = self._order_state(client_order_id="clord_repair_truth_sync", status="SUBMITTING")
            execution_repo.save_order_state(initial)
            intent = OrderIntent(
                intent_id=initial.intent_id,
                decision_id=initial.decision_id,
                symbol=initial.symbol,
                side="sell",
                quantity=initial.requested_qty,
                execution_style="taker",
                order_type="market",
                reference_price=Decimal("60000"),
                urgency="medium",
                time_in_force="IOC",
                reduce_only=True,
                close_only=True,
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="long",
                instrument_family="BTC-USDT",
                settle_currency="USDT",
                idempotency_key=f"submit:{initial.intent_id}",
                product_type="derivatives",
                margin_mode="cross",
                execution_attempt_id=initial.execution_attempt_id,
                position_intent="close_long",
            )
            order_repo.create_order(
                order_id=initial.client_order_id,
                intent=intent,
                initial_state="SUBMITTING",
                created_at=initial.created_at,
                raw_payload={
                    "client_order_id": initial.client_order_id,
                    "source_system": "execution_command_service",
                    "intent": intent.model_dump(mode="python"),
                    "lifecycle_snapshot_refs": {"submit": {"source": "seed_submit"}},
                    "operator_note": "preserve_me",
                },
            )
            failed = initial.model_copy(
                update={
                    "status": "FAILED",
                    "execution_error": "operator_resolved_stuck_submission_after_restart",
                    "last_update_ts": utc_now(),
                }
            )

            persisted = await publisher.persist_order_state(
                order_state=failed,
                key=failed.symbol,
                source_component="operator_api",
                emit_execution_error_summary=False,
                sync_execution_order_truth=True,
                history_reason_code="operator_state_sync",
            )

            self.assertEqual(persisted.status, "FAILED")
            stored_state = execution_repo.get_order_state(initial.client_order_id)
            self.assertIsNotNone(stored_state)
            assert stored_state is not None
            self.assertEqual(stored_state.status, "FAILED")
            stored_order = order_repo.get_order(initial.client_order_id)
            self.assertIsNotNone(stored_order)
            assert stored_order is not None
            self.assertEqual(stored_order["state"], "FAILED")
            raw_payload = dict(stored_order["raw_payload"])
            self.assertEqual(raw_payload["source_system"], "operator_api")
            self.assertEqual(raw_payload["operator_note"], "preserve_me")
            self.assertEqual(raw_payload["intent"]["intent_id"], initial.intent_id)
            self.assertEqual(raw_payload["lifecycle_snapshot_refs"]["submit"]["source"], "seed_submit")
            self.assertEqual(raw_payload["order_state"]["status"], "FAILED")
            history = order_history_repo.history_for_order(initial.client_order_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["from_state"], "SUBMITTING")
            self.assertEqual(history[0]["to_state"], "FAILED")
            self.assertEqual(history[0]["reason_code"], "operator_state_sync")
            self.assertEqual(history[0]["source"], "operator_api")
            self.assertEqual(event_store.count(topic=topics.ORDER_UPDATES), 1)
            self.assertEqual(event_store.count(topic=topics.EXECUTION_ERROR_SUMMARIES), 0)
            self.assertEqual(outbox_repo.counts(), {"pending": 0, "published": 1, "failed": 0})
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
            original_attempts = PostgresExecutionOutboxPublisher._MAX_PUBLISH_ATTEMPTS
            PostgresExecutionOutboxPublisher._MAX_PUBLISH_ATTEMPTS = 4
            try:
                await publisher.persist_order_state(
                    order_state=self._order_state(client_order_id="clord_outbox_poison", status="SUBMITTED"),
                    key="BTC-USDT",
                )
                await publisher.flush_pending()
                await publisher.persist_order_state(
                    order_state=self._order_state(client_order_id="clord_outbox_ok_waiting", status="SUBMITTED"),
                    key="BTC-USDT",
                )

                self.assertEqual(outbox_repo.counts(), {"pending": 2, "published": 0, "failed": 0})
                self.assertEqual(received, [])

                await publisher.flush_pending()

                await publisher.persist_order_state(
                    order_state=self._order_state(client_order_id="clord_outbox_ok_after_poison", status="SUBMITTED"),
                    key="BTC-USDT",
                )

                self.assertEqual(outbox_repo.counts(), {"pending": 0, "published": 2, "failed": 1})
                self.assertEqual(received, ["clord_outbox_ok_waiting", "clord_outbox_ok_after_poison"])
            finally:
                PostgresExecutionOutboxPublisher._MAX_PUBLISH_ATTEMPTS = original_attempts
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_persist_order_state_and_command_seeds_execution_order_with_submit_intent_fields(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            event_store = PostgresEventStore(runtime.session_factory)
            execution_repo = PostgresExecutionRepository(runtime.session_factory)
            obligation_repo = PostgresExecutionObligationRepository(runtime.session_factory)
            outbox_repo = PostgresOutboxRepository(runtime.session_factory)
            command_repo = PostgresExecutionCommandRepository(runtime.session_factory)
            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            order_history_repo = PostgresExecutionOrderHistoryRepository(runtime.session_factory)
            bus = InMemoryEventBus()
            publisher = PostgresExecutionOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=event_store,
                execution_repo=execution_repo,
                obligation_repo=obligation_repo,
                outbox_repo=outbox_repo,
                bus=bus,
                execution_command_repo=command_repo,
                execution_order_repo=order_repo,
                execution_order_history_repo=order_history_repo,
            )
            intent = OrderIntent(
                intent_id="intent_outbox_limit_seed",
                decision_id="decision_outbox_limit_seed",
                symbol="BTC-USDT-SWAP",
                side="sell",
                quantity=Decimal("0.002"),
                execution_style="maker",
                order_type="limit",
                limit_price=Decimal("70010.5"),
                reference_price=Decimal("70000"),
                urgency="low",
                time_in_force="GTC",
                max_slippage_tolerance_bps=15,
                reduce_only=False,
                close_only=False,
                td_mode="cross",
                position_mode="long_short_mode",
                pos_side="short",
                idempotency_key="outbox_limit_seed",
                product_type="derivatives",
                target_leverage=3.0,
                margin_mode="cross",
                exposure_side="short",
                position_intent="open_short",
                market_snapshot_ref="mkt_outbox_limit_seed",
                feature_snapshot_ref="feat_outbox_limit_seed",
                portfolio_snapshot_ref="port_outbox_limit_seed",
                health_snapshot_ref="health_outbox_limit_seed",
            )
            state = OrderState(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id="clord_outbox_limit_seed",
                venue="OKX",
                exchange_order_id=None,
                status="CREATED",
                submission_mode="phase2_command_flow",
                requested_qty=intent.quantity,
                filled_qty=Decimal("0"),
                remaining_qty=intent.quantity,
                average_fill_price=None,
                fees=Decimal("0"),
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                product_type=intent.product_type,
                margin_mode=intent.margin_mode,
                target_leverage=intent.target_leverage,
                exposure_side=intent.exposure_side,
                position_intent=intent.position_intent,
                submission_payload={},
            )

            await publisher.persist_order_state_and_command(
                order_state=state,
                key=state.symbol,
                command_id="cmd_outbox_limit_seed",
                command_type="submit",
                command_idempotency_key="submit:intent_outbox_limit_seed",
                command_payload={"intent": intent.model_dump(mode="python"), "client_order_id": state.client_order_id},
                command_created_at=utc_now(),
            )

            stored_order = order_repo.get_order(state.client_order_id)
            self.assertIsNotNone(stored_order)
            self.assertEqual(stored_order["order_type"], "limit")
            self.assertEqual(stored_order["time_in_force"], "GTC")
            self.assertEqual(Decimal(str(stored_order["limit_price"])), Decimal("70010.5"))
            self.assertEqual(stored_order["side"], "sell")
            self.assertEqual(stored_order["execution_style"], "maker")
            raw_payload = dict(stored_order.get("raw_payload") or {})
            self.assertIn("intent", raw_payload)
            self.assertEqual(raw_payload["intent"]["order_type"], "limit")
            self.assertEqual(raw_payload["intent"]["time_in_force"], "GTC")
            self.assertEqual(Decimal(str(raw_payload["intent"]["limit_price"])), Decimal("70010.5"))
            self.assertEqual(raw_payload["market_snapshot_ref"], "mkt_outbox_limit_seed")
            lifecycle = raw_payload["lifecycle_snapshot_refs"]
            self.assertEqual(lifecycle["submit"]["market_snapshot_ref"], "mkt_outbox_limit_seed")
            self.assertEqual(lifecycle["submit"]["source"], "execution_outbox_submit")
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_enqueue_command_treats_duplicate_idempotency_key_as_idempotent_success(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            command_repo = PostgresExecutionCommandRepository(runtime.session_factory)
            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            state = self._order_state(client_order_id="clord_command_idem", status="CREATED")
            order_repo.create_order(
                order_id=state.client_order_id,
                intent=OrderIntent(
                    intent_id=state.intent_id,
                    decision_id=state.decision_id,
                    symbol=state.symbol,
                    side="sell",
                    quantity=Decimal("0.001"),
                    execution_style="taker",
                    order_type="market",
                    reference_price=Decimal("60000"),
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=True,
                    close_only=True,
                    td_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                    idempotency_key="submit:intent_command_idem",
                    product_type="derivatives",
                    margin_mode="cross",
                    execution_attempt_id="execution_attempt:clord_command_idem",
                    position_intent="close_long",
                ),
                initial_state="CREATED",
                created_at=utc_now(),
                raw_payload={"client_order_id": state.client_order_id},
            )
            created_at = utc_now()

            command_repo.enqueue_command(
                command_id="cmd_command_idem_1",
                order_id=state.client_order_id,
                command_type="submit",
                idempotency_key="submit:intent_command_idem",
                payload={"client_order_id": state.client_order_id},
                created_at=created_at,
            )
            command_repo.enqueue_command(
                command_id="cmd_command_idem_2",
                order_id=state.client_order_id,
                command_type="submit",
                idempotency_key="submit:intent_command_idem",
                payload={"client_order_id": state.client_order_id},
                created_at=created_at,
            )

            pending = command_repo.pending_commands(limit=10)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["command_id"], "cmd_command_idem_1")
            self.assertEqual(pending[0]["idempotency_key"], "submit:intent_command_idem")
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_command_attempt_count_tracks_claims_without_double_counting_terminal_updates(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            command_repo = PostgresExecutionCommandRepository(runtime.session_factory)
            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            created_at = utc_now()
            state = self._order_state(client_order_id="clord_attempt_count_1", status="CREATED")
            order_repo.create_order(
                order_id=state.client_order_id,
                intent=OrderIntent(
                    intent_id=state.intent_id,
                    decision_id=state.decision_id,
                    symbol=state.symbol,
                    side="sell",
                    quantity=Decimal("0.001"),
                    execution_style="taker",
                    order_type="market",
                    reference_price=Decimal("60000"),
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=True,
                    close_only=True,
                    td_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                    idempotency_key="submit:intent_attempt_count_1",
                    product_type="derivatives",
                    margin_mode="cross",
                    execution_attempt_id="execution_attempt:clord_attempt_count_1",
                    position_intent="close_long",
                ),
                initial_state="CREATED",
                created_at=created_at,
                raw_payload={"client_order_id": state.client_order_id},
            )
            command_repo.enqueue_command(
                command_id="cmd_attempt_count_1",
                order_id="clord_attempt_count_1",
                command_type="submit",
                idempotency_key="submit:intent_attempt_count_1",
                payload={"client_order_id": "clord_attempt_count_1"},
                created_at=created_at,
            )
            pending = command_repo.pending_commands(limit=10)
            self.assertEqual(len(pending), 1)
            claimed = command_repo.claim_command(
                command_id="cmd_attempt_count_1",
                expected_state="PENDING",
                expected_updated_at=pending[0]["updated_at"],
                updated_at=utc_now(),
            )
            self.assertTrue(claimed)
            command_repo.mark_acked("cmd_attempt_count_1", updated_at=utc_now())

            stored = command_repo.get_command("cmd_attempt_count_1")

            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["state"], "ACKED")
            self.assertEqual(stored["attempt_count"], 1)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_persist_fill_updates_obligation_in_same_commit(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            event_store = PostgresEventStore(runtime.session_factory)
            execution_repo = PostgresExecutionRepository(runtime.session_factory)
            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            fill_repo = PostgresExecutionFillRepositoryV2(runtime.session_factory)
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
                execution_order_repo=order_repo,
                execution_fill_repo=fill_repo,
            )
            base_obligation = OrderObligation(
                client_order_id="clord_fill_atomic",
                decision_id="decision_fill_atomic",
                intent_id="intent_fill_atomic",
                symbol="BTC-USDT-SWAP",
                side="sell",
                reserve_currency="USDT",
                reserved_amount=Decimal("60.0"),
                status="ACTIVE",
                product_type="derivatives",
                margin_mode="cross",
                reference_price=Decimal("60000.0"),
                last_update_ts=utc_now(),
            )
            obligation_repo.save_obligation(base_obligation)
            fill = FillEvent(
                fill_id="fill_atomic_1",
                decision_id="decision_fill_atomic",
                execution_attempt_id="execution_attempt:clord_fill_atomic",
                intent_id="intent_fill_atomic",
                client_order_id="clord_fill_atomic",
                exchange_order_id="ord_fill_atomic",
                symbol="BTC-USDT-SWAP",
                venue="OKX",
                side="sell",
                fill_qty=Decimal("0.001"),
                fill_price=Decimal("60000.0"),
                fee_amount=Decimal("0.0"),
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
                position_intent="close_long",
                liquidity_role="taker",
                exchange_timestamp=utc_now(),
                ingestion_timestamp=utc_now(),
                order_status_after_fill="FILLED",
                market_snapshot_ref="mkt_fill_atomic",
                feature_snapshot_ref="feat_fill_atomic",
                portfolio_snapshot_ref="port_fill_atomic",
                health_snapshot_ref="health_fill_atomic",
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
            stored_fills = execution_repo.fills_for_order("clord_fill_atomic")
            self.assertEqual(len(stored_fills), 1)
            self.assertEqual(stored_fills[0].td_mode, "cross")
            self.assertEqual(stored_fills[0].instrument_family, "BTC-USDT")
            self.assertTrue(stored_fills[0].close_only)
            stored_fill_row = fill_repo.get_fill("fill_atomic_1")
            self.assertIsNotNone(stored_fill_row)
            self.assertEqual(stored_fill_row["execution_attempt_id"], "execution_attempt:clord_fill_atomic")
            fill_payload = dict(stored_fill_row.get("raw_payload") or {})
            self.assertEqual(fill_payload["market_snapshot_ref"], "mkt_fill_atomic")
            lifecycle = fill_payload["lifecycle_snapshot_refs"]
            self.assertEqual(lifecycle["fill"]["market_snapshot_ref"], "mkt_fill_atomic")
            self.assertEqual(lifecycle["fill"]["source"], "execution_outbox_fill")
            stored_obligation = obligation_repo.get_obligation("clord_fill_atomic")
            self.assertIsNotNone(stored_obligation)
            self.assertEqual(stored_obligation.consumed_amount, Decimal("60.0"))
            self.assertEqual(stored_obligation.status, "RELEASED")
            self.assertEqual(event_store.count(topic=topics.FILL_EVENTS), 1)
            self.assertEqual(outbox_repo.counts(), {"pending": 0, "published": 1, "failed": 0})
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_persist_fill_syncs_order_state_cache_after_converged_repo_refresh(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            event_store = PostgresEventStore(runtime.session_factory)
            order_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            order_history_repo = PostgresExecutionOrderHistoryRepository(runtime.session_factory)
            fill_repo = PostgresExecutionFillRepositoryV2(runtime.session_factory)
            execution_repo = ConvergedPostgresExecutionRepository(
                runtime.session_factory,
                execution_order_repo=order_repo,
                execution_order_history_repo=order_history_repo,
                execution_fill_repo=fill_repo,
            )
            obligation_repo = PostgresExecutionObligationRepository(runtime.session_factory)
            outbox_repo = PostgresOutboxRepository(runtime.session_factory)
            bus = InMemoryEventBus()
            hot_store = InMemoryHotStateStore()
            order_state_cache = OrderStateHotCache(
                logger=logging.getLogger("test.execution_outbox.order_state_cache")
            )
            await order_state_cache.bootstrap(
                hot_state_store=hot_store,
                bus=bus,
                process_role="execution",
            )
            publisher = PostgresExecutionOutboxPublisher(
                session_factory=runtime.session_factory,
                event_store=event_store,
                execution_repo=execution_repo,
                obligation_repo=obligation_repo,
                outbox_repo=outbox_repo,
                bus=bus,
                execution_order_repo=order_repo,
                execution_order_history_repo=order_history_repo,
                execution_fill_repo=fill_repo,
                order_state_cache=order_state_cache,
            )

            client_order_id = "clord_fill_cache_sync"
            order_state = self._order_state(client_order_id=client_order_id, status="SUBMITTED")
            order_repo.create_order(
                order_id=client_order_id,
                intent=OrderIntent(
                    intent_id=order_state.intent_id,
                    decision_id=order_state.decision_id,
                    symbol=order_state.symbol,
                    side="sell",
                    quantity=Decimal("0.001"),
                    execution_style="taker",
                    order_type="market",
                    reference_price=Decimal("60000"),
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=True,
                    close_only=True,
                    td_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                    idempotency_key="submit:intent_fill_cache_sync",
                    product_type="derivatives",
                    margin_mode="cross",
                    execution_attempt_id=f"execution_attempt:{client_order_id}",
                    position_intent="close_long",
                ),
                initial_state="SUBMITTED",
                created_at=utc_now(),
                raw_payload={"client_order_id": client_order_id, "source_system": "local_order_manager"},
            )

            fill = FillEvent(
                fill_id="fill_cache_sync_1",
                decision_id=order_state.decision_id,
                execution_attempt_id=f"execution_attempt:{client_order_id}",
                intent_id=order_state.intent_id,
                client_order_id=client_order_id,
                exchange_order_id=order_state.exchange_order_id,
                symbol=order_state.symbol,
                venue="OKX",
                side="sell",
                fill_qty=Decimal("0.001"),
                fill_price=Decimal("60000"),
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
                position_intent="close_long",
                liquidity_role="taker",
                exchange_timestamp=utc_now(),
                ingestion_timestamp=utc_now(),
                order_status_after_fill="FILLED",
            )

            saved = await publisher.persist_fill(fill=fill)
            self.assertTrue(saved)
            await asyncio.sleep(0.05)

            latest_state = execution_repo.get_order_state(client_order_id)
            self.assertIsNotNone(latest_state)
            assert latest_state is not None
            self.assertEqual(latest_state.status, "FILLED")

            cached = await hot_store.get(_order_state_key(client_order_id))
            self.assertIsNotNone(cached)
            self.assertEqual(cached["status"], "FILLED")
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_save_fill_treats_duplicate_source_and_venue_fill_as_idempotent_success(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            execution_repo = PostgresExecutionOrderRepository(runtime.session_factory)
            fill_repo = PostgresExecutionFillRepositoryV2(runtime.session_factory)
            order_state = self._order_state(client_order_id="clord_fill_idem", status="SUBMITTED")
            execution_repo.create_order(
                order_id=order_state.client_order_id,
                intent=OrderIntent(
                    intent_id=order_state.intent_id,
                    decision_id=order_state.decision_id,
                    symbol=order_state.symbol,
                    side="sell",
                    quantity=Decimal("0.001"),
                    execution_style="taker",
                    order_type="market",
                    reference_price=Decimal("60000"),
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=True,
                    close_only=True,
                    td_mode="cross",
                    position_mode="long_short_mode",
                    pos_side="long",
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                    idempotency_key="submit:intent_fill_idem",
                    product_type="derivatives",
                    margin_mode="cross",
                    execution_attempt_id="execution_attempt:clord_fill_idem",
                    position_intent="close_long",
                ),
                initial_state="SUBMITTED",
                created_at=utc_now(),
                raw_payload={"client_order_id": order_state.client_order_id},
            )
            exchange_ts = utc_now()
            ingestion_ts = utc_now()
            fill = FillEvent(
                fill_id="fill_idem_1",
                decision_id=order_state.decision_id,
                execution_attempt_id="execution_attempt:clord_fill_idem",
                intent_id=order_state.intent_id,
                client_order_id=order_state.client_order_id,
                exchange_order_id=order_state.exchange_order_id,
                symbol=order_state.symbol,
                venue="OKX",
                side="sell",
                fill_qty=Decimal("0.001"),
                fill_price=Decimal("60000"),
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
                position_intent="close_long",
                liquidity_role="taker",
                raw_exchange={
                    "feeRate": "-0.0005",
                    "execType": "T",
                },
                exchange_timestamp=exchange_ts,
                ingestion_timestamp=ingestion_ts,
                order_status_after_fill="FILLED",
            )

            first = fill_repo.save_fill(
                fill=fill,
                order_id=order_state.client_order_id,
                source="OKX",
                raw_payload={"venue_fill_id": "venue_fill_idem_1"},
            )
            second = fill_repo.save_fill(
                fill=fill.model_copy(update={"fill_id": "fill_idem_2"}),
                order_id=order_state.client_order_id,
                source="OKX",
                raw_payload={"venue_fill_id": "venue_fill_idem_1"},
            )

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(fill_repo.count_fills(), 1)
            stored = fill_repo.get_fill_by_dedupe_key("OKX", "venue_fill_idem_1")
            self.assertIsNotNone(stored)
            self.assertEqual(stored["fill_id"], "fill_idem_1")
            self.assertEqual(stored["fee_rate"], "-0.0005")
            self.assertEqual(stored["exec_type"], "T")
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
            symbol="BTC-USDT-SWAP",
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
            position_intent="close_long",
            execution_attempt_id=f"execution_attempt:{client_order_id}",
            submission_payload={"instId": "BTC-USDT-SWAP", "tdMode": "cross", "posSide": "long"},
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
