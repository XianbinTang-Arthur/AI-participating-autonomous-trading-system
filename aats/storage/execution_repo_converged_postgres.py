from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.services.execution_engine.state_machine import OrderStateMachine
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.base import ExecutionRepository
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)


_TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "FAILED", "BLOCKED", "DRY_RUN", "EXPIRED"}


class ConvergedPostgresExecutionRepository(ExecutionRepository):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        execution_order_repo: PostgresExecutionOrderRepository,
        execution_order_history_repo: PostgresExecutionOrderHistoryRepository | None,
        execution_fill_repo: PostgresExecutionFillRepositoryV2,
    ) -> None:
        self.session_factory = session_factory
        self.execution_order_repo = execution_order_repo
        self.execution_order_history_repo = execution_order_history_repo
        self.execution_fill_repo = execution_fill_repo
        self.state_machine = OrderStateMachine()

    def save_order_state(self, state: OrderState) -> OrderState:
        with self.session_factory() as session:
            persisted, _previous = self.save_order_state_in_session(session, state)
            session.commit()
        return persisted

    def save_order_state_in_session(self, session: Session, state: OrderState) -> tuple[OrderState, OrderState | None]:
        existing = self.execution_order_repo.get_order_by_client_order_id_in_session(
            session,
            state.client_order_id,
            for_update=True,
        )
        previous = self._hydrate_order_state(existing) if existing is not None else None
        validation = self.state_machine.validate_transition(
            current_status=None if previous is None else previous.status,
            next_status=state.status,
        )
        if not validation.accepted and validation.reason == "invalid_transition":
            raise ValueError(
                f"invalid_order_state_transition current={None if previous is None else previous.status} next={state.status}"
            )
        merged = self.state_machine.merge(current=previous, incoming=state)
        raw_payload = {
            "source_system": merged.submission_mode or "converged_execution_repo",
            "order_state": dump_payload_exact(merged),
        }
        if existing is None:
            self.execution_order_repo.create_order_in_session(
                session,
                order_id=merged.client_order_id,
                intent=self._intent_from_order_state(merged),
                initial_state=merged.status,
                created_at=merged.created_at,
                raw_payload=raw_payload,
            )
            if self.execution_order_history_repo is not None:
                self.execution_order_history_repo.append_transition_in_session(
                    session,
                    order_id=merged.client_order_id,
                    from_state=None,
                    to_state=merged.status,
                    reason_code="converged_execution_seed",
                    source="execution_repo",
                    source_message_id=merged.intent_id,
                    payload=dump_payload_exact(merged),
                    created_at=merged.last_update_ts or merged.created_at,
                )
        else:
            self.execution_order_repo.update_order_state_in_session(
                session,
                order_id=str(existing["order_id"]),
                expected_state_version=int(existing["state_version"]),
                next_state=merged.status,
                venue_order_id=merged.exchange_order_id,
                last_exchange_ts=merged.last_exchange_update_ts or merged.last_update_ts,
                updated_at=merged.last_update_ts or merged.created_at,
                raw_payload=raw_payload,
            )
            if self.execution_order_history_repo is not None and previous is not None and previous.status != merged.status:
                self.execution_order_history_repo.append_transition_in_session(
                    session,
                    order_id=str(existing["order_id"]),
                    from_state=previous.status,
                    to_state=merged.status,
                    reason_code="converged_execution_update",
                    source="execution_repo",
                    source_message_id=merged.intent_id,
                    payload=dump_payload_exact(merged),
                    created_at=merged.last_update_ts or merged.created_at,
                )
        return merged, previous

    def has_intent(self, intent_id: str) -> bool:
        return self.execution_order_repo.get_order_by_intent(intent_id) is not None

    def save_fill(self, fill: FillEvent) -> bool:
        with self.session_factory() as session:
            saved = self.save_fill_in_session(session, fill)
            session.commit()
            return saved

    def save_fill_in_session(self, session: Session, fill: FillEvent) -> bool:
        existing = self.execution_order_repo.get_order_by_client_order_id_in_session(
            session,
            fill.client_order_id,
            for_update=True,
        )
        if existing is None:
            synthetic_intent = Phase1ExecutionShadowService.intent_from_fill(fill)
            self.execution_order_repo.create_order_in_session(
                session,
                order_id=fill.client_order_id,
                intent=synthetic_intent,
                initial_state=fill.order_status_after_fill or "FILLED",
                created_at=fill.created_at,
                raw_payload={
                    "source_system": "converged_fill_backfill",
                    "client_order_id": fill.client_order_id,
                    "venue_order_id": fill.exchange_order_id,
                    "fill_event": dump_payload_exact(fill),
                },
            )
            order_id = fill.client_order_id
        else:
            order_id = str(existing["order_id"])
        return self.execution_fill_repo.save_fill_in_session(
            session,
            fill=fill,
            order_id=order_id,
            source=fill.venue.lower(),
            raw_payload={
                "venue_fill_id": fill.fill_id,
                "fill_event": dump_payload_exact(fill),
            },
        )

    def order_states(self) -> list[OrderState]:
        return [self._hydrate_order_state(row) for row in self.execution_order_repo.list_orders(limit=None)]

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        row = self.execution_order_repo.get_order_by_client_order_id(client_order_id)
        return None if row is None else self._hydrate_order_state(row)

    def recent_order_states(
        self,
        *,
        limit: int = 20,
        statuses: tuple[str, ...] | None = None,
    ) -> list[OrderState]:
        states = self.order_states()
        if statuses is not None:
            status_set = set(statuses)
            states = [state for state in states if state.status in status_set]
        states.sort(key=lambda item: (item.last_update_ts or item.created_at, item.client_order_id), reverse=True)
        return states[:limit]

    def open_order_states(self) -> list[OrderState]:
        return [self._hydrate_order_state(row) for row in self.execution_order_repo.open_orders()]

    def fills(self) -> list[FillEvent]:
        return [self._hydrate_fill(row) for row in self.execution_fill_repo.fills_since(limit=None)]

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        return [self._hydrate_fill(row) for row in self.execution_fill_repo.fills_since(since=since, limit=limit)]

    def fills_for_order(self, client_order_id: str) -> list[FillEvent]:
        row = self.execution_order_repo.get_order_by_client_order_id(client_order_id)
        order_id = client_order_id if row is None else str(row["order_id"])
        return [self._hydrate_fill(fill) for fill in self.execution_fill_repo.fills_for_order(order_id)]

    def order_states_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        statuses: tuple[str, ...] | None = None,
        limit: int | None = None,
        open_only: bool = False,
    ) -> list[OrderState]:
        states = [
            state
            for state in self.order_states()
            if state.product_type == scope.product_type and state.margin_mode == scope.margin_mode
        ]
        if scope.allowed_symbols:
            allowed = set(scope.allowed_symbols)
            states = [state for state in states if state.symbol in allowed]
        if open_only:
            states = [state for state in states if state.status not in _TERMINAL_STATUSES]
        if statuses is not None:
            status_set = set(statuses)
            states = [state for state in states if state.status in status_set]
        if limit is not None:
            return states[:limit]
        return states

    def fills_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        fills = self.fills_since(since=since, limit=None)
        fills = [
            fill
            for fill in fills
            if fill.product_type == scope.product_type and fill.margin_mode == scope.margin_mode
        ]
        if scope.allowed_symbols:
            allowed = set(scope.allowed_symbols)
            fills = [fill for fill in fills if fill.symbol in allowed]
        if limit is not None:
            return fills[:limit]
        return fills

    @staticmethod
    def _hydrate_order_state(row: dict) -> OrderState:
        payload = dict(row.get("raw_payload") or {})
        order_payload = payload.get("order_state")
        if isinstance(order_payload, dict):
            return OrderState.model_validate(order_payload)
        return OrderState(
            decision_id=str(row.get("decision_id") or ""),
            intent_id=str(row.get("intent_id") or ""),
            symbol=str(row.get("symbol") or ""),
            client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
            exchange_order_id=row.get("venue_order_id"),
            status=str(row.get("state") or "CREATED"),
            submission_mode=str(payload.get("source_system") or "converged_execution_repo"),
            submitted_ts=row.get("created_at") if str(row.get("state") or "CREATED") != "CREATED" else None,
            last_update_ts=row.get("updated_at") or row.get("created_at"),
            last_exchange_update_ts=row.get("last_exchange_ts"),
            requested_qty=Decimal(str(row.get("requested_qty") or "0")),
            filled_qty=Decimal("0"),
            remaining_qty=Decimal(str(row.get("requested_qty") or "0")),
            average_fill_price=None,
            fees=Decimal("0"),
            reduce_only=bool(row.get("reduce_only", False)),
            close_only=bool(row.get("close_only", False)),
            td_mode=(
                row.get("td_mode")
                or payload.get("td_mode")
                or payload.get("fill_event", {}).get("td_mode")
                or row.get("margin_mode")
            ),
            position_mode=row.get("position_mode") or payload.get("position_mode"),
            pos_side=row.get("pos_side") or payload.get("pos_side"),
            reduce_only_reason=row.get("reduce_only_reason") or payload.get("reduce_only_reason"),
            close_only_reason=row.get("close_only_reason") or payload.get("close_only_reason"),
            instrument_family=row.get("instrument_family") or payload.get("instrument_family"),
            settle_currency=row.get("settle_currency") or payload.get("settle_currency"),
            product_type=str(row.get("product_type") or "spot"),
            margin_mode=str(row.get("margin_mode") or "cash"),
            target_leverage=float(payload.get("target_leverage") or 1.0),
            exposure_side=str(payload.get("exposure_side") or "flat"),
            execution_action=row.get("execution_action"),
            position_intent=str(row.get("position_intent") or "open_long"),
            submission_payload={},
        )

    @staticmethod
    def _hydrate_fill(row: dict) -> FillEvent:
        payload = dict(row.get("raw_payload") or {})
        fill_payload = payload.get("fill_event")
        if isinstance(fill_payload, dict):
            return FillEvent.model_validate(fill_payload)
        return FillEvent(
            fill_id=str(row["fill_id"]),
            decision_id=str(row.get("decision_id") or ""),
            intent_id=str(row.get("intent_id") or ""),
            client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
            exchange_order_id=str(row.get("venue_order_id") or ""),
            symbol=str(row["symbol"]),
            venue=str(row.get("source_system") or "OKX").upper(),
            side=str(row["side"]),
            fill_qty=Decimal(str(row["fill_qty"])),
            fill_price=Decimal(str(row["fill_price"])),
            fee_amount=Decimal(str(row.get("fee_amount") or "0")),
            fee_currency=row.get("fee_currency"),
            reduce_only=bool(row.get("reduce_only", False)),
            close_only=bool(row.get("close_only", False)),
            td_mode=row.get("td_mode") or payload.get("td_mode"),
            position_mode=row.get("position_mode") or payload.get("position_mode"),
            pos_side=row.get("pos_side") or payload.get("pos_side"),
            reduce_only_reason=row.get("reduce_only_reason") or payload.get("reduce_only_reason"),
            close_only_reason=row.get("close_only_reason") or payload.get("close_only_reason"),
            instrument_family=row.get("instrument_family") or payload.get("instrument_family"),
            settle_currency=row.get("settle_currency") or payload.get("settle_currency"),
            liquidity_role=str(row.get("liquidity_role") or "taker"),
            exchange_timestamp=row["exchange_ts"],
            ingestion_timestamp=row["ingestion_ts"],
        )

    @staticmethod
    def _intent_from_order_state(order_state: OrderState) -> OrderIntent:
        side = "buy"
        if order_state.position_intent in {"open_short", "reduce_short", "close_short"}:
            side = "sell"
        return OrderIntent(
            intent_id=order_state.intent_id,
            decision_id=order_state.decision_id,
            symbol=order_state.symbol,
            side=side,
            quantity=order_state.requested_qty,
            execution_style=order_state.submission_mode or "converged_execution_repo",
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
            position_intent=order_state.position_intent,
        )
