from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ExternalInboxRepository(Protocol):
    def save_incoming(
        self,
        *,
        inbox_id: str,
        source_system: str,
        dedupe_key: str,
        payload: dict,
        received_at: datetime,
    ) -> bool:
        ...

    def mark_processed(
        self,
        *,
        inbox_id: str,
        processing_result: str,
        processed_at: datetime,
        last_error: str | None = None,
    ) -> None:
        ...

    def unprocessed(self, *, limit: int) -> list[dict]:
        ...
