from __future__ import annotations

from aats.schemas.execution import OrderObligation


class InMemoryExecutionObligationRepository:
    def __init__(self) -> None:
        self._obligations_by_client_order_id: dict[str, OrderObligation] = {}

    def save_obligation(self, obligation: OrderObligation) -> OrderObligation:
        self._obligations_by_client_order_id[obligation.client_order_id] = obligation
        return obligation

    def get_obligation(self, client_order_id: str) -> OrderObligation | None:
        return self._obligations_by_client_order_id.get(client_order_id)

    def active_obligations(self) -> list[OrderObligation]:
        return [
            obligation
            for obligation in self._obligations_by_client_order_id.values()
            if obligation.status in {"ACTIVE", "PARTIALLY_CONSUMED"}
        ]

    def all_obligations(self) -> list[OrderObligation]:
        return list(self._obligations_by_client_order_id.values())
