from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.execution import FillEvent, OrderState
from aats.storage.sqlalchemy_models import FillEventModel, OrderStateModel


class PostgresExecutionRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_order_state(self, state: OrderState) -> None:
        with self.session_factory() as session:
            row = session.get(OrderStateModel, state.client_order_id)
            payload = state.model_dump(mode="json")
            if row is None:
                row = OrderStateModel(
                    client_order_id=state.client_order_id,
                    intent_id=state.intent_id,
                    exchange_order_id=state.exchange_order_id,
                    created_at=state.created_at,
                    status=state.status,
                    submitted_ts=state.submitted_ts,
                    last_update_ts=state.last_update_ts,
                    requested_qty=state.requested_qty,
                    filled_qty=state.filled_qty,
                    remaining_qty=state.remaining_qty,
                    average_fill_price=state.average_fill_price,
                    fees=state.fees,
                    payload=payload,
                )
                session.add(row)
            else:
                row.intent_id = state.intent_id
                row.exchange_order_id = state.exchange_order_id
                row.created_at = state.created_at
                row.status = state.status
                row.submitted_ts = state.submitted_ts
                row.last_update_ts = state.last_update_ts
                row.requested_qty = state.requested_qty
                row.filled_qty = state.filled_qty
                row.remaining_qty = state.remaining_qty
                row.average_fill_price = state.average_fill_price
                row.fees = state.fees
                row.payload = payload
            session.commit()

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
        return [OrderState.model_validate(row.payload) for row in rows]

    def fills(self) -> list[FillEvent]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FillEventModel).order_by(FillEventModel.ingestion_timestamp, FillEventModel.fill_id)
            ).all()
        return [FillEvent.model_validate(row.payload) for row in rows]
