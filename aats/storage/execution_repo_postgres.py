from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.execution import FillEvent, OrderState
from aats.services.execution_engine.state_machine import OrderStateMachine
from aats.storage.sqlalchemy_models import FillEventModel, OrderStateModel


class PostgresExecutionRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.state_machine = OrderStateMachine()

    def save_order_state(self, state: OrderState) -> OrderState:
        with self.session_factory() as session:
            row = session.get(OrderStateModel, state.client_order_id)
            if row is None:
                row = session.scalar(
                    select(OrderStateModel).where(OrderStateModel.intent_id == state.intent_id).limit(1)
                )
            current = self._to_order_state(row) if row is not None else None
            merged = self.state_machine.merge(current=current, incoming=state)
            payload = merged.model_dump(mode="json")
            if row is None:
                row = OrderStateModel(
                    client_order_id=merged.client_order_id,
                    decision_id=merged.decision_id,
                    intent_id=merged.intent_id,
                    exchange_order_id=merged.exchange_order_id,
                    created_at=merged.created_at,
                    status=merged.status,
                    submitted_ts=merged.submitted_ts,
                    last_update_ts=merged.last_update_ts,
                    requested_qty=merged.requested_qty,
                    filled_qty=merged.filled_qty,
                    remaining_qty=merged.remaining_qty,
                    average_fill_price=merged.average_fill_price,
                    fees=merged.fees,
                    payload=payload,
                )
                session.add(row)
            else:
                if row.client_order_id != merged.client_order_id:
                    session.delete(row)
                    session.flush()
                    row = OrderStateModel(client_order_id=merged.client_order_id)
                    session.add(row)
                row.decision_id = merged.decision_id
                row.intent_id = merged.intent_id
                row.exchange_order_id = merged.exchange_order_id
                row.created_at = merged.created_at
                row.status = merged.status
                row.submitted_ts = merged.submitted_ts
                row.last_update_ts = merged.last_update_ts
                row.requested_qty = merged.requested_qty
                row.filled_qty = merged.filled_qty
                row.remaining_qty = merged.remaining_qty
                row.average_fill_price = merged.average_fill_price
                row.fees = merged.fees
                row.payload = payload
            session.commit()
            return merged

    def has_intent(self, intent_id: str) -> bool:
        with self.session_factory() as session:
            return session.scalar(select(OrderStateModel.intent_id).where(OrderStateModel.intent_id == intent_id)) is not None

    def save_fill(self, fill: FillEvent) -> bool:
        with self.session_factory() as session:
            if session.get(FillEventModel, fill.fill_id) is not None:
                return False

            session.add(
                FillEventModel(
                    fill_id=fill.fill_id,
                    decision_id=fill.decision_id,
                    intent_id=fill.intent_id,
                    client_order_id=fill.client_order_id,
                    exchange_order_id=fill.exchange_order_id,
                    symbol=fill.symbol,
                    side=fill.side,
                    fill_qty=fill.fill_qty,
                    fill_price=fill.fill_price,
                    fee_amount=fill.fee_amount,
                    exchange_timestamp=fill.exchange_timestamp,
                    ingestion_timestamp=fill.ingestion_timestamp,
                    created_at=fill.created_at,
                    payload=fill.model_dump(mode="json"),
                )
            )
            session.commit()
            return True

    def order_states(self) -> list[OrderState]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(OrderStateModel).order_by(OrderStateModel.created_at, OrderStateModel.client_order_id)
            ).all()
        return [self._to_order_state(row) for row in rows]

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        with self.session_factory() as session:
            row = session.get(OrderStateModel, client_order_id)
        return self._to_order_state(row) if row is not None else None

    def recent_order_states(
        self,
        *,
        limit: int = 20,
        statuses: tuple[str, ...] | None = None,
    ) -> list[OrderState]:
        with self.session_factory() as session:
            query = select(OrderStateModel)
            if statuses is not None:
                query = query.where(OrderStateModel.status.in_(tuple(statuses)))
            rows = session.scalars(
                query.order_by(desc(OrderStateModel.last_update_ts), desc(OrderStateModel.created_at)).limit(limit)
            ).all()
        return [self._to_order_state(row) for row in rows]

    def open_order_states(self) -> list[OrderState]:
        final_statuses = ("FILLED", "CANCELED", "REJECTED", "BLOCKED", "DRY_RUN", "FAILED", "EXPIRED")
        with self.session_factory() as session:
            rows = session.scalars(
                select(OrderStateModel)
                .where(~OrderStateModel.status.in_(final_statuses))
                .order_by(OrderStateModel.created_at, OrderStateModel.client_order_id)
            ).all()
        return [self._to_order_state(row) for row in rows]

    def fills(self) -> list[FillEvent]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FillEventModel).order_by(FillEventModel.ingestion_timestamp, FillEventModel.fill_id)
            ).all()
        return [FillEvent.model_validate(row.payload) for row in rows]

    def fills_for_order(self, client_order_id: str) -> list[FillEvent]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FillEventModel)
                .where(FillEventModel.client_order_id == client_order_id)
                .order_by(FillEventModel.ingestion_timestamp, FillEventModel.fill_id)
            ).all()
        return [FillEvent.model_validate(row.payload) for row in rows]

    @staticmethod
    def _to_order_state(row: OrderStateModel) -> OrderState:
        payload = dict(row.payload)
        payload.setdefault("decision_id", row.decision_id)
        return OrderState.model_validate(payload)
