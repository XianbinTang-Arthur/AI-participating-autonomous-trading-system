from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from aats.storage.sqlalchemy_models import ReservationModel


class PostgresReservationRepository:
    _EPSILON = Decimal("1e-18")

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def create_reservation(
        self,
        *,
        reservation_id: str,
        order_id: str,
        reserve_account_id: str,
        reserved_amount: Decimal,
        state: str,
        created_at: datetime,
    ) -> None:
        with self.session_factory() as session:
            self.create_reservation_in_session(
                session,
                reservation_id=reservation_id,
                order_id=order_id,
                reserve_account_id=reserve_account_id,
                reserved_amount=reserved_amount,
                state=state,
                created_at=created_at,
            )
            session.commit()

    def create_reservation_in_session(
        self,
        session: Session,
        *,
        reservation_id: str,
        order_id: str,
        reserve_account_id: str,
        reserved_amount: Decimal,
        state: str,
        created_at: datetime,
    ) -> None:
        existing = self.get_by_order_id_in_session(session, order_id, for_update=True)
        if existing is not None:
            return
        session.execute(
            insert(ReservationModel)
            .values(
                reservation_id=reservation_id,
                order_id=order_id,
                reserve_account_id=reserve_account_id,
                reserved_amount=reserved_amount,
                consumed_amount=Decimal("0"),
                released_amount=Decimal("0"),
                state=state,
                created_at=created_at,
                updated_at=created_at,
            )
            .on_conflict_do_nothing()
        )

    def get_by_order_id(self, order_id: str) -> dict | None:
        with self.session_factory() as session:
            row = session.scalar(select(ReservationModel).where(ReservationModel.order_id == order_id).limit(1))
        return _reservation_row_to_dict(row) if row is not None else None

    def get_by_order_id_in_session(self, session: Session, order_id: str, *, for_update: bool = False) -> dict | None:
        query = select(ReservationModel).where(ReservationModel.order_id == order_id).limit(1)
        if for_update:
            query = query.with_for_update()
        row = session.scalar(query)
        return _reservation_row_to_dict(row) if row is not None else None

    def consume(self, *, reservation_id: str, amount: Decimal, updated_at: datetime) -> None:
        with self.session_factory() as session:
            self.consume_in_session(
                session,
                reservation_id=reservation_id,
                amount=amount,
                updated_at=updated_at,
            )
            session.commit()

    def consume_in_session(self, session: Session, *, reservation_id: str, amount: Decimal, updated_at: datetime) -> None:
        if amount < Decimal("0"):
            raise ValueError("reservation_consume_amount_must_be_non_negative")
        row = session.get(ReservationModel, reservation_id, with_for_update=True)
        if row is None:
            raise KeyError(f"reservation_not_found:{reservation_id}")
        next_consumed = row.consumed_amount + amount
        if next_consumed + row.released_amount > row.reserved_amount + self._EPSILON:
            raise ValueError(
                f"reservation_over_consume:{reservation_id}:{next_consumed}:{row.released_amount}:{row.reserved_amount}"
            )
        row.consumed_amount = next_consumed
        row.updated_at = updated_at
        if row.consumed_amount > 0 and row.state == "ACTIVE":
            row.state = "PARTIALLY_CONSUMED"

    def release(self, *, reservation_id: str, amount: Decimal, next_state: str, updated_at: datetime) -> None:
        with self.session_factory() as session:
            self.release_in_session(
                session,
                reservation_id=reservation_id,
                amount=amount,
                next_state=next_state,
                updated_at=updated_at,
            )
            session.commit()

    def release_in_session(
        self,
        session: Session,
        *,
        reservation_id: str,
        amount: Decimal,
        next_state: str,
        updated_at: datetime,
    ) -> None:
        if amount < Decimal("0"):
            raise ValueError("reservation_release_amount_must_be_non_negative")
        row = session.get(ReservationModel, reservation_id, with_for_update=True)
        if row is None:
            raise KeyError(f"reservation_not_found:{reservation_id}")
        next_released = row.released_amount + amount
        if row.consumed_amount + next_released > row.reserved_amount + self._EPSILON:
            raise ValueError(
                f"reservation_over_release:{reservation_id}:{row.consumed_amount}:{next_released}:{row.reserved_amount}"
            )
        row.released_amount = next_released
        row.state = next_state
        row.updated_at = updated_at

    def count_reservations(self) -> int:
        with self.session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(ReservationModel)) or 0)


def _reservation_row_to_dict(row: ReservationModel) -> dict:
    return {
        "reservation_id": row.reservation_id,
        "order_id": row.order_id,
        "reserve_account_id": row.reserve_account_id,
        "reserved_amount": row.reserved_amount,
        "consumed_amount": row.consumed_amount,
        "released_amount": row.released_amount,
        "state": row.state,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
