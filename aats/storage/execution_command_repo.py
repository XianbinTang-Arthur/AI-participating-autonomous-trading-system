from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ExecutionCommandRepository(Protocol):
    def enqueue_command(
        self,
        *,
        command_id: str,
        order_id: str,
        command_type: str,
        idempotency_key: str,
        payload: dict,
        created_at: datetime,
    ) -> None:
        ...

    def get_command(self, command_id: str) -> dict | None:
        ...

    def get_by_idempotency_key(self, idempotency_key: str) -> dict | None:
        ...

    def pending_commands(self, *, limit: int, sent_stale_before: datetime | None = None) -> list[dict]:
        ...

    def claim_command(
        self,
        *,
        command_id: str,
        expected_state: str,
        expected_updated_at: datetime,
        updated_at: datetime,
    ) -> bool:
        ...

    def mark_sent(self, command_id: str, updated_at: datetime) -> None:
        ...

    def mark_acked(self, command_id: str, updated_at: datetime) -> None:
        ...

    def mark_failed(self, command_id: str, error: str, updated_at: datetime) -> None:
        ...

    def mark_abandoned(self, command_id: str, reason: str, updated_at: datetime) -> None:
        ...
