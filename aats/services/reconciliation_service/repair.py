from __future__ import annotations

from typing import Callable

from aats.bootstrap.metrics import MetricsRegistry
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.reconciliation_service.comparator import StateComparator
from aats.services.reconciliation_service.fetcher import ExchangeStateFetcher
from aats.storage.base import ExecutionRepository, ReconciliationRepository


class ReconciliationRepairService:
    def repair(self, report: ReconciliationReport) -> None:
        # TODO: implement remediation workflows once exchange and persistent storage are added.
        _ = report


class ReconciliationService:
    def __init__(
        self,
        *,
        bus: EventBus,
        fetcher: ExchangeStateFetcher,
        comparator: StateComparator,
        repair_service: ReconciliationRepairService,
        reconciliation_repo: ReconciliationRepository,
        execution_repo: ExecutionRepository,
        reconstruction_service: PortfolioReconstructionService,
        price_provider: Callable[[str], float],
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.bus = bus
        self.fetcher = fetcher
        self.comparator = comparator
        self.repair_service = repair_service
        self.reconciliation_repo = reconciliation_repo
        self.execution_repo = execution_repo
        self.reconstruction_service = reconstruction_service
        self.price_provider = price_provider
        self.metrics = metrics

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        snapshot = PortfolioSnapshot.model_validate(envelope.payload)
        order_states: list[OrderState] = self.execution_repo.order_states()
        fills: list[FillEvent] = self.execution_repo.fills()
        reconstructed_snapshot = self.reconstruction_service.rebuild_snapshot(
            fills=fills,
            price_provider=self.price_provider,
        )
        report = self.comparator.compare(
            decision_id=snapshot.decision_id,
            portfolio_snapshot_ref=envelope.event_id,
            order_states=order_states,
            fills=fills,
            stored_snapshot=snapshot,
            reconstructed_snapshot=reconstructed_snapshot,
        )
        self.reconciliation_repo.save_report(report)
        if report.severity != "CLEAN":
            if self.metrics is not None:
                self.metrics.increment("reconciliation_mismatches")
            self.repair_service.repair(report)
        await publish_model(
            bus=self.bus,
            topic=topics.RECONCILIATION_REPORTS,
            key="portfolio",
            payload_model=report,
            source_component="reconciliation_service",
        )
