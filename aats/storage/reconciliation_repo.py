from __future__ import annotations

from aats.schemas.reconciliation import ReconciliationReport


class InMemoryReconciliationRepository:
    def __init__(self) -> None:
        self._reports: list[ReconciliationReport] = []

    def save_report(self, report: ReconciliationReport) -> None:
        self._reports.append(report)

    def latest(self) -> ReconciliationReport | None:
        return self._reports[-1] if self._reports else None

    def history(self) -> list[ReconciliationReport]:
        return list(self._reports)
