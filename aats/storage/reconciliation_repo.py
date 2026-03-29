from __future__ import annotations

from aats.schemas.reconciliation import (
    BaselineGenerationRecord,
    ExchangeAckWatermark,
    ReconciliationFinding,
    ReconciliationReport,
    ReconciliationStateSnapshot,
)
from aats.services.runtime_scope import RuntimeStateScope, latest_matching_reconciliation, reconciliation_report_matches_scope


class InMemoryReconciliationRepository:
    def __init__(self) -> None:
        self._reports: list[ReconciliationReport] = []
        self._findings: list[ReconciliationFinding] = []
        self._state_snapshots: list[ReconciliationStateSnapshot] = []
        self._baseline_generations: list[BaselineGenerationRecord] = []
        self._exchange_ack_watermarks: list[ExchangeAckWatermark] = []

    def save_report(self, report: ReconciliationReport) -> None:
        self._reports.append(report)
        if report.findings:
            self.save_findings(report.findings)

    def save_findings(self, findings: list[ReconciliationFinding]) -> None:
        self._findings = [item for item in self._findings if item.reconciliation_id != findings[0].reconciliation_id] if findings else self._findings
        self._findings.extend(findings)

    def findings_for_reconciliation(self, *, reconciliation_id: str) -> list[ReconciliationFinding]:
        return [item for item in self._findings if item.reconciliation_id == reconciliation_id]

    def save_state_snapshot(self, snapshot: ReconciliationStateSnapshot) -> None:
        self._state_snapshots.append(snapshot)

    def latest_state_snapshot_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> ReconciliationStateSnapshot | None:
        rows = [
            item
            for item in self._state_snapshots
            if item.product_type == scope.product_type and item.margin_mode == scope.margin_mode
        ]
        return rows[-1] if rows else None

    def save_baseline_generation(self, generation: BaselineGenerationRecord) -> None:
        self._baseline_generations.append(generation)

    def latest_baseline_generation_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> BaselineGenerationRecord | None:
        rows = [
            item
            for item in self._baseline_generations
            if item.product_type == scope.product_type and item.margin_mode == scope.margin_mode
        ]
        return rows[-1] if rows else None

    def save_exchange_ack_watermark(self, watermark: ExchangeAckWatermark) -> None:
        self._exchange_ack_watermarks.append(watermark)

    def latest_exchange_ack_watermark_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
    ) -> ExchangeAckWatermark | None:
        rows = [
            item
            for item in self._exchange_ack_watermarks
            if item.product_type == scope.product_type and item.margin_mode == scope.margin_mode
        ]
        return rows[-1] if rows else None

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
