from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol


class PositionLotRepository(Protocol):
    def replace_scope(
        self,
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
        lots: list[dict],
    ) -> None:
        ...

    def lots_for_scope(
        self,
        *,
        symbol: str | None = None,
        product_type: str,
        margin_mode: str,
        open_only: bool = False,
    ) -> list[dict]:
        ...


class LotEventRepository(Protocol):
    def replace_scope(
        self,
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
        events: list[dict],
    ) -> None:
        ...

    def events_for_fill(self, fill_id: str) -> list[dict]:
        ...

