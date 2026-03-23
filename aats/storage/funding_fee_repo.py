from __future__ import annotations

from datetime import datetime

from aats.schemas.portfolio import FundingFeeRecord
from aats.services.runtime_scope import RuntimeStateScope, filter_funding_fee_records


class InMemoryFundingFeeRepository:
    def __init__(self) -> None:
        self._records_by_bill_id: dict[str, FundingFeeRecord] = {}

    def save_record(self, record: FundingFeeRecord) -> FundingFeeRecord:
        current = self._records_by_bill_id.get(record.bill_id)
        merged = record if current is None else current.model_copy(update=record.model_dump(mode="python"))
        self._records_by_bill_id[merged.bill_id] = merged
        return merged

    def get_record(self, bill_id: str) -> FundingFeeRecord | None:
        return self._records_by_bill_id.get(bill_id)

    def records(self) -> list[FundingFeeRecord]:
        return list(self._records_by_bill_id.values())

    def records_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FundingFeeRecord]:
        rows = filter_funding_fee_records(self.records(), scope)
        rows = sorted(rows, key=lambda item: (item.bill_ts or item.created_at, item.bill_id))
        if since is not None:
            rows = [row for row in rows if row.created_at >= since]
        if limit is not None:
            rows = rows[-limit:]
        return rows
