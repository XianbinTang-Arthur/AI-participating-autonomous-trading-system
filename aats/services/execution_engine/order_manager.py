from __future__ import annotations

import asyncio
from decimal import Decimal
from datetime import timedelta, timezone
from typing import Callable

from aats.bootstrap.settings import AATSSettings
from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import (
    FillEvent,
    LegOrderIntent,
    OrderIntent,
    OrderObligation,
    OrderState,
    execution_attempt_id_from_components,
    leg_intent_from_order_intent,
    order_intent_from_leg_order_intent,
)
from aats.schemas.operator import ExecutionErrorSummary
from aats.services.execution_control.order_service import ExecutionOrderService
from aats.services.execution_control.order_state_machine import OrderStateMachine
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.execution_engine.bundle_status import (
    apply_strategy_bundle_status_reason_codes,
    derive_strategy_bundle_status,
)
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.execution_engine.obligations import ExecutionObligationService, ExecutionReservationError
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.ledger.posting import Phase1LedgerMirrorService
from aats.services.strategy_overlay_rollout import overlay_mode_from_execution_mode, overlay_rollout_status
from aats.storage.base import ExecutionRepository
from aats.storage.execution_fill_repo_v2 import ExecutionFillRepositoryV2
from aats.storage.execution_order_repo import ExecutionOrderHistoryRepository, ExecutionOrderRepository


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
        persistent_order_service: ExecutionOrderService | None = None,
        shadow_execution_service: Phase1ExecutionShadowService | None = None,
        shadow_execution_order_repo: ExecutionOrderRepository | None = None,
        shadow_execution_order_history_repo: ExecutionOrderHistoryRepository | None = None,
        shadow_execution_fill_repo: ExecutionFillRepositoryV2 | None = None,
        shadow_ledger_mirror_service: Phase1LedgerMirrorService | None = None,
        leg_risk_evaluator: Callable[[LegOrderIntent], object] | None = None,
        strategy_runtime_repo=None,
        kill_switch: KillSwitch,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.adapter = adapter
        self.execution_repo = execution_repo
        self.obligation_service = obligation_service
        self.execution_outbox_publisher = execution_outbox_publisher
        self.persistent_order_service = persistent_order_service
        self.shadow_execution_service = shadow_execution_service or self._build_legacy_shadow_execution_service(
            shadow_execution_order_repo=shadow_execution_order_repo,
            shadow_execution_order_history_repo=shadow_execution_order_history_repo,
            shadow_execution_fill_repo=shadow_execution_fill_repo,
        )
        self.shadow_ledger_mirror_service = shadow_ledger_mirror_service
        self.leg_risk_evaluator = leg_risk_evaluator
        self.strategy_runtime_repo = strategy_runtime_repo
        self.kill_switch = kill_switch
        self.order_state_machine = OrderStateMachine()
        # Serialize reservation preview/persist so concurrent intents cannot over-reserve the same balance window.
        self._reservation_lock = asyncio.Lock()
        self.logger = get_logger("aats.execution_engine")

    async def handle_order_intent(self, message: dict) -> None:
        intent = parse_payload(message, OrderIntent)
        await self._handle_normalized_order_intent(
            intent=intent,
            leg_intent=leg_intent_from_order_intent(intent),
        )

    async def handle_leg_order_intent(self, message: dict) -> None:
        leg_intent = parse_payload(message, LegOrderIntent)
        await self.submit_leg_order(leg_intent=leg_intent)

    async def submit_leg_order(self, *, leg_intent: LegOrderIntent) -> None:
        await self._handle_normalized_order_intent(
            intent=order_intent_from_leg_order_intent(leg_intent),
            leg_intent=leg_intent,
        )

    async def _handle_normalized_order_intent(
        self,
        *,
        intent: OrderIntent,
        leg_intent: LegOrderIntent | None = None,
    ) -> None:
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
        intent, leg_intent = self._apply_execution_attempt_id(
            intent=intent,
            client_order_id=preview_client_order_id,
            leg_intent=leg_intent,
        )
        initial_obligation = None
        async with self._reservation_lock:
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
            intent, leg_intent, blocked_state = self._apply_leg_submit_guards(
                intent=intent,
                client_order_id=preview_client_order_id,
                leg_intent=leg_intent,
            )
            if blocked_state is not None:
                await self._persist_order_state(order_state=blocked_state, key=intent.symbol)
                return
            try:
                if self.obligation_service is not None:
                    initial_obligation = await self.obligation_service.preview_reservation_for_intent(
                        intent=intent,
                        client_order_id=preview_client_order_id,
                    )
            except ExecutionReservationError as exc:
                blocked_state = OrderState(
                    decision_id=intent.decision_id,
                    execution_chain_id=intent.execution_chain_id,
                    execution_attempt_id=intent.execution_attempt_id,
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
                    reduce_only=intent.reduce_only,
                    close_only=intent.close_only,
                    td_mode=intent.td_mode,
                    position_mode=intent.position_mode,
                    pos_side=intent.pos_side,
                    reduce_only_reason=intent.reduce_only_reason,
                    close_only_reason=intent.close_only_reason,
                    instrument_family=intent.instrument_family,
                    settle_currency=intent.settle_currency,
                    product_type=intent.product_type,
                    target_leverage=intent.target_leverage,
                    margin_mode=intent.margin_mode,
                    exposure_side=intent.exposure_side,
                    execution_action=intent.execution_action,
                    leg_action=intent.leg_action,
                    position_intent=intent.position_intent,
                    leg_intent_id=intent.leg_intent_id,
                    strategy_family=intent.strategy_family,
                    strategy_sleeve_id=intent.strategy_sleeve_id,
                    allocation_id=intent.allocation_id,
                    strategy_bundle_id=intent.strategy_bundle_id,
                    strategy_leg_role=intent.strategy_leg_role,
                    strategy_pair_id=intent.strategy_pair_id,
                    strategy_opportunity_kind=intent.strategy_opportunity_kind,
                    strategy_execution_mode=intent.strategy_execution_mode,
                    strategy_state_phase=intent.strategy_state_phase,
                    execution_error=str(exc),
                    submission_payload={},
                )
                await self._persist_order_state(order_state=blocked_state, key=intent.symbol)
                return
            created_state = OrderState(
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=intent.execution_attempt_id,
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
                reduce_only=intent.reduce_only,
                close_only=intent.close_only,
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                leg_intent_id=intent.leg_intent_id,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
                strategy_pair_id=intent.strategy_pair_id,
                strategy_opportunity_kind=intent.strategy_opportunity_kind,
                strategy_execution_mode=intent.strategy_execution_mode,
                strategy_state_phase=intent.strategy_state_phase,
                submission_payload={},
            )
            submit_command = None
            if self.persistent_order_service is not None:
                submit_command = {
                    "command_id": new_id("cmd"),
                    "command_type": "submit",
                    "idempotency_key": self.persistent_order_service.submit_command_idempotency_key(intent.intent_id),
                    "payload": self.persistent_order_service.submit_command_payload(
                        intent=intent,
                        client_order_id=created_state.client_order_id,
                    ),
                    "created_at": utc_now(),
                }
            created_state = await self._persist_order_state(
                order_state=created_state,
                key=intent.symbol,
                obligation=initial_obligation,
                intent=intent,
                command=submit_command,
            )
            if (
                self.obligation_service is not None
                and self.execution_outbox_publisher is None
                and initial_obligation is not None
            ):
                self.obligation_service.persist_previewed_obligation(initial_obligation)
        self._shadow_sync_obligation(initial_obligation, reason="reservation_hold", related_fill=None)
        if self.persistent_order_service is not None:
            if not self._submit_command_persisted_transactionally():
                try:
                    self.persistent_order_service.enqueue_submit(
                        intent=intent,
                        client_order_id=created_state.client_order_id,
                    )
                except Exception as exc:
                    failed_state = created_state.model_copy(
                        update={
                            "status": "FAILED",
                            "submission_mode": "phase2_enqueue_failed",
                            "submitted_ts": utc_now(),
                            "last_update_ts": utc_now(),
                            "cancel_reason": str(exc),
                            "execution_error": str(exc),
                        }
                    )
                    failed_state = await self._persist_order_state(
                        order_state=failed_state,
                        key=intent.symbol,
                        obligation=self._terminal_outbox_obligation(order_state=failed_state, fills=[]),
                    )
                    self._finalize_obligation(order_state=failed_state)
                    raise
            return
        await self.process_submit_command(
            intent=intent,
            client_order_id=created_state.client_order_id,
            leg_intent=leg_intent,
        )

    async def process_submit_command(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str | None = None,
        leg_intent: LegOrderIntent | None = None,
    ) -> OrderState:
        if client_order_id is not None:
            current = self.execution_repo.get_order_state(client_order_id)
            if current is not None and current.status not in {"CREATED", "SUBMITTING"}:
                return current
        resolved_client_order_id = client_order_id or intent.idempotency_key or new_id("clord")
        intent, leg_intent = self._apply_execution_attempt_id(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )
        intent, leg_intent, blocked_state = self._apply_leg_submit_guards(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )
        if blocked_state is not None:
            return await self._persist_order_state(order_state=blocked_state, key=intent.symbol)
        return await self._execute_submit_intent(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )

    async def process_cancel_command(self, *, client_order_id: str) -> OrderState:
        current = self.resolve_order_state_for_control(client_order_id)
        if current is None:
            raise KeyError(f"order_state_not_found client_order_id={client_order_id}")
        if self.order_state_machine.is_terminal(current.status):
            return current
        return await self._execute_cancel_from_state(current)

    async def _execute_submit_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str | None = None,
        leg_intent: LegOrderIntent | None = None,
    ) -> OrderState:
        resolved_client_order_id = client_order_id or intent.idempotency_key or new_id("clord")
        intent, leg_intent = self._apply_execution_attempt_id(
            intent=intent,
            client_order_id=resolved_client_order_id,
            leg_intent=leg_intent,
        )
        current = self.execution_repo.get_order_state(resolved_client_order_id)
        if current is None:
            current = OrderState(
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=intent.execution_attempt_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=resolved_client_order_id,
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
                reduce_only=intent.reduce_only,
                close_only=intent.close_only,
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                leg_intent_id=intent.leg_intent_id,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
                submission_payload={},
            )
        submitting_state = current.model_copy(
            update={
                "status": "SUBMITTING",
                "last_update_ts": utc_now(),
            }
        )
        await self._persist_order_state(order_state=submitting_state, key=intent.symbol)

        try:
            if leg_intent is not None:
                submit_leg_order = getattr(self.adapter, "submit_leg_order", None)
                if callable(submit_leg_order):
                    order_state, fills = await submit_leg_order(leg_intent)
                else:
                    order_state, fills = await self.adapter.submit(intent)
            else:
                order_state, fills = await self.adapter.submit(intent)
        except Exception as exc:
            order_state = OrderState(
                decision_id=intent.decision_id,
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=intent.execution_attempt_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                client_order_id=resolved_client_order_id,
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
                reduce_only=intent.reduce_only,
                close_only=intent.close_only,
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                leg_intent_id=intent.leg_intent_id,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
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
                fill = fill.model_copy(
                    update={
                        "client_order_id": persisted_order_state.client_order_id,
                        "execution_attempt_id": (
                            persisted_order_state.execution_attempt_id
                            or execution_attempt_id_from_components(
                                client_order_id=persisted_order_state.client_order_id,
                                execution_chain_id=persisted_order_state.execution_chain_id,
                                intent_id=persisted_order_state.intent_id,
                            )
                        ),
                    }
                )
            await self._persist_fill(fill)
        self._finalize_obligation(order_state=persisted_order_state)
        return persisted_order_state

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
        current = self.resolve_order_state_for_control(client_order_id)
        if current is None:
            raise KeyError(f"order_state_not_found client_order_id={client_order_id}")
        if self.order_state_machine.is_terminal(current.status):
            return current
        pre_submit_canceled = await self._cancel_pending_submit_before_exchange_ack(current)
        if pre_submit_canceled is not None:
            return pre_submit_canceled
        cancel_pending = current.model_copy(
            update={
                "status": "CANCEL_PENDING",
                "cancellation_requested_ts": utc_now(),
                "last_update_ts": utc_now(),
            }
        )
        persisted_pending = await self._persist_order_state(order_state=cancel_pending, key=current.symbol)
        if self.persistent_order_service is not None:
            self.persistent_order_service.enqueue_cancel(
                order_state=persisted_pending,
                reason="operator_requested_cancel",
            )
            return persisted_pending
        return await self._execute_cancel_from_state(persisted_pending)

    async def _execute_cancel_from_state(self, order_state: OrderState) -> OrderState:
        current = order_state
        pre_submit_canceled = await self._cancel_pending_submit_before_exchange_ack(current)
        if pre_submit_canceled is not None:
            return pre_submit_canceled
        if current.status != "CANCEL_PENDING":
            current = current.model_copy(
                update={
                    "status": "CANCEL_PENDING",
                    "cancellation_requested_ts": utc_now(),
                    "last_update_ts": utc_now(),
                }
            )
            current = await self._persist_order_state(order_state=current, key=current.symbol)
        pre_submit_canceled = await self._cancel_pending_submit_before_exchange_ack(current)
        if pre_submit_canceled is not None:
            return pre_submit_canceled
        state, fills = await self.adapter.cancel(current)
        persisted = await self._persist_order_state(
            order_state=state,
            key=current.symbol,
            obligation=self._terminal_outbox_obligation(order_state=state, fills=fills),
        )
        for fill in fills:
            await self._persist_fill(fill)
        self._finalize_obligation(order_state=persisted)
        return persisted

    def resolve_order_state_for_control(self, client_order_id: str) -> OrderState | None:
        current = self.execution_repo.get_order_state(client_order_id)
        if current is not None:
            return current
        row = self._phase2_execution_order_row(client_order_id)
        if row is None:
            return None
        return self._hydrate_order_state_from_execution_row(row)

    async def _persist_order_state(
        self,
        *,
        order_state: OrderState,
        key: str,
        obligation=None,
        intent: OrderIntent | None = None,
        command: dict | None = None,
    ) -> OrderState:
        if self.execution_outbox_publisher is not None:
            if command is not None and self._submit_command_persisted_transactionally():
                persisted = await self.execution_outbox_publisher.persist_order_state_and_command(
                    order_state=order_state,
                    key=key,
                    obligation=obligation,
                    command_id=str(command["command_id"]),
                    command_type=str(command["command_type"]),
                    command_idempotency_key=str(command["idempotency_key"]),
                    command_payload=dict(command["payload"]),
                    command_created_at=command["created_at"],
                )
            else:
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
            self._shadow_write_order_state(order_state=persisted, intent=intent)
            self._sync_strategy_bundle_status(order_state=persisted)
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
        self._shadow_write_order_state(order_state=persisted, intent=intent)
        self._sync_strategy_bundle_status(order_state=persisted)
        return persisted

    def _sync_strategy_bundle_status(self, *, order_state: OrderState) -> None:
        if self.strategy_runtime_repo is None:
            return
        bundle_id = str(order_state.strategy_bundle_id or "").strip()
        if not bundle_id:
            return
        bundle = self.strategy_runtime_repo.get_execution_bundle(bundle_id)
        if bundle is None:
            return
        bundle_order_states = [
            state
            for state in self.execution_repo.order_states()
            if str(state.strategy_bundle_id or "").strip() == bundle_id
        ]
        derived_status = derive_strategy_bundle_status(
            order_states=bundle_order_states,
            previous_status=bundle.status,
        )
        if derived_status == bundle.status:
            return
        self.strategy_runtime_repo.save_execution_bundle(
            bundle.model_copy(
                update={
                    "status": derived_status,
                    "reason_codes": apply_strategy_bundle_status_reason_codes(
                        reason_codes=list(bundle.reason_codes),
                        status=derived_status,
                    ),
                }
            )
        )

    def _submit_command_persisted_transactionally(self) -> bool:
        if self.execution_outbox_publisher is None:
            return False
        return getattr(self.execution_outbox_publisher, "execution_command_repo", None) is not None

    async def _persist_fill(self, fill: FillEvent) -> None:
        obligation = None
        mirrored_obligation = None
        if self.obligation_service is not None and self.execution_outbox_publisher is not None:
            obligation = self.obligation_service.preview_obligation_for_fill(fill)
            mirrored_obligation = obligation
        if self.execution_outbox_publisher is not None:
            saved = await self.execution_outbox_publisher.persist_fill(fill=fill, obligation=obligation)
            if not saved:
                return
            self._shadow_write_fill(fill)
            self._shadow_sync_obligation(mirrored_obligation, reason="fill_settlement", related_fill=fill)
            return
        elif not self.execution_repo.save_fill(fill):
            return
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
        self._shadow_write_fill(fill)
        if self.obligation_service is not None:
            mirrored_obligation = self.obligation_service.consume_for_fill(fill)
        self._shadow_sync_obligation(mirrored_obligation, reason="fill_settlement", related_fill=fill)

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
        obligation = self.obligation_service.finalize_for_order_state(order_state)
        self._shadow_sync_obligation(obligation, reason="reservation_release", related_fill=None)

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
                execution_chain_id=intent.execution_chain_id,
                execution_attempt_id=(
                    intent.execution_attempt_id
                    or execution_attempt_id_from_components(
                        client_order_id=intent.idempotency_key,
                        execution_chain_id=intent.execution_chain_id,
                        intent_id=intent.intent_id,
                    )
                ),
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
                reduce_only=intent.reduce_only,
                close_only=intent.close_only,
                td_mode=intent.td_mode,
                position_mode=intent.position_mode,
                pos_side=intent.pos_side,
                reduce_only_reason=intent.reduce_only_reason,
                close_only_reason=intent.close_only_reason,
                instrument_family=intent.instrument_family,
                settle_currency=intent.settle_currency,
                product_type=intent.product_type,
                target_leverage=intent.target_leverage,
                margin_mode=intent.margin_mode,
                exposure_side=intent.exposure_side,
                execution_action=intent.execution_action,
                leg_action=intent.leg_action,
                position_intent=intent.position_intent,
                leg_intent_id=intent.leg_intent_id,
                strategy_family=intent.strategy_family,
                strategy_sleeve_id=intent.strategy_sleeve_id,
                allocation_id=intent.allocation_id,
                strategy_bundle_id=intent.strategy_bundle_id,
                strategy_leg_role=intent.strategy_leg_role,
                execution_error=f"transient_close_retry_cooldown_active:{state.execution_error or state.cancel_reason or 'transient_exchange_failure'}",
                submission_payload={},
            )
        return None

    @staticmethod
    def _apply_leg_risk_context(
        *,
        intent: OrderIntent,
        leg_intent: LegOrderIntent,
        risk_decision: object,
    ) -> tuple[OrderIntent, LegOrderIntent]:
        update_payload = {
            "required_initial_margin": getattr(risk_decision, "required_initial_margin", None),
            "projected_margin_usage": getattr(risk_decision, "projected_margin_usage", None),
            "projected_notional": getattr(risk_decision, "projected_notional", None),
            "risk_budget_multiplier": getattr(risk_decision, "risk_budget_multiplier", None),
            "risk_budget_state": dict(getattr(risk_decision, "risk_budget_state", {}) or {}),
            "execution_aggressiveness_multiplier": getattr(
                risk_decision,
                "execution_aggressiveness_multiplier",
                None,
            ),
            "execution_aggressiveness_state": dict(
                getattr(risk_decision, "execution_aggressiveness_state", {}) or {}
            ),
            "only_reduce_required": bool(getattr(risk_decision, "only_reduce_required", False)),
            "risk_limit_breached": bool(getattr(risk_decision, "risk_limit_breached", False)),
            "liquidation_buffer_remaining": getattr(
                risk_decision,
                "liquidation_buffer_remaining",
                None,
            ),
        }
        return (
            intent.model_copy(update=update_payload),
            leg_intent.model_copy(update=update_payload),
        )

    @staticmethod
    def _leg_risk_blocked_error(risk_decision: object) -> str:
        reasons = [
            str(item)
            for item in (getattr(risk_decision, "rejection_reasons", []) or [])
            if str(item).strip()
        ]
        if not reasons:
            reasons = ["leg_only_reduce_mode_active"]
        return f"leg_risk_blocked:{','.join(dict.fromkeys(reasons))}"

    def _leg_overlay_rollout_blockers(self, *, leg_intent: LegOrderIntent) -> list[str]:
        if (
            str(leg_intent.product_type or "") != "derivatives"
            or str(leg_intent.position_mode or "") != "long_short_mode"
        ):
            return []
        overlay_mode = overlay_mode_from_execution_mode(leg_intent.strategy_execution_mode)
        if overlay_mode is None:
            return []
        blockers: list[str] = []
        if not self.settings.strategy_hedge_overlay_enabled:
            blockers.append("strategy_hedge_overlay_disabled")
        if overlay_mode == "protective" and not self.settings.strategy_hedge_protective_enabled:
            blockers.append("strategy_hedge_protective_disabled")
        if overlay_mode == "opportunistic" and not self.settings.strategy_hedge_opportunistic_enabled:
            blockers.append("strategy_hedge_opportunistic_disabled")
        if overlay_mode == "independent" and not self.settings.strategy_hedge_independent_enabled:
            blockers.append("strategy_hedge_independent_disabled")
        if blockers:
            return list(dict.fromkeys(blockers))
        rollout = overlay_rollout_status(self.settings, mode=overlay_mode)
        return [str(item) for item in (rollout.get("blocking_reasons") or []) if str(item).strip()]

    @staticmethod
    def _leg_overlay_rollout_blocked_error(reasons: list[str]) -> str:
        cleaned = [str(item) for item in reasons if str(item).strip()]
        if not cleaned:
            cleaned = ["overlay_rollout_blocked"]
        return f"leg_overlay_rollout_blocked:{','.join(dict.fromkeys(cleaned))}"

    def _apply_leg_submit_guards(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        leg_intent: LegOrderIntent | None = None,
    ) -> tuple[OrderIntent, LegOrderIntent | None, OrderState | None]:
        normalized_leg_intent = leg_intent or leg_intent_from_order_intent(intent)
        if normalized_leg_intent is None:
            return intent, None, None
        guarded_intent = intent
        guarded_leg_intent = normalized_leg_intent
        if self.leg_risk_evaluator is not None:
            risk_decision = self.leg_risk_evaluator(guarded_leg_intent)
            guarded_intent, guarded_leg_intent = self._apply_leg_risk_context(
                intent=guarded_intent,
                leg_intent=guarded_leg_intent,
                risk_decision=risk_decision,
            )
            if not bool(getattr(risk_decision, "approved", False)):
                return (
                    guarded_intent,
                    guarded_leg_intent,
                    self._blocked_order_state_from_intent(
                        intent=guarded_intent,
                        client_order_id=client_order_id,
                        submission_mode="leg_risk_blocked",
                        execution_error=self._leg_risk_blocked_error(risk_decision),
                    ),
                )
        rollout_blockers = self._leg_overlay_rollout_blockers(leg_intent=guarded_leg_intent)
        if rollout_blockers:
            return (
                guarded_intent,
                guarded_leg_intent,
                self._blocked_order_state_from_intent(
                    intent=guarded_intent,
                    client_order_id=client_order_id,
                    submission_mode="leg_overlay_rollout_blocked",
                    execution_error=self._leg_overlay_rollout_blocked_error(rollout_blockers),
                ),
            )
        return guarded_intent, guarded_leg_intent, None

    def _blocked_order_state_from_intent(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        submission_mode: str,
        execution_error: str,
    ) -> OrderState:
        return OrderState(
            decision_id=intent.decision_id,
            execution_chain_id=intent.execution_chain_id,
            execution_attempt_id=intent.execution_attempt_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            client_order_id=client_order_id,
            venue="OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER",
            exchange_order_id=None,
            status="BLOCKED",
            submission_mode=submission_mode,
            submitted_ts=None,
            last_update_ts=utc_now(),
            requested_qty=intent.quantity,
            filled_qty=Decimal("0"),
            remaining_qty=intent.quantity,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=intent.reduce_only,
            close_only=intent.close_only,
            td_mode=intent.td_mode,
            position_mode=intent.position_mode,
            pos_side=intent.pos_side,
            reduce_only_reason=intent.reduce_only_reason,
            close_only_reason=intent.close_only_reason,
            instrument_family=intent.instrument_family,
            settle_currency=intent.settle_currency,
            product_type=intent.product_type,
            target_leverage=intent.target_leverage,
            margin_mode=intent.margin_mode,
            exposure_side=intent.exposure_side,
            execution_action=intent.execution_action,
            leg_action=intent.leg_action,
            position_intent=intent.position_intent,
            leg_intent_id=intent.leg_intent_id,
            strategy_family=intent.strategy_family,
            strategy_sleeve_id=intent.strategy_sleeve_id,
            allocation_id=intent.allocation_id,
            strategy_bundle_id=intent.strategy_bundle_id,
            strategy_leg_role=intent.strategy_leg_role,
            strategy_pair_id=intent.strategy_pair_id,
            strategy_opportunity_kind=intent.strategy_opportunity_kind,
            strategy_execution_mode=intent.strategy_execution_mode,
            strategy_state_phase=intent.strategy_state_phase,
            execution_error=execution_error,
            submission_payload={},
        )

    async def _cancel_pending_submit_before_exchange_ack(self, order_state: OrderState) -> OrderState | None:
        if self.persistent_order_service is None:
            return None
        if order_state.exchange_order_id is not None:
            return None
        if order_state.status not in {"CREATED", "SUBMITTING", "CANCEL_PENDING"}:
            return None
        submit_command = self.persistent_order_service.execution_command_repo.get_by_idempotency_key(
            f"submit:{order_state.intent_id}"
        )
        command_state = str(submit_command.get("state") or "").upper() if submit_command is not None else None
        if submit_command is not None and command_state not in {"PENDING", "ACKED", "FAILED"}:
            return None
        now = utc_now()
        if submit_command is not None and command_state == "PENDING":
            self.persistent_order_service.execution_command_repo.mark_abandoned(
                str(submit_command["command_id"]),
                reason="operator_cancel_before_submit",
                updated_at=now,
            )
        canceled = order_state.model_copy(
            update={
                "status": "CANCELED",
                "canceled_ts": now,
                "last_update_ts": now,
                "cancel_reason": order_state.cancel_reason or "operator_cancel_before_submit",
                "execution_error": None,
            }
        )
        persisted = await self._persist_order_state(
            order_state=canceled,
            key=order_state.symbol,
            obligation=self._terminal_outbox_obligation(order_state=canceled, fills=[]),
        )
        self._finalize_obligation(order_state=persisted)
        return persisted

    @staticmethod
    def _apply_execution_attempt_id(
        *,
        intent: OrderIntent,
        client_order_id: str,
        leg_intent: LegOrderIntent | None = None,
    ) -> tuple[OrderIntent, LegOrderIntent | None]:
        attempt_id = execution_attempt_id_from_components(
            execution_attempt_id=intent.execution_attempt_id,
            client_order_id=client_order_id,
            execution_chain_id=intent.execution_chain_id,
            intent_id=intent.intent_id,
        )
        updated_intent = (
            intent
            if attempt_id == intent.execution_attempt_id
            else intent.model_copy(update={"execution_attempt_id": attempt_id})
        )
        if leg_intent is None:
            return updated_intent, None
        updated_leg_intent = (
            leg_intent
            if attempt_id == leg_intent.execution_attempt_id
            else leg_intent.model_copy(update={"execution_attempt_id": attempt_id})
        )
        return updated_intent, updated_leg_intent

    def _phase2_execution_order_row(self, client_order_id: str) -> dict | None:
        execution_order_repo = getattr(self.persistent_order_service, "execution_order_repo", None)
        if execution_order_repo is not None:
            row = execution_order_repo.get_order_by_client_order_id(client_order_id)
            if row is not None:
                return row
        execution_shadow_repo = getattr(self.shadow_execution_service, "execution_order_repo", None)
        if execution_shadow_repo is not None:
            return execution_shadow_repo.get_order_by_client_order_id(client_order_id)
        return None

    def _hydrate_order_state_from_execution_row(self, row: dict) -> OrderState:
        def _aware(value):
            if value is None:
                return None
            if getattr(value, "tzinfo", None) is None:
                return value.replace(tzinfo=timezone.utc)
            return value

        raw_payload = dict(row.get("raw_payload") or {})
        order_payload = raw_payload.get("order_state")
        if isinstance(order_payload, dict):
            payload = dict(order_payload)
            submission_payload = payload.get("submission_payload")
            if not isinstance(submission_payload, dict):
                submission_payload = {}
            payload.setdefault("decision_id", row.get("decision_id"))
            payload.setdefault(
                "execution_chain_id",
                raw_payload.get("execution_chain_id") or submission_payload.get("executionChainId"),
            )
            payload.setdefault(
                "execution_attempt_id",
                raw_payload.get("execution_attempt_id") or submission_payload.get("executionAttemptId"),
            )
            payload.setdefault("intent_id", row.get("intent_id"))
            payload.setdefault("symbol", row.get("symbol"))
            payload.setdefault("client_order_id", row.get("client_order_id") or row.get("order_id"))
            payload.setdefault("exchange_order_id", row.get("venue_order_id"))
            payload.setdefault("status", row.get("state"))
            payload.setdefault("requested_qty", row.get("requested_qty"))
            payload.setdefault("remaining_qty", row.get("requested_qty"))
            payload.setdefault("product_type", row.get("product_type"))
            payload.setdefault("margin_mode", row.get("margin_mode"))
            payload.setdefault("target_leverage", row.get("raw_payload", {}).get("target_leverage", 1.0))
            payload.setdefault("reduce_only", row.get("reduce_only", False))
            payload.setdefault("close_only", row.get("close_only", False))
            payload.setdefault(
                "td_mode",
                row.get("td_mode") or submission_payload.get("tdMode") or row.get("margin_mode"),
            )
            payload.setdefault("position_mode", row.get("position_mode"))
            payload.setdefault("pos_side", row.get("pos_side") or submission_payload.get("posSide"))
            payload.setdefault("reduce_only_reason", row.get("reduce_only_reason"))
            payload.setdefault("close_only_reason", row.get("close_only_reason"))
            payload.setdefault("instrument_family", row.get("instrument_family"))
            payload.setdefault("settle_currency", row.get("settle_currency"))
            payload.setdefault("position_intent", row.get("position_intent") or "open_long")
            payload.setdefault("execution_action", row.get("execution_action"))
            payload.setdefault("strategy_family", raw_payload.get("strategy_family"))
            payload.setdefault("strategy_sleeve_id", raw_payload.get("strategy_sleeve_id") or row.get("strategy_sleeve_id"))
            payload.setdefault("allocation_id", raw_payload.get("allocation_id") or row.get("allocation_id"))
            payload.setdefault("strategy_bundle_id", raw_payload.get("strategy_bundle_id"))
            payload.setdefault("strategy_leg_role", raw_payload.get("strategy_leg_role"))
            payload.setdefault("submission_payload", submission_payload)
            if payload.get("pos_side") in {"", None}:
                payload["pos_side"] = row.get("pos_side") or submission_payload.get("posSide") or None
            return OrderState.model_validate(payload)
        submission_mode = str(raw_payload.get("source_system") or "phase2_execution_order_repo")
        venue = str(raw_payload.get("venue") or ("OKX" if self.adapter.readiness().get("backend") == "okx" else "PAPER"))
        created_at = _aware(row.get("created_at")) or utc_now()
        updated_at = _aware(row.get("updated_at")) or created_at
        requested_qty = row.get("requested_qty")
        return OrderState(
            decision_id=str(row.get("decision_id") or ""),
            execution_chain_id=raw_payload.get("execution_chain_id"),
            execution_attempt_id=(
                raw_payload.get("execution_attempt_id")
                or execution_attempt_id_from_components(
                    client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
                    execution_chain_id=raw_payload.get("execution_chain_id"),
                    intent_id=str(row.get("intent_id") or ""),
                )
            ),
            intent_id=str(row.get("intent_id") or ""),
            symbol=str(row.get("symbol") or self.settings.default_symbol),
            client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
            venue=venue,
            exchange_order_id=row.get("venue_order_id"),
            status=str(row.get("state") or "CREATED"),
            submission_mode=submission_mode,
            submitted_ts=created_at if str(row.get("state") or "CREATED") != "CREATED" else None,
            last_update_ts=updated_at,
            last_exchange_update_ts=_aware(row.get("last_exchange_ts")),
            requested_qty=requested_qty,
            filled_qty=Decimal("0"),
            remaining_qty=requested_qty,
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=bool(raw_payload.get("reduce_only", False)),
            close_only=bool(raw_payload.get("close_only", False)),
            td_mode=str(
                raw_payload.get("td_mode")
                or raw_payload.get("submission_payload", {}).get("tdMode")
                or row.get("margin_mode")
                or "cash"
            ),
            position_mode=raw_payload.get("position_mode"),
            pos_side=raw_payload.get("pos_side") or raw_payload.get("submission_payload", {}).get("posSide") or None,
            reduce_only_reason=raw_payload.get("reduce_only_reason"),
            close_only_reason=raw_payload.get("close_only_reason"),
            instrument_family=raw_payload.get("instrument_family"),
            settle_currency=raw_payload.get("settle_currency"),
            product_type=row.get("product_type") or "spot",
            target_leverage=float(raw_payload.get("target_leverage") or 1.0),
            margin_mode=row.get("margin_mode") or "cash",
            exposure_side=str(raw_payload.get("exposure_side") or "flat"),
            execution_action=row.get("execution_action"),
            position_intent=str(row.get("position_intent") or "open_long"),
            strategy_family=raw_payload.get("strategy_family"),
            strategy_sleeve_id=raw_payload.get("strategy_sleeve_id") or row.get("strategy_sleeve_id"),
            allocation_id=raw_payload.get("allocation_id") or row.get("allocation_id"),
            strategy_bundle_id=raw_payload.get("strategy_bundle_id"),
            strategy_leg_role=raw_payload.get("strategy_leg_role"),
            submission_payload={},
        )

    def _shadow_write_order_state(self, *, order_state: OrderState, intent: OrderIntent | None = None) -> None:
        if self.shadow_execution_service is None:
            return
        try:
            self.shadow_execution_service.shadow_order_state(order_state=order_state, intent=intent)
        except Exception as exc:
            log_event(
                self.logger,
                "shadow_order_state_write_failed",
                level="warning",
                **correlation_fields(
                    decision_id=order_state.decision_id,
                    intent_id=order_state.intent_id,
                    order_id=order_state.client_order_id,
                    status=order_state.status,
                    error=str(exc),
                ),
            )

    def _shadow_write_fill(self, fill: FillEvent) -> None:
        if self.shadow_execution_service is None:
            return
        try:
            self.shadow_execution_service.shadow_fill(fill)
        except Exception as exc:
            log_event(
                self.logger,
                "shadow_fill_write_failed",
                level="warning",
                **correlation_fields(
                    decision_id=fill.decision_id,
                    intent_id=fill.intent_id,
                    order_id=fill.client_order_id,
                    fill_id=fill.fill_id,
                    error=str(exc),
                ),
            )

    @staticmethod
    def _build_legacy_shadow_execution_service(
        *,
        shadow_execution_order_repo: ExecutionOrderRepository | None,
        shadow_execution_order_history_repo: ExecutionOrderHistoryRepository | None,
        shadow_execution_fill_repo: ExecutionFillRepositoryV2 | None,
    ) -> Phase1ExecutionShadowService | None:
        if shadow_execution_order_repo is None:
            return None
        return Phase1ExecutionShadowService(
            execution_order_repo=shadow_execution_order_repo,
            execution_order_history_repo=shadow_execution_order_history_repo,
            execution_fill_repo=shadow_execution_fill_repo,
        )

    def _shadow_sync_obligation(
        self,
        obligation: OrderObligation | None,
        *,
        reason: str,
        related_fill: FillEvent | None,
    ) -> None:
        if obligation is None or self.shadow_ledger_mirror_service is None:
            return
        try:
            self.shadow_ledger_mirror_service.sync_obligation(
                obligation,
                reason=reason,
                related_fill=related_fill,
            )
        except Exception as exc:
            log_event(
                self.logger,
                "shadow_obligation_write_failed",
                level="warning",
                **correlation_fields(
                    decision_id=obligation.decision_id,
                    intent_id=obligation.intent_id,
                    order_id=obligation.client_order_id,
                    reason=reason,
                    error=str(exc),
                ),
            )
