from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session


class ReservationRepositoryV2(Protocol):
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
        ...

    def get_by_order_id(self, order_id: str) -> dict | None:
        ...

    def get_by_order_id_in_session(self, session: Session, order_id: str, *, for_update: bool = False) -> dict | None:
        ...

    def consume(self, *, reservation_id: str, amount: Decimal, updated_at: datetime) -> None:
        ...

    def consume_in_session(self, session: Session, *, reservation_id: str, amount: Decimal, updated_at: datetime) -> None:
        ...

    def release(self, *, reservation_id: str, amount: Decimal, next_state: str, updated_at: datetime) -> None:
        ...

    def release_in_session(
        self,
        session: Session,
        *,
        reservation_id: str,
        amount: Decimal,
        next_state: str,
        updated_at: datetime,
    ) -> None:
        ...

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
        ...

    def count_reservations(self) -> int:
        ...
