from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from aats.schemas.execution import FillEvent, LegOrderIntent, OrderIntent, OrderState, order_intent_from_leg_order_intent


class ExchangeAdapter(ABC):
    @abstractmethod
    async def submit(self, intent: OrderIntent) -> tuple[OrderState, list[FillEvent]]:
        raise NotImplementedError

    async def submit_leg_order(self, leg_intent: LegOrderIntent) -> tuple[OrderState, list[FillEvent]]:
        return await self.submit(order_intent_from_leg_order_intent(leg_intent))

    def preview_client_order_id(self, intent: OrderIntent) -> str | None:
        return None

    async def cancel(self, order_state: OrderState) -> tuple[OrderState, list[FillEvent]]:
        raise NotImplementedError

    async def sync(self, open_order_states: list[OrderState]) -> tuple[list[OrderState], list[FillEvent]]:
        return [], []

    def readiness(self) -> dict[str, Any]:
        return {"ready": True, "backend": self.__class__.__name__}
