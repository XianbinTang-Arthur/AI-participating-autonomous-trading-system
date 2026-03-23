from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aats.schemas.execution import FillEvent


class ExecutionFillRepositoryV2(Protocol):
    def save_fill(
        self,
        *,
        fill: FillEvent,
        order_id: str,
        source: str,
        raw_payload: dict,
    ) -> bool:
        ...

    def get_fill(self, fill_id: str) -> dict | None:
        ...

    def get_fill_by_dedupe_key(self, source: str, venue_fill_id: str | None) -> dict | None:
        ...

    def fills_for_order(self, order_id: str) -> list[dict]:
        ...

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        ...

    def count_fills(self) -> int:
        ...
