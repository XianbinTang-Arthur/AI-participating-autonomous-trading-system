from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aats.schemas.execution import OrderIntent


class ExecutionOrderRepository(Protocol):
    def create_order(
        self,
        *,
        order_id: str,
        intent: OrderIntent,
        initial_state: str,
        created_at: datetime,
        raw_payload: dict,
    ) -> None:
        ...

    def get_order(self, order_id: str) -> dict | None:
        ...

    def get_order_by_intent(self, intent_id: str) -> dict | None:
        ...

    def get_order_by_client_order_id(self, client_order_id: str) -> dict | None:
        ...

    def update_order_state(
        self,
        *,
        order_id: str,
        expected_state_version: int,
        next_state: str,
        venue_order_id: str | None,
        last_exchange_ts: datetime | None,
        updated_at: datetime,
        raw_payload: dict,
    ) -> None:
        ...

    def open_orders(self) -> list[dict]:
        ...

    def list_orders(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        ...

    def count_orders(self) -> int:
        ...


class ExecutionOrderHistoryRepository(Protocol):
    def append_transition(
        self,
        *,
        order_id: str,
        from_state: str | None,
        to_state: str,
        reason_code: str | None,
        source: str,
        source_message_id: str | None,
        payload: dict,
        created_at: datetime,
    ) -> None:
        ...

    def history_for_order(self, order_id: str) -> list[dict]:
        ...
