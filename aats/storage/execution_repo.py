from __future__ import annotations

from aats.schemas.execution import FillEvent, OrderState


class InMemoryExecutionRepository:
    def __init__(self) -> None:
        self._order_states_by_client_order_id: dict[str, OrderState] = {}
        self._order_states_by_intent_id: dict[str, OrderState] = {}
        self._fills_by_fill_id: dict[str, FillEvent] = {}

    def save_order_state(self, state: OrderState) -> None:
        self._order_states_by_client_order_id[state.client_order_id] = state
        self._order_states_by_intent_id[state.intent_id] = state

    def has_intent(self, intent_id: str) -> bool:
        return intent_id in self._order_states_by_intent_id

    def save_fill(self, fill: FillEvent) -> bool:
        if fill.fill_id in self._fills_by_fill_id:
            return False
        self._fills_by_fill_id[fill.fill_id] = fill
        return True

    def order_states(self) -> list[OrderState]:
        return list(self._order_states_by_client_order_id.values())

    def fills(self) -> list[FillEvent]:
        return list(self._fills_by_fill_id.values())
