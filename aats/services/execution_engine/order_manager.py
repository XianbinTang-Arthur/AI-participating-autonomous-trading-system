from __future__ import annotations

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import OrderIntent, OrderState
from aats.schemas.operator import ExecutionErrorSummary
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.execution_engine.obligations import ExecutionObligationService, ExecutionReservationError
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.base import ExecutionRepository


class OrderManager:
    def __init__(
        self,
        *,
        bus: EventBus,
        adapter: ExchangeAdapter,
        execution_repo: ExecutionRepository,
        obligation_service: ExecutionObligationService | None = None,
        kill_switch: KillSwitch,
    ) -> None:
        self.bus = bus
        self.adapter = adapter
        self.execution_repo = execution_repo
        self.obligation_service = obligation_service
        self.kill_switch = kill_switch
        self.logger = get_logger("aats.execution_engine")

    async def handle_order_intent(self, message: dict) -> None:
        intent = parse_payload(message, OrderIntent)
        if self.kill_switch.halted:
            log_event(
                self.logger,
                "order_intent_blocked",
                level="warning",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    reason="kill_switch_active",
                ),
            )
            return
        if self.execution_repo.has_intent(intent.intent_id):
            return

        log_event(
            self.logger,
            "order_intent_received",
            **correlation_fields(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
            ),
        )
        preview_client_order_id_fn = getattr(self.adapter, "preview_client_order_id", None)
        preview_client_order_id = (
            preview_client_order_id_fn(intent)
            if callable(preview_client_order_id_fn)
            else None
        ) or intent.idempotency_key or new_id("clord")
        try:
            if self.obligation_service is not None:
                await self.obligation_service.reserve_for_intent(
                    intent=intent,
                    client_order_id=preview_client_order_id,
                )
        except ExecutionReservationError as exc:
            blocked_state = OrderState(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=preview_client_order_id,
                venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
                exchange_order_id=None,
                status="BLOCKED",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=utc_now(),
                requested_qty=intent.quantity,
                filled_qty=0.0,
                remaining_qty=intent.quantity,
                average_fill_price=None,
                fees=0.0,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                position_intent=intent.position_intent,
                execution_error=str(exc),
                submission_payload={},
            )
            await self._persist_order_state(order_state=blocked_state, key=intent.symbol)
            return
        created_state = OrderState(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=preview_client_order_id,
            venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
            exchange_order_id=None,
            status="CREATED",
            submission_mode="local_order_manager",
            submitted_ts=None,
            last_update_ts=utc_now(),
            requested_qty=intent.quantity,
            filled_qty=0.0,
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=0.0,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            position_intent=intent.position_intent,
            submission_payload={},
        )
        created_state = await self._persist_order_state(order_state=created_state, key=intent.symbol)
        submitting_state = created_state.model_copy(
            update={
                "status": "SUBMITTING",
                "last_update_ts": utc_now(),
            }
        )
        await self._persist_order_state(order_state=submitting_state, key=intent.symbol)

        try:
            order_state, fills = await self.adapter.submit(intent)
        except Exception as exc:
            order_state = OrderState(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=preview_client_order_id,
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
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                position_intent=intent.position_intent,
                cancel_reason=str(exc),
                execution_error=str(exc),
                submission_payload={},
            )
            fills = []
            log_event(
                self.logger,
                "order_submit_failed",
                level="error",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    error=str(exc),
                ),
            )

        persisted_order_state = await self._persist_order_state(order_state=order_state, key=intent.symbol)

        for fill in fills:
            if fill.client_order_id != persisted_order_state.client_order_id:
                fill = fill.model_copy(update={"client_order_id": persisted_order_state.client_order_id})
            await self._persist_fill(fill)
        self._finalize_obligation(order_state=persisted_order_state)

    async def sync_exchange_state(self) -> None:
        order_states, fills = await self.adapter.sync(self.execution_repo.open_order_states())
        persisted_states: list[OrderState] = []
        for order_state in order_states:
            persisted_states.append(await self._persist_order_state(order_state=order_state, key=order_state.symbol))
        for fill in fills:
            await self._persist_fill(fill)
        for order_state in persisted_states:
            self._finalize_obligation(order_state=order_state)

    async def cancel_order(self, client_order_id: str) -> OrderState:
        current = self.execution_repo.get_order_state(client_order_id)
        if current is None:
            raise KeyError(f"order_state_not_found client_order_id={client_order_id}")
        cancel_pending = current.model_copy(
            update={
                "status": "CANCEL_PENDING",
                "cancellation_requested_ts": utc_now(),
                "last_update_ts": utc_now(),
            }
        )
        persisted_pending = await self._persist_order_state(order_state=cancel_pending, key=current.symbol)
        state, fills = await self.adapter.cancel(persisted_pending)
        persisted = await self._persist_order_state(order_state=state, key=current.symbol)
        for fill in fills:
            await self._persist_fill(fill)
        self._finalize_obligation(order_state=persisted)
        return persisted

    async def _persist_order_state(self, *, order_state: OrderState, key: str) -> OrderState:
        previous = self.execution_repo.get_order_state(order_state.client_order_id)
        persisted = self.execution_repo.save_order_state(order_state)
        log_event(
            self.logger,
            "order_state_persisted",
            **correlation_fields(
                decision_id=persisted.decision_id,
                intent_id=persisted.intent_id,
                order_id=persisted.client_order_id,
                status=persisted.status,
                venue=persisted.venue,
                submission_mode=persisted.submission_mode,
                execution_error=persisted.execution_error,
            ),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.ORDER_UPDATES,
            key=key,
            payload_model=persisted,
            source_component="execution_engine",
        )
        await self._publish_execution_error_summary(previous=previous, persisted=persisted)
        return persisted

    async def _persist_fill(self, fill) -> None:
        if not self.execution_repo.save_fill(fill):
            return
        if self.obligation_service is not None:
            self.obligation_service.consume_for_fill(fill)
        log_event(
            self.logger,
            "fill_event_created",
            **correlation_fields(
                decision_id=fill.decision_id,
                intent_id=fill.intent_id,
                order_id=fill.client_order_id,
                fill_id=fill.fill_id,
                symbol=fill.symbol,
                fill_qty=fill.fill_qty,
                fill_price=fill.fill_price,
                venue=fill.venue,
            ),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.FILL_EVENTS,
            key=fill.symbol,
            payload_model=fill,
            source_component="execution_engine",
        )

    async def _publish_execution_error_summary(
        self,
        *,
        previous: OrderState | None,
        persisted: OrderState,
    ) -> None:
        if persisted.status not in {"FAILED", "REJECTED", "BLOCKED"}:
            return
        if previous is not None and previous.status == persisted.status and previous.execution_error == persisted.execution_error:
            return
        summary = ExecutionErrorSummary(
            subsystem="execution_engine",
            severity="error" if persisted.status == "FAILED" else "warning",
            message=persisted.execution_error or persisted.cancel_reason or persisted.status,
            decision_id=persisted.decision_id,
            intent_id=persisted.intent_id,
            order_id=persisted.client_order_id,
            status=persisted.status,
            observed_at=persisted.last_update_ts or persisted.created_at,
        )
        await publish_model(
            bus=self.bus,
            topic=topics.EXECUTION_ERROR_SUMMARIES,
            key=persisted.symbol,
            payload_model=summary,
            source_component="execution_engine",
        )

    def _finalize_obligation(self, *, order_state: OrderState) -> None:
        if self.obligation_service is None:
            return
        self.obligation_service.finalize_for_order_state(order_state)
