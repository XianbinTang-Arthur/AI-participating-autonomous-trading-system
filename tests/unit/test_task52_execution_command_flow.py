from __future__ import annotations

import unittest
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_control.command_service import ExecutionCommandProcessor
from aats.services.execution_control.order_service import ExecutionOrderService
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.event_store import InMemoryEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository


class _InMemoryExecutionCommandRepository:
    def __init__(self) -> None:
        self.rows: OrderedDict[str, dict] = OrderedDict()

    def enqueue_command(
        self,
        *,
        command_id: str,
        order_id: str,
        command_type: str,
        idempotency_key: str,
        payload: dict,
        created_at: datetime,
    ) -> None:
        existing = self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return
        self.rows[command_id] = {
            "command_id": command_id,
            "order_id": order_id,
            "command_type": command_type,
            "idempotency_key": idempotency_key,
            "state": "PENDING",
            "attempt_count": 0,
            "last_error": None,
            "command_payload": deepcopy(payload),
            "created_at": created_at,
            "updated_at": created_at,
        }

    def get_command(self, command_id: str) -> dict | None:
        row = self.rows.get(command_id)
        return deepcopy(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> dict | None:
        for row in self.rows.values():
            if row["idempotency_key"] == idempotency_key:
                return deepcopy(row)
        return None

    def pending_commands(self, *, limit: int, sent_stale_before: datetime | None = None) -> list[dict]:
        claimable = []
        for row in self.rows.values():
            if row["state"] == "PENDING":
                claimable.append(row)
                continue
            if row["state"] != "SENT":
                continue
            if sent_stale_before is None or row["updated_at"] <= sent_stale_before:
                claimable.append(row)
        return [deepcopy(row) for row in claimable[:limit]]

    def claim_command(
        self,
        *,
        command_id: str,
        expected_state: str,
        expected_updated_at: datetime,
        updated_at: datetime,
    ) -> bool:
        row = self.rows.get(command_id)
        if row is None:
            return False
        if row["state"] != expected_state or row["updated_at"] != expected_updated_at:
            return False
        row["state"] = "SENT"
        row["attempt_count"] += 1
        row["updated_at"] = updated_at
        row["last_error"] = None
        return True

    def mark_sent(self, command_id: str, updated_at: datetime) -> None:
        row = self.rows[command_id]
        row["state"] = "SENT"
        row["attempt_count"] += 1
        row["updated_at"] = updated_at
        row["last_error"] = None

    def mark_acked(self, command_id: str, updated_at: datetime) -> None:
        row = self.rows[command_id]
        row["state"] = "ACKED"
        row["attempt_count"] += 1
        row["updated_at"] = updated_at
        row["last_error"] = None

    def mark_failed(self, command_id: str, error: str, updated_at: datetime) -> None:
        row = self.rows[command_id]
        row["state"] = "FAILED"
        row["attempt_count"] += 1
        row["updated_at"] = updated_at
        row["last_error"] = error

    def mark_abandoned(self, command_id: str, reason: str, updated_at: datetime) -> None:
        row = self.rows[command_id]
        row["state"] = "ABANDONED"
        row["attempt_count"] += 1
        row["updated_at"] = updated_at
        row["last_error"] = reason

    def first(self) -> dict:
        return deepcopy(next(iter(self.rows.values())))


class _StaticExecutionOrderRepository:
    def __init__(self, row: dict | None = None) -> None:
        self.row = deepcopy(row)

    def get_order_by_client_order_id(self, client_order_id: str) -> dict | None:
        if self.row is None:
            return None
        if self.row["client_order_id"] != client_order_id:
            return None
        return deepcopy(self.row)


class _QueueOnlyAdapter:
    def __init__(self) -> None:
        self.submit_count = 0
        self.cancel_count = 0

    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return f"cl{intent.idempotency_key}"

    async def submit(self, intent: OrderIntent):
        self.submit_count += 1
        raise AssertionError("submit should not run before command processor drains pending commands")

    async def cancel(self, order_state: OrderState):
        self.cancel_count += 1
        raise AssertionError("cancel should not run before command processor drains pending commands")

    async def sync(self, open_order_states):
        return [], []

    def readiness(self):
        return {"backend": "paper"}


class _SubmittedThenCanceledAdapter:
    def __init__(self) -> None:
        self.submit_count = 0
        self.cancel_count = 0

    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return f"cl{intent.idempotency_key}"

    async def submit(self, intent: OrderIntent):
        self.submit_count += 1
        now = utc_now()
        state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=f"cl{intent.idempotency_key}",
            venue="OKX",
            exchange_order_id=f"ord_{intent.intent_id}",
            status="SUBMITTED",
            submission_mode="phase2_test",
            submitted_ts=now,
            last_update_ts=now,
            last_exchange_update_ts=now,
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            submission_payload={},
        )
        return state, []

    async def cancel(self, order_state: OrderState):
        self.cancel_count += 1
        now = utc_now()
        state = order_state.model_copy(
            update={
                "status": "CANCELED",
                "last_update_ts": now,
                "last_exchange_update_ts": now,
                "canceled_ts": now,
                "remaining_qty": order_state.remaining_qty,
                "filled_qty": order_state.filled_qty,
            }
        )
        return state, []

    async def sync(self, open_order_states):
        return [], []

    def readiness(self):
        return {"backend": "okx"}


def _intent(*, suffix: str) -> OrderIntent:
    return OrderIntent(
        intent_id=f"intent_{suffix}",
        decision_id=f"decision_{suffix}",
        symbol="BTC-USDT",
        side="buy",
        quantity=Decimal("0.001"),
        execution_style="exchange",
        order_type="market",
        urgency="medium",
        time_in_force="IOC",
        reduce_only=False,
        close_only=False,
        idempotency_key=suffix,
    )


def _intent_message(intent: OrderIntent) -> dict:
    envelope = build_envelope(
        topic=topics.ORDER_INTENTS,
        key=intent.symbol,
        payload_model=intent,
        source_component="test",
    )
    return {"topic": topics.ORDER_INTENTS, "key": intent.symbol, "payload": envelope.model_dump(mode="json")}


class TestTask52ExecutionCommandFlow(unittest.IsolatedAsyncioTestCase):
    async def test_phase2_handle_order_intent_enqueues_submit_command_before_adapter_execution(self) -> None:
        command_repo = _InMemoryExecutionCommandRepository()
        adapter = _QueueOnlyAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({"execution_command_flow_enabled": True}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=InMemoryExecutionRepository(),
            persistent_order_service=ExecutionOrderService(execution_command_repo=command_repo),
            kill_switch=KillSwitch(),
        )
        intent = _intent(suffix="phase2_enqueue")

        await manager.handle_order_intent(_intent_message(intent))

        self.assertEqual(adapter.submit_count, 0)
        state = manager.execution_repo.get_order_state("clphase2_enqueue")
        self.assertIsNotNone(state)
        self.assertEqual(state.status, "CREATED")
        command = command_repo.first()
        self.assertEqual(command["command_type"], "submit")
        self.assertEqual(command["state"], "PENDING")
        self.assertEqual(command["command_payload"]["client_order_id"], "clphase2_enqueue")

    async def test_phase2_command_processor_executes_submit_and_marks_command_acked(self) -> None:
        command_repo = _InMemoryExecutionCommandRepository()
        manager = OrderManager(
            settings=AATSSettings.model_validate({"execution_command_flow_enabled": True}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=PaperExecutionAdapter(price_provider=lambda _symbol: Decimal("100"), taker_fee_bps=5.0),
            execution_repo=InMemoryExecutionRepository(),
            persistent_order_service=ExecutionOrderService(execution_command_repo=command_repo),
            kill_switch=KillSwitch(),
        )
        intent = _intent(suffix="phase2_submit")
        processor = ExecutionCommandProcessor(
            execution_command_repo=command_repo,
            submit_executor=lambda next_intent, client_order_id=None: manager.process_submit_command(
                intent=next_intent,
                client_order_id=client_order_id,
            ),
            cancel_executor=lambda client_order_id: manager.process_cancel_command(client_order_id=client_order_id),
        )

        await manager.handle_order_intent(_intent_message(intent))
        processed = await processor.process_pending()

        self.assertEqual(processed, 1)
        command = command_repo.first()
        self.assertEqual(command["state"], "ACKED")
        state = manager.execution_repo.get_order_state("clphase2_submit")
        self.assertIsNotNone(state)
        self.assertEqual(state.status, "FILLED")
        self.assertEqual(len(manager.execution_repo.fills()), 1)

    async def test_phase2_sent_submit_command_is_not_replayed_after_claim_ambiguity(self) -> None:
        command_repo = _InMemoryExecutionCommandRepository()
        intent = _intent(suffix="phase2_replay")
        command_repo.enqueue_command(
            command_id="cmd_phase2_replay",
            order_id="clphase2_replay",
            command_type="submit",
            idempotency_key="submit:intent_phase2_replay",
            payload={
                "intent": intent.model_dump(mode="python"),
                "client_order_id": "clphase2_replay",
            },
            created_at=utc_now(),
        )
        command_repo.mark_sent("cmd_phase2_replay", updated_at=utc_now())
        repo = InMemoryExecutionRepository()
        now = utc_now()
        repo.save_order_state(
            OrderState(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id="clphase2_replay",
                venue="PAPER",
                exchange_order_id="paper_phase2_replay",
                status="FILLED",
                submission_mode="paper_local",
                submitted_ts=now,
                last_update_ts=now,
                last_exchange_update_ts=now,
                requested_qty=intent.quantity,
                filled_qty=intent.quantity,
                remaining_qty=Decimal("0"),
                average_fill_price=Decimal("100"),
                fees=Decimal("0.00005"),
                submission_payload={},
            )
        )
        adapter = _QueueOnlyAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({"execution_command_flow_enabled": True}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=repo,
            persistent_order_service=ExecutionOrderService(execution_command_repo=command_repo),
            kill_switch=KillSwitch(),
        )
        processor = ExecutionCommandProcessor(
            execution_command_repo=command_repo,
            submit_executor=lambda next_intent, client_order_id=None: manager.process_submit_command(
                intent=next_intent,
                client_order_id=client_order_id,
            ),
            cancel_executor=lambda client_order_id: manager.process_cancel_command(client_order_id=client_order_id),
        )

        processed = await processor.process_pending()

        self.assertEqual(processed, 0)
        self.assertEqual(adapter.submit_count, 0)
        self.assertEqual(command_repo.get_command("cmd_phase2_replay")["state"], "SENT")

    async def test_phase2_sent_cancel_command_can_be_retried(self) -> None:
        command_repo = _InMemoryExecutionCommandRepository()
        adapter = _SubmittedThenCanceledAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({"execution_command_flow_enabled": True}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=InMemoryExecutionRepository(),
            persistent_order_service=ExecutionOrderService(execution_command_repo=command_repo),
            kill_switch=KillSwitch(),
        )
        processor = ExecutionCommandProcessor(
            execution_command_repo=command_repo,
            submit_executor=lambda next_intent, client_order_id=None: manager.process_submit_command(
                intent=next_intent,
                client_order_id=client_order_id,
            ),
            cancel_executor=lambda client_order_id: manager.process_cancel_command(client_order_id=client_order_id),
        )
        intent = _intent(suffix="phase2_sent_cancel")

        await manager.handle_order_intent(_intent_message(intent))
        await processor.process_pending()
        await manager.cancel_order("clphase2_sent_cancel")

        cancel_command = command_repo.get_by_idempotency_key("cancel:clphase2_sent_cancel")
        self.assertIsNotNone(cancel_command)
        assert cancel_command is not None
        command_repo.mark_sent(str(cancel_command["command_id"]), updated_at=utc_now())

        processed = await processor.process_pending()

        self.assertEqual(processed, 1)
        self.assertEqual(adapter.cancel_count, 1)
        self.assertEqual(command_repo.get_by_idempotency_key("cancel:clphase2_sent_cancel")["state"], "ACKED")

    async def test_phase2_cancel_command_is_enqueued_then_applied_by_command_processor(self) -> None:
        command_repo = _InMemoryExecutionCommandRepository()
        adapter = _SubmittedThenCanceledAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({"execution_command_flow_enabled": True}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=InMemoryExecutionRepository(),
            persistent_order_service=ExecutionOrderService(execution_command_repo=command_repo),
            kill_switch=KillSwitch(),
        )
        processor = ExecutionCommandProcessor(
            execution_command_repo=command_repo,
            submit_executor=lambda next_intent, client_order_id=None: manager.process_submit_command(
                intent=next_intent,
                client_order_id=client_order_id,
            ),
            cancel_executor=lambda client_order_id: manager.process_cancel_command(client_order_id=client_order_id),
        )
        intent = _intent(suffix="phase2_cancel")

        await manager.handle_order_intent(_intent_message(intent))
        await processor.process_pending()
        pending = await manager.cancel_order("clphase2_cancel")

        self.assertEqual(adapter.cancel_count, 0)
        self.assertEqual(pending.status, "CANCEL_PENDING")
        pending_cancel_command = command_repo.get_by_idempotency_key("cancel:clphase2_cancel")
        self.assertIsNotNone(pending_cancel_command)
        self.assertEqual(pending_cancel_command["state"], "PENDING")

        await processor.process_pending()

        canceled = manager.execution_repo.get_order_state("clphase2_cancel")
        self.assertIsNotNone(canceled)
        self.assertEqual(canceled.status, "CANCELED")
        self.assertEqual(adapter.cancel_count, 1)
        self.assertEqual(command_repo.get_by_idempotency_key("cancel:clphase2_cancel")["state"], "ACKED")

    async def test_phase2_cancel_before_submit_abandons_pending_submit_and_never_calls_adapter_cancel(self) -> None:
        command_repo = _InMemoryExecutionCommandRepository()
        adapter = _QueueOnlyAdapter()
        manager = OrderManager(
            settings=AATSSettings.model_validate({"execution_command_flow_enabled": True}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=adapter,
            execution_repo=InMemoryExecutionRepository(),
            persistent_order_service=ExecutionOrderService(execution_command_repo=command_repo),
            kill_switch=KillSwitch(),
        )
        processor = ExecutionCommandProcessor(
            execution_command_repo=command_repo,
            submit_executor=lambda next_intent, client_order_id=None: manager.process_submit_command(
                intent=next_intent,
                client_order_id=client_order_id,
            ),
            cancel_executor=lambda client_order_id: manager.process_cancel_command(client_order_id=client_order_id),
        )
        intent = _intent(suffix="phase2_cancel_before_submit")

        await manager.handle_order_intent(_intent_message(intent))
        pending = await manager.cancel_order("clphase2_cancel_before_submit")

        self.assertEqual(pending.status, "CANCELED")
        self.assertEqual(command_repo.get_by_idempotency_key("submit:intent_phase2_cancel_before_submit")["state"], "ABANDONED")
        self.assertIsNone(command_repo.get_by_idempotency_key("cancel:clphase2_cancel_before_submit"))

        await processor.process_pending()

        submit_command = command_repo.get_by_idempotency_key("submit:intent_phase2_cancel_before_submit")
        final_state = manager.execution_repo.get_order_state("clphase2_cancel_before_submit")
        self.assertIsNotNone(submit_command)
        self.assertEqual(submit_command["state"], "ABANDONED")
        self.assertIsNotNone(final_state)
        self.assertEqual(final_state.status, "CANCELED")
        self.assertEqual(adapter.submit_count, 0)
        self.assertEqual(adapter.cancel_count, 0)

    async def test_phase2_cancel_can_hydrate_order_from_execution_repo_when_legacy_state_is_missing(self) -> None:
        command_repo = _InMemoryExecutionCommandRepository()
        order_repo = _StaticExecutionOrderRepository(
            {
                "order_id": "clphase2_repo_only",
                "intent_id": "intent_phase2_repo_only",
                "decision_id": "decision_phase2_repo_only",
                "client_order_id": "clphase2_repo_only",
                "venue_order_id": None,
                "symbol": "BTC-USDT",
                "side": "buy",
                "order_type": "market",
                "time_in_force": "IOC",
                "requested_qty": Decimal("0.001"),
                "product_type": "spot",
                "margin_mode": "cash",
                "execution_action": "enter",
                "position_intent": "open_long",
                "state": "CREATED",
                "state_version": 1,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "raw_payload": {
                    "source_system": "phase2_execution_command_flow",
                    "target_leverage": 1.0,
                    "exposure_side": "flat",
                },
            }
        )
        manager = OrderManager(
            settings=AATSSettings.model_validate({"execution_command_flow_enabled": True}),
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            adapter=_QueueOnlyAdapter(),
            execution_repo=InMemoryExecutionRepository(),
            persistent_order_service=ExecutionOrderService(
                execution_command_repo=command_repo,
                execution_order_repo=order_repo,
            ),
            kill_switch=KillSwitch(),
        )

        resolved = manager.resolve_order_state_for_control("clphase2_repo_only")
        canceled = await manager.process_cancel_command(client_order_id="clphase2_repo_only")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.status, "CREATED")
        self.assertEqual(canceled.status, "CANCELED")
        self.assertEqual(manager.execution_repo.get_order_state("clphase2_repo_only").status, "CANCELED")
