from __future__ import annotations

from abc import ABC, abstractmethod

from aats.schemas.execution import FillEvent, OrderIntent, OrderState


class ExchangeAdapter(ABC):
    @abstractmethod
    def submit(self, intent: OrderIntent) -> tuple[OrderState, list[FillEvent]]:
        raise NotImplementedError

