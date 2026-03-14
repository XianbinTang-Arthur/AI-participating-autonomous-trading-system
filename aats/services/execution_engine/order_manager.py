from __future__ import annotations

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderIntent, OrderState
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.base import ExecutionRepository


class OrderManager:
    def __init__(
        self,
        *,
        bus: EventBus,
        adapter: ExchangeAdapter,
        execution_repo: ExecutionRepository,
        kill_switch: KillSwitch,
    ) -> None:
        self.bus = bus
        self.adapter = adapter
        self.execution_repo = execution_repo
        self.kill_switch = kill_switch
        self.logger = get_logger("aats.execution_engine")

    async def handle_order_intent(self, message: dict) -> None:
        intent = parse_payload(message, OrderIntent)
        if self.kill_switch.halted:
            log_event(
                self.logger,
                "order_intent_blocked",
                level="warning",
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                reason="kill_switch_active",
            )
            return
        if self.execution_repo.has_intent(intent.intent_id):
            return

        log_event(
            self.logger,
            "order_intent_received",
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
        )

        try:
            order_state, fills = await self.adapter.submit(intent)
        except Exception as exc:
            order_state = OrderState(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=intent.idempotency_key,
                venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
                exchange_order_id=None,
                status="FAILED",
                submission_mode="adapter_exception",
                submitted_ts=utc_now(),
                last_update_ts=utc_now(),
                requested_qty=intent.quantity,
                filled_qty=0.0,
                remaining_qty=intent.quantity,
                average_fill_price=None,
                fees=0.0,
                cancel_reason=str(exc),
                execution_error=str(exc),
                submission_payload={},
            )
            fills = []
            log_event(
                self.logger,
                "order_submit_failed",
                level="error",
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                error=str(exc),
            )

        await self._persist_order_state(order_state=order_state, key=intent.symbol)

        for fill in fills:
            await self._persist_fill(fill)

    async def sync_exchange_state(self) -> None:
        order_states, fills = await self.adapter.sync(self.execution_repo.open_order_states())
        for order_state in order_states:
            await self._persist_order_state(order_state=order_state, key=order_state.symbol)
        for fill in fills:
            await self._persist_fill(fill)

    async def _persist_order_state(self, *, order_state: OrderState, key: str) -> None:
        self.execution_repo.save_order_state(order_state)
        log_event(
            self.logger,
            "order_state_persisted",
            decision_id=order_state.decision_id,
            intent_id=order_state.intent_id,
            status=order_state.status,
            venue=order_state.venue,
            submission_mode=order_state.submission_mode,
        )
        await publish_model(
            bus=self.bus,
            topic=topics.ORDER_UPDATES,
            key=key,
            payload_model=order_state,
            source_component="execution_engine",
        )

    async def _persist_fill(self, fill) -> None:
        if not self.execution_repo.save_fill(fill):
            return
        log_event(
            self.logger,
            "fill_event_created",
            decision_id=fill.decision_id,
            fill_id=fill.fill_id,
            symbol=fill.symbol,
            fill_qty=fill.fill_qty,
            fill_price=fill.fill_price,
            venue=fill.venue,
        )
        await publish_model(
            bus=self.bus,
            topic=topics.FILL_EVENTS,
            key=fill.symbol,
            payload_model=fill,
            source_component="execution_engine",
        )
