from __future__ import annotations

from threading import Lock

from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.storage.execution_fill_repo_v2 import ExecutionFillRepositoryV2
from aats.storage.execution_order_repo import ExecutionOrderHistoryRepository, ExecutionOrderRepository


class Phase1ExecutionShadowService:
    def __init__(
        self,
        *,
        execution_order_repo: ExecutionOrderRepository,
        execution_order_history_repo: ExecutionOrderHistoryRepository | None = None,
        execution_fill_repo: ExecutionFillRepositoryV2 | None = None,
    ) -> None:
        self.execution_order_repo = execution_order_repo
        self.execution_order_history_repo = execution_order_history_repo
        self.execution_fill_repo = execution_fill_repo
        self._lock = Lock()
        self._order_attempt_count = 0
        self._order_success_count = 0
        self._order_failure_count = 0
        self._fill_attempt_count = 0
        self._fill_success_count = 0
        self._fill_failure_count = 0
        self._last_order_sync_ts = None
        self._last_fill_sync_ts = None
        self._last_failure_ts = None
        self._last_error = None
        self._last_outcome = "idle"
        self._last_synced_order_id = None
        self._last_synced_order_state = None
        self._last_synced_fill_id = None

    def shadow_order_state(self, *, order_state: OrderState, intent: OrderIntent | None = None) -> None:
        with self._lock:
            self._order_attempt_count += 1
        try:
            existing = self.execution_order_repo.get_order_by_client_order_id(order_state.client_order_id)
            if existing is None:
                seed_intent = intent or self.intent_from_order_state(order_state)
                self.execution_order_repo.create_order(
                    order_id=order_state.client_order_id,
                    intent=seed_intent,
                    initial_state=order_state.status,
                    created_at=order_state.created_at,
                    raw_payload={
                        "client_order_id": order_state.client_order_id,
                        "venue_order_id": order_state.exchange_order_id,
                        "source_system": "shadow_order_manager",
                        "order_state": order_state.model_dump(mode="python"),
                    },
                )
                if self.execution_order_history_repo is not None:
                    self.execution_order_history_repo.append_transition(
                        order_id=order_state.client_order_id,
                        from_state=None,
                        to_state=order_state.status,
                        reason_code="shadow_initial_persist",
                        source="order_manager",
                        source_message_id=order_state.intent_id,
                        payload=order_state.model_dump(mode="python"),
                        created_at=order_state.last_update_ts or order_state.created_at,
                    )
                self._record_order_success(order_state)
                return

            needs_update = (
                existing["state"] != order_state.status
                or existing.get("venue_order_id") != order_state.exchange_order_id
                or existing.get("last_exchange_ts") != order_state.last_exchange_update_ts
            )
            previous_state = existing["state"]
            if needs_update:
                self.execution_order_repo.update_order_state(
                    order_id=existing["order_id"],
                    expected_state_version=int(existing["state_version"]),
                    next_state=order_state.status,
                    venue_order_id=order_state.exchange_order_id,
                    last_exchange_ts=order_state.last_exchange_update_ts,
                    updated_at=order_state.last_update_ts or utc_now(),
                    raw_payload=order_state.model_dump(mode="python"),
                )
            if self.execution_order_history_repo is not None and previous_state != order_state.status:
                self.execution_order_history_repo.append_transition(
                    order_id=existing["order_id"],
                    from_state=previous_state,
                    to_state=order_state.status,
                    reason_code="shadow_state_persist",
                    source="order_manager",
                    source_message_id=order_state.intent_id,
                    payload=order_state.model_dump(mode="python"),
                    created_at=order_state.last_update_ts or order_state.created_at,
                )
            self._record_order_success(order_state)
        except Exception as exc:
            self._record_failure(kind="order", subject_id=order_state.client_order_id, error=str(exc))
            raise

    def shadow_fill(self, fill: FillEvent) -> None:
        if self.execution_fill_repo is None:
            return
        with self._lock:
            self._fill_attempt_count += 1
        try:
            order_row = self.execution_order_repo.get_order_by_client_order_id(fill.client_order_id)
            if order_row is None:
                synthetic_intent = self.intent_from_fill(fill)
                self.execution_order_repo.create_order(
                    order_id=fill.client_order_id,
                    intent=synthetic_intent,
                    initial_state=fill.order_status_after_fill or "FILLED",
                    created_at=fill.created_at,
                    raw_payload={
                        "client_order_id": fill.client_order_id,
                        "venue_order_id": fill.exchange_order_id,
                        "source_system": "shadow_fill_backfill",
                        "fill_event": fill.model_dump(mode="python"),
                    },
                )
                order_row = self.execution_order_repo.get_order_by_client_order_id(fill.client_order_id)
            order_id = fill.client_order_id if order_row is None else str(order_row["order_id"])
            self.execution_fill_repo.save_fill(
                fill=fill,
                order_id=order_id,
                source=fill.venue.lower(),
                raw_payload={
                    "venue_fill_id": fill.fill_id,
                    "fill_event": fill.model_dump(mode="python"),
                },
            )
            self._record_fill_success(fill)
        except Exception as exc:
            self._record_failure(kind="fill", subject_id=fill.fill_id, error=str(exc))
            raise

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            status = "idle"
            if self._last_outcome == "failure":
                status = "degraded"
            elif self._order_success_count > 0 or self._fill_success_count > 0:
                status = "healthy"
            return {
                "configured": True,
                "status": status,
                "last_outcome": self._last_outcome,
                "order_attempt_count": self._order_attempt_count,
                "order_success_count": self._order_success_count,
                "order_failure_count": self._order_failure_count,
                "fill_attempt_count": self._fill_attempt_count,
                "fill_success_count": self._fill_success_count,
                "fill_failure_count": self._fill_failure_count,
                "last_order_sync_ts": self._last_order_sync_ts,
                "last_fill_sync_ts": self._last_fill_sync_ts,
                "last_failure_ts": self._last_failure_ts,
                "last_error": self._last_error,
                "last_synced_order_id": self._last_synced_order_id,
                "last_synced_order_state": self._last_synced_order_state,
                "last_synced_fill_id": self._last_synced_fill_id,
            }

    def _record_order_success(self, order_state: OrderState) -> None:
        with self._lock:
            self._order_success_count += 1
            self._last_order_sync_ts = order_state.last_update_ts or order_state.created_at
            self._last_synced_order_id = order_state.client_order_id
            self._last_synced_order_state = order_state.status
            self._last_outcome = "success"

    def _record_fill_success(self, fill: FillEvent) -> None:
        with self._lock:
            self._fill_success_count += 1
            self._last_fill_sync_ts = fill.ingestion_timestamp
            self._last_synced_fill_id = fill.fill_id
            self._last_outcome = "success"

    def _record_failure(self, *, kind: str, subject_id: str, error: str) -> None:
        with self._lock:
            self._last_failure_ts = utc_now()
            self._last_error = {
                "kind": kind,
                "subject_id": subject_id,
                "message": error,
            }
            self._last_outcome = "failure"
            if kind == "fill":
                self._fill_failure_count += 1
            else:
                self._order_failure_count += 1

    @staticmethod
    def intent_from_order_state(order_state: OrderState) -> OrderIntent:
        return OrderIntent(
            intent_id=order_state.intent_id,
            leg_intent_id=order_state.leg_intent_id,
            decision_id=order_state.decision_id,
            symbol=order_state.symbol,
            side=(
                "buy"
                if order_state.position_intent
                not in {"open_short", "scale_in_short", "reduce_short", "close_short", "reverse_to_short"}
                else "sell"
            ),
            quantity=order_state.requested_qty,
            execution_style=order_state.submission_mode or "shadow",
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
        )

    @staticmethod
    def intent_from_fill(fill: FillEvent) -> OrderIntent:
        return OrderIntent(
            intent_id=fill.intent_id,
            leg_intent_id=fill.leg_intent_id,
            decision_id=fill.decision_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.fill_qty,
            execution_style="shadow_fill_ingestion",
            order_type="market",
            urgency="high",
            time_in_force="IOC",
            reduce_only=fill.reduce_only,
            close_only=fill.close_only,
            td_mode=fill.td_mode,
            position_mode=fill.position_mode,
            pos_side=fill.pos_side,
            reduce_only_reason=fill.reduce_only_reason,
            close_only_reason=fill.close_only_reason,
            instrument_family=fill.instrument_family,
            settle_currency=fill.settle_currency,
            idempotency_key=fill.client_order_id,
            product_type=fill.product_type,
            target_leverage=fill.target_leverage,
            margin_mode=fill.margin_mode,
            exposure_side=fill.exposure_side,
            execution_action=fill.execution_action,
            leg_action=fill.leg_action,
            position_intent=fill.position_intent,
        )
