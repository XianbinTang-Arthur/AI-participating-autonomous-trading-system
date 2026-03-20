from __future__ import annotations

from decimal import Decimal
from datetime import timedelta

from aats.bootstrap.settings import AATSSettings
from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import OrderIntent, OrderState
from aats.schemas.operator import ExecutionErrorSummary
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.execution_engine.obligations import ExecutionObligationService, ExecutionReservationError
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.storage.base import ExecutionRepository


class OrderManager:
    _OBLIGATION_ATOMIC_FINALIZE_EPSILON = Decimal("1e-12")
    _FILL_BACKFILL_RECENT_LIMIT = 100
    _FILL_BACKFILL_TERMINAL_STATUSES = ("FILLED", "CANCELED", "EXPIRED")
    _TRANSIENT_RETRY_PATTERNS = ("50013", "systems are busy", "service busy", "temporarily unavailable")

    def __init__(
        self,
        *,
        settings: AATSSettings,
        bus: EventBus,
        adapter: ExchangeAdapter,
        execution_repo: ExecutionRepository,
        obligation_service: ExecutionObligationService | None = None,
        execution_outbox_publisher: PostgresExecutionOutboxPublisher | None = None,
        kill_switch: KillSwitch,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.adapter = adapter
        self.execution_repo = execution_repo
        self.obligation_service = obligation_service
        self.execution_outbox_publisher = execution_outbox_publisher
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
            log_event(
                self.logger,
                "duplicate_order_intent_ignored",
                level="warning",
                **correlation_fields(
                    decision_id=intent.decision_id,
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                ),
            )
            return
        cooldown_state = self._transient_close_retry_cooldown_state(intent=intent)
        if cooldown_state is not None:
            await self._persist_order_state(order_state=cooldown_state, key=intent.symbol)
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
        initial_obligation = None
        try:
            if self.obligation_service is not None:
                if self.execution_outbox_publisher is not None:
                    initial_obligation = await self.obligation_service.preview_reservation_for_intent(
                        intent=intent,
                        client_order_id=preview_client_order_id,
                    )
                else:
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
            submission_payload={},
        )
        created_state = await self._persist_order_state(
            order_state=created_state,
            key=intent.symbol,
            obligation=initial_obligation,
        )
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

        persisted_order_state = await self._persist_order_state(
            order_state=order_state,
            key=intent.symbol,
            obligation=self._terminal_outbox_obligation(order_state=order_state, fills=fills),
        )

        for fill in fills:
            if fill.client_order_id != persisted_order_state.client_order_id:
                fill = fill.model_copy(update={"client_order_id": persisted_order_state.client_order_id})
            await self._persist_fill(fill)
        self._finalize_obligation(order_state=persisted_order_state)

    async def sync_exchange_state(self) -> None:
        order_states, fills = await self.adapter.sync(self._sync_candidates())
        persisted_states: list[OrderState] = []
        for order_state in order_states:
            persisted_states.append(
                await self._persist_order_state(
                    order_state=order_state,
                    key=order_state.symbol,
                    obligation=self._terminal_outbox_obligation(order_state=order_state, fills=[]),
                )
            )
        for fill in fills:
            await self._persist_fill(fill)
        for order_state in persisted_states:
            self._finalize_obligation(order_state=order_state)

    def _sync_candidates(self) -> list[OrderState]:
        candidates: dict[str, OrderState] = {
            state.client_order_id: state
            for state in self.execution_repo.open_order_states()
        }
        for state in self.execution_repo.recent_order_states(
            limit=self._FILL_BACKFILL_RECENT_LIMIT,
            statuses=self._FILL_BACKFILL_TERMINAL_STATUSES,
        ):
            if state.filled_qty <= self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
                continue
            if self.execution_repo.fills_for_order(state.client_order_id):
                continue
            candidates.setdefault(state.client_order_id, state)
        return list(candidates.values())

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
        persisted = await self._persist_order_state(
            order_state=state,
            key=current.symbol,
            obligation=self._terminal_outbox_obligation(order_state=state, fills=fills),
        )
        for fill in fills:
            await self._persist_fill(fill)
        self._finalize_obligation(order_state=persisted)
        return persisted

    async def _persist_order_state(
        self,
        *,
        order_state: OrderState,
        key: str,
        obligation=None,
    ) -> OrderState:
        if self.execution_outbox_publisher is not None:
            persisted = await self.execution_outbox_publisher.persist_order_state(
                order_state=order_state,
                key=key,
                obligation=obligation,
            )
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
            return persisted
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
        obligation = None
        if self.obligation_service is not None and self.execution_outbox_publisher is not None:
            obligation = self.obligation_service.preview_obligation_for_fill(fill)
        if self.execution_outbox_publisher is not None:
            saved = await self.execution_outbox_publisher.persist_fill(fill=fill, obligation=obligation)
            if not saved:
                return
        elif not self.execution_repo.save_fill(fill):
            return
        elif self.obligation_service is not None:
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
        if self.execution_outbox_publisher is not None:
            return
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

    def _terminal_outbox_obligation(self, *, order_state: OrderState, fills: list) -> object | None:
        if self.execution_outbox_publisher is None or self.obligation_service is None:
            return None
        if fills:
            return None
        if order_state.status not in {"CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}:
            return None
        if abs(order_state.filled_qty) > self._OBLIGATION_ATOMIC_FINALIZE_EPSILON:
            return None
        return self.obligation_service.preview_obligation_for_order_state(order_state)

    def _transient_close_retry_cooldown_state(self, *, intent: OrderIntent) -> OrderState | None:
        cooldown_seconds = max(self.settings.strategy_transient_close_retry_cooldown_seconds, 0.0)
        if cooldown_seconds <= 0.0:
            return None
        if intent.position_intent not in {"close_long", "close_short"}:
            return None
        if intent.urgency == "high":
            return None
        cutoff = utc_now() - timedelta(seconds=cooldown_seconds)
        for state in self.execution_repo.recent_order_states(limit=25, statuses=("FAILED", "BLOCKED")):
            if state.symbol != intent.symbol or state.position_intent != intent.position_intent:
                continue
            observed_at = state.last_update_ts or state.created_at
            if observed_at < cutoff:
                continue
            if abs(state.requested_qty - intent.quantity) > max(intent.quantity * Decimal("0.2"), Decimal("1e-8")):
                continue
            error_text = f"{state.execution_error or ''} {state.cancel_reason or ''}".lower()
            if not any(pattern in error_text for pattern in self._TRANSIENT_RETRY_PATTERNS):
                continue
            return OrderState(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=intent.idempotency_key or new_id("clord"),
                venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
                exchange_order_id=None,
                status="BLOCKED",
                submission_mode="local_retry_cooldown",
                submitted_ts=None,
                last_update_ts=utc_now(),
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
                execution_error=f"transient_close_retry_cooldown_active:{state.execution_error or state.cancel_reason or 'transient_exchange_failure'}",
                submission_payload={},
            )
        return None
