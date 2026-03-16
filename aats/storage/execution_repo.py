from __future__ import annotations

from datetime import datetime

from aats.schemas.execution import FillEvent, OrderState
from aats.services.execution_engine.state_machine import OrderStateMachine


class InMemoryExecutionRepository:
    def __init__(self) -> None:
        self._order_states_by_client_order_id: dict[str, OrderState] = {}
        self._order_states_by_intent_id: dict[str, OrderState] = {}
        self._fills_by_fill_id: dict[str, FillEvent] = {}
        self._state_machine = OrderStateMachine()

    def save_order_state(self, state: OrderState) -> OrderState:
        current = self._order_states_by_client_order_id.get(state.client_order_id)
        if current is None:
            current = self._order_states_by_intent_id.get(state.intent_id)
        merged = self._state_machine.merge(current=current, incoming=state)
        if current is not None and current.client_order_id != merged.client_order_id:
            self._order_states_by_client_order_id.pop(current.client_order_id, None)
        self._order_states_by_client_order_id[merged.client_order_id] = merged
        self._order_states_by_intent_id[merged.intent_id] = merged
        return merged

    def has_intent(self, intent_id: str) -> bool:
        return intent_id in self._order_states_by_intent_id

    def save_fill(self, fill: FillEvent) -> bool:
        if fill.fill_id in self._fills_by_fill_id:
            return False
        self._fills_by_fill_id[fill.fill_id] = fill
        return True

    def order_states(self) -> list[OrderState]:
        return list(self._order_states_by_client_order_id.values())

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        return self._order_states_by_client_order_id.get(client_order_id)

    def recent_order_states(
        self,
        *,
        limit: int = 20,
        statuses: tuple[str, ...] | None = None,
    ) -> list[OrderState]:
        rows = sorted(
            self.order_states(),
            key=lambda item: (item.last_update_ts or item.created_at, item.client_order_id),
            reverse=True,
        )
        if statuses is not None:
            allowed = {status.upper() for status in statuses}
            rows = [row for row in rows if row.status.upper() in allowed]
        return rows[:limit]

    def open_order_states(self) -> list[OrderState]:
        return [state for state in self.order_states() if self._state_machine.is_open(state.status)]

    def fills(self) -> list[FillEvent]:
        return list(self._fills_by_fill_id.values())

    def fills_for_order(self, client_order_id: str) -> list[FillEvent]:
        return sorted(
            [fill for fill in self._fills_by_fill_id.values() if fill.client_order_id == client_order_id],
            key=lambda item: (item.ingestion_timestamp, item.fill_id),
        )

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        rows = sorted(
            self._fills_by_fill_id.values(),
            key=lambda item: (item.ingestion_timestamp, item.fill_id),
        )
        if since is not None:
            rows = [fill for fill in rows if fill.ingestion_timestamp >= since]
        if limit is not None:
            rows = rows[-limit:]
        return rows
