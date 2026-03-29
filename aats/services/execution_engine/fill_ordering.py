from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aats.schemas.execution import FillEvent

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def fill_processing_sort_key(fill: FillEvent | Any) -> tuple[datetime, datetime, str]:
    exchange_timestamp = getattr(fill, "exchange_timestamp", None)
    ingestion_timestamp = getattr(fill, "ingestion_timestamp", None)
    created_at = getattr(fill, "created_at", None)
    fill_id = str(getattr(fill, "fill_id", ""))
    primary = exchange_timestamp or ingestion_timestamp or created_at or _EPOCH
    secondary = ingestion_timestamp or exchange_timestamp or created_at or _EPOCH
    return (primary, secondary, fill_id)


def sorted_fills(fills: list[FillEvent] | tuple[FillEvent, ...]) -> list[FillEvent]:
    return sorted(fills, key=fill_processing_sort_key)
