from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from aats.schemas.execution import FillEvent, OrderIntent, OrderState


class ExchangeAdapter(ABC):
    @abstractmethod
    async def submit(self, intent: OrderIntent) -> tuple[OrderState, list[FillEvent]]:
        raise NotImplementedError

    async def cancel(self, order_state: OrderState) -> tuple[OrderState, list[FillEvent]]:
        raise NotImplementedError

    async def sync(self, open_order_states: list[OrderState]) -> tuple[list[OrderState], list[FillEvent]]:
        return [], []

    def readiness(self) -> dict[str, Any]:
        return {"ready": True, "backend": self.__class__.__name__}
