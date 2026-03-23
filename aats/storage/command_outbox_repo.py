from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aats.schemas.common import EventEnvelope


class CommandOutboxRepositoryV2(Protocol):
    def enqueue(self, *, envelope: EventEnvelope, aggregate_type: str, aggregate_id: str) -> None:
        ...

    def pending(self, *, limit: int) -> list[dict]:
        ...

    def mark_published(self, event_id: str, published_at: datetime) -> None:
        ...

    def mark_failed(self, event_id: str, error: str) -> None:
        ...
