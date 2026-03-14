from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

MessageHandler = Callable[[dict], Awaitable[None]]


class EventBus(ABC):
    @abstractmethod
    async def publish(self, topic: str, key: str, payload: dict) -> None:
        raise NotImplementedError

    @abstractmethod
    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        raise NotImplementedError

