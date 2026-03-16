from __future__ import annotations

from aats.schemas.reconciliation import ReconciliationReport
from aats.services.runtime_scope import RuntimeStateScope, latest_matching_reconciliation, reconciliation_report_matches_scope


class InMemoryReconciliationRepository:
    def __init__(self) -> None:
        self._reports: list[ReconciliationReport] = []

    def save_report(self, report: ReconciliationReport) -> None:
        self._reports.append(report)

    def latest(self) -> ReconciliationReport | None:
        return self._reports[-1] if self._reports else None

    def history(self) -> list[ReconciliationReport]:
        return list(self._reports)

    def recent_history(self, *, limit: int) -> list[ReconciliationReport]:
        if limit <= 0:
            return []
        return list(self._reports[-limit:])

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[ReconciliationReport]:
        rows = [report for report in self._reports if reconciliation_report_matches_scope(report, scope)]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> ReconciliationReport | None:
        return latest_matching_reconciliation(self._reports, scope)
