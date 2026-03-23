from __future__ import annotations

from datetime import datetime

from aats.schemas.portfolio import FillOutcomeRecord
from aats.services.runtime_scope import RuntimeStateScope, filter_fill_outcomes


class InMemoryFillOutcomeRepository:
    def __init__(self) -> None:
        self._outcomes_by_fill_id: dict[str, FillOutcomeRecord] = {}

    def save_outcome(self, outcome: FillOutcomeRecord) -> FillOutcomeRecord:
        current = self._outcomes_by_fill_id.get(outcome.fill_id)
        merged = outcome if current is None else current.model_copy(update=outcome.model_dump(mode="python"))
        self._outcomes_by_fill_id[merged.fill_id] = merged
        return merged

    def get_outcome(self, fill_id: str) -> FillOutcomeRecord | None:
        return self._outcomes_by_fill_id.get(fill_id)

    def outcomes(self) -> list[FillOutcomeRecord]:
        return list(self._outcomes_by_fill_id.values())

    def outcomes_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillOutcomeRecord]:
        rows = filter_fill_outcomes(self.outcomes(), scope)
        rows = sorted(rows, key=lambda item: (item.created_at, item.fill_id))
        if since is not None:
            rows = [row for row in rows if row.created_at >= since]
        if limit is not None:
            rows = rows[-limit:]
        return rows
