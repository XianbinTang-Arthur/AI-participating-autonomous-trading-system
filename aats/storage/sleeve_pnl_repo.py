from __future__ import annotations

from datetime import datetime

from aats.schemas.portfolio import SleevePnLRecord
from aats.services.runtime_scope import RuntimeStateScope, filter_sleeve_pnl_records


class InMemorySleevePnLRepository:
    def __init__(self) -> None:
        self._records_by_id: dict[str, SleevePnLRecord] = {}

    def save_record(self, record: SleevePnLRecord) -> SleevePnLRecord:
        current = self._records_by_id.get(record.record_id)
        merged = record if current is None else current.model_copy(update=record.model_dump(mode="python"))
        self._records_by_id[merged.record_id] = merged
        return merged

    def get_record(self, record_id: str) -> SleevePnLRecord | None:
        return self._records_by_id.get(record_id)

    def records(self) -> list[SleevePnLRecord]:
        return list(self._records_by_id.values())

    def records_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[SleevePnLRecord]:
        rows = filter_sleeve_pnl_records(self.records(), scope)
        rows = sorted(rows, key=lambda item: (item.event_timestamp or item.created_at, item.record_id))
        if since is not None:
            rows = [row for row in rows if row.created_at >= since]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    def replace_scope(
        self,
        *,
        scope: RuntimeStateScope,
        records: list[SleevePnLRecord],
    ) -> None:
        keys_to_remove = [
            record_id
            for record_id, record in self._records_by_id.items()
            if record.product_type == scope.product_type
            and record.margin_mode == scope.margin_mode
            and (record.symbol in {None, ""} or scope.symbol_allowed(record.symbol))
        ]
        for record_id in keys_to_remove:
            self._records_by_id.pop(record_id, None)
        for record in records:
            self._records_by_id[record.record_id] = record
