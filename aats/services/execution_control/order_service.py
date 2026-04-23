from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.schemas.common import new_id, utc_now
from aats.schemas.execution import OrderIntent, OrderState, side_from_position_intent
from aats.storage.execution_command_repo import ExecutionCommandRepository
from aats.storage.execution_order_repo import ExecutionOrderHistoryRepository, ExecutionOrderRepository


@dataclass(slots=True, frozen=True)
class EnqueuedExecutionCommand:
    command_id: str
    order_id: str
    command_type: str
    idempotency_key: str
    payload: dict[str, Any]


class ExecutionOrderService:
    def __init__(
        self,
        *,
        execution_command_repo: ExecutionCommandRepository,
        execution_order_repo: ExecutionOrderRepository | None = None,
        execution_order_history_repo: ExecutionOrderHistoryRepository | None = None,
    ) -> None:
        self.execution_command_repo = execution_command_repo
        self.execution_order_repo = execution_order_repo
        self.execution_order_history_repo = execution_order_history_repo
        self.logger = get_logger("aats.execution_control.order_service")

    def enqueue_submit(self, *, intent: OrderIntent, client_order_id: str) -> EnqueuedExecutionCommand:
        self._ensure_order_row(intent=intent, client_order_id=client_order_id, initial_state="CREATED")
        payload = self.submit_command_payload(intent=intent, client_order_id=client_order_id)
        command = self._enqueue_command(
            order_id=client_order_id,
            command_type="submit",
            idempotency_key=self.submit_command_idempotency_key(client_order_id),
            payload=payload,
        )
        log_event(
            self.logger,
            "execution_submit_command_enqueued",
            **correlation_fields(
                decision_id=intent.decision_id,
                intent_id=intent.intent_id,
                order_id=client_order_id,
                symbol=intent.symbol,
                command_id=command.command_id,
            ),
        )
        return command

    def enqueue_cancel(self, *, order_state: OrderState, reason: str | None = None) -> EnqueuedExecutionCommand:
        self._ensure_order_row(
            intent=self._intent_from_order_state(order_state),
            client_order_id=order_state.client_order_id,
            initial_state=order_state.status,
            order_state=order_state,
        )
        payload = self.cancel_command_payload(order_state=order_state, reason=reason)
        command = self._enqueue_command(
            order_id=order_state.client_order_id,
            command_type="cancel",
            idempotency_key=self.cancel_command_idempotency_key(order_state.client_order_id),
            payload=payload,
        )
        log_event(
            self.logger,
            "execution_cancel_command_enqueued",
            **correlation_fields(
                decision_id=order_state.decision_id,
                intent_id=order_state.intent_id,
                order_id=order_state.client_order_id,
                symbol=order_state.symbol,
                command_id=command.command_id,
            ),
        )
        return command

    @staticmethod
    def submit_command_idempotency_key(stable_order_key: str) -> str:
        return f"submit:{stable_order_key}"

    @staticmethod
    def legacy_submit_command_idempotency_key(intent_id: str) -> str:
        return f"submit:{intent_id}"

    @classmethod
    def submit_command_lookup_keys(
        cls,
        *,
        client_order_id: str | None,
        intent_id: str | None,
    ) -> tuple[str, ...]:
        keys: list[str] = []
        normalized_client_order_id = str(client_order_id or "").strip()
        normalized_intent_id = str(intent_id or "").strip()
        if normalized_client_order_id:
            keys.append(cls.submit_command_idempotency_key(normalized_client_order_id))
        if normalized_intent_id:
            legacy_key = cls.legacy_submit_command_idempotency_key(normalized_intent_id)
            if legacy_key not in keys:
                keys.append(legacy_key)
        return tuple(keys)

    @staticmethod
    def cancel_command_idempotency_key(client_order_id: str) -> str:
        return f"cancel:{client_order_id}"

    @staticmethod
    def submit_command_payload(*, intent: OrderIntent, client_order_id: str) -> dict[str, Any]:
        return {
            "intent": intent.model_dump(mode="python"),
            "client_order_id": client_order_id,
            "symbol": intent.symbol,
        }

    @staticmethod
    def cancel_command_payload(*, order_state: OrderState, reason: str | None) -> dict[str, Any]:
        return {
            "client_order_id": order_state.client_order_id,
            "symbol": order_state.symbol,
            "reason": reason,
        }

    def _enqueue_command(
        self,
        *,
        order_id: str,
        command_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> EnqueuedExecutionCommand:
        existing = self.execution_command_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return EnqueuedExecutionCommand(
                command_id=str(existing["command_id"]),
                order_id=str(existing["order_id"]),
                command_type=str(existing["command_type"]),
                idempotency_key=str(existing["idempotency_key"]),
                payload=dict(existing.get("command_payload") or {}),
            )
        command = EnqueuedExecutionCommand(
            command_id=new_id("cmd"),
            order_id=order_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        now = utc_now()
        self.execution_command_repo.enqueue_command(
            command_id=command.command_id,
            order_id=command.order_id,
            command_type=command.command_type,
            idempotency_key=command.idempotency_key,
            payload=command.payload,
            created_at=now,
        )
        return command

    def _ensure_order_row(
        self,
        *,
        intent: OrderIntent,
        client_order_id: str,
        initial_state: str,
        order_state: OrderState | None = None,
    ) -> None:
        if self.execution_order_repo is None:
            return
        existing = self.execution_order_repo.get_order_by_client_order_id(client_order_id)
        if existing is not None:
            return
        created_at = order_state.created_at if order_state is not None else intent.created_at
        self.execution_order_repo.create_order(
            order_id=client_order_id,
            intent=intent,
            initial_state=initial_state,
            created_at=created_at,
            raw_payload={
                "client_order_id": client_order_id,
                "source_system": "phase2_execution_command_flow",
                # Path C observability fix (2026-04-19): 顶层落库 execution_style 供事后对账
                # 参见 docs/review/cost_audit_live_reconciliation_2026_04_19.md §7.2
                "execution_style": intent.execution_style,
                # Execution truth snapshot refs：顶层落库 decision 层四类 snapshot refs
                # 作为盘口快照事后归因的稳定锚点。沿用 execution_style 的顶层锚点约定
                # 便于 SQL 直接查询，无需解压 nested intent dump。
                "market_snapshot_ref": intent.market_snapshot_ref,
                "feature_snapshot_ref": intent.feature_snapshot_ref,
                "portfolio_snapshot_ref": intent.portfolio_snapshot_ref,
                "health_snapshot_ref": intent.health_snapshot_ref,
                "order_state": order_state.model_dump(mode="python") if order_state is not None else None,
            },
        )
        if self.execution_order_history_repo is not None:
            self.execution_order_history_repo.append_transition(
                order_id=client_order_id,
                from_state=None,
                to_state=initial_state,
                reason_code="phase2_order_seed",
                source="execution_order_service",
                source_message_id=intent.intent_id,
                payload=(
                    order_state.model_dump(mode="python")
                    if order_state is not None
                    else intent.model_dump(mode="python")
                ),
                created_at=created_at,
            )

    @staticmethod
    def _intent_from_order_state(order_state: OrderState) -> OrderIntent:
        side = side_from_position_intent(order_state.position_intent) or "buy"
        return OrderIntent(
            intent_id=order_state.intent_id,
            execution_chain_id=order_state.execution_chain_id,
            execution_attempt_id=order_state.execution_attempt_id,
            leg_intent_id=order_state.leg_intent_id,
            decision_id=order_state.decision_id,
            symbol=order_state.symbol,
            side=side,
            quantity=order_state.requested_qty,
            execution_style=order_state.submission_mode or "phase2",
            order_type="market",
            urgency="medium",
            time_in_force="IOC",
            reduce_only=order_state.reduce_only,
            close_only=order_state.close_only,
            td_mode=order_state.td_mode,
            position_mode=order_state.position_mode,
            pos_side=order_state.pos_side,
            reduce_only_reason=order_state.reduce_only_reason,
            close_only_reason=order_state.close_only_reason,
            instrument_family=order_state.instrument_family,
            settle_currency=order_state.settle_currency,
            idempotency_key=order_state.client_order_id,
            product_type=order_state.product_type,
            target_leverage=order_state.target_leverage,
            margin_mode=order_state.margin_mode,
            exposure_side=order_state.exposure_side,
            execution_action=order_state.execution_action,
            leg_action=order_state.leg_action,
            position_intent=order_state.position_intent,
            strategy_family=order_state.strategy_family,
            strategy_sleeve_id=order_state.strategy_sleeve_id,
            allocation_id=order_state.allocation_id,
            strategy_bundle_id=order_state.strategy_bundle_id,
            strategy_leg_role=order_state.strategy_leg_role,
            strategy_pair_id=order_state.strategy_pair_id,
            strategy_opportunity_kind=order_state.strategy_opportunity_kind,
            strategy_execution_mode=order_state.strategy_execution_mode,
            strategy_state_phase=order_state.strategy_state_phase,
            market_snapshot_ref=order_state.market_snapshot_ref,
            feature_snapshot_ref=order_state.feature_snapshot_ref,
            portfolio_snapshot_ref=order_state.portfolio_snapshot_ref,
            health_snapshot_ref=order_state.health_snapshot_ref,
        )
