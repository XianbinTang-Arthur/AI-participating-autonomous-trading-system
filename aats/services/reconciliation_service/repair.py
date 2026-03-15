from __future__ import annotations

from typing import Callable

from aats.bootstrap.metrics import MetricsRegistry
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.reconciliation_service.comparator import StateComparator
from aats.services.reconciliation_service.fetcher import ExchangeStateFetcher
from aats.storage.base import EventStore
from aats.storage.base import ExecutionRepository, ReconciliationRepository
from aats.schemas.common import utc_now
from aats.storage.base import PortfolioRepository


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
        portfolio_repo: PortfolioRepository,
        event_store: EventStore,
        reconstruction_service: PortfolioReconstructionService,
        price_provider: Callable[[str], float],
        bootstrap_portfolio_from_exchange: bool = False,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.bus = bus
        self.fetcher = fetcher
        self.comparator = comparator
        self.repair_service = repair_service
        self.reconciliation_repo = reconciliation_repo
        self.execution_repo = execution_repo
        self.portfolio_repo = portfolio_repo
        self.event_store = event_store
        self.reconstruction_service = reconstruction_service
        self.price_provider = price_provider
        self.bootstrap_portfolio_from_exchange = bootstrap_portfolio_from_exchange
        self.metrics = metrics

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        snapshot = PortfolioSnapshot.model_validate(envelope.payload)
        report = self._build_report(
            decision_id=snapshot.decision_id,
            portfolio_snapshot_ref=envelope.event_id,
            stored_snapshot=snapshot,
        )
        await self._persist_report(report)

    async def validate_now(self, *, reason: str = "operator_validate") -> ReconciliationReport:
        latest_snapshot = self.portfolio_repo.latest()
        latest_snapshot_event = self.event_store.latest(topics.PORTFOLIO_SNAPSHOTS)
        if latest_snapshot is None:
            latest_snapshot = self.reconstruction_service.rebuild_snapshot(
                fills=self.execution_repo.fills(),
                price_provider=self.price_provider,
            ).model_copy(
                update={
                    "snapshot_ts": utc_now(),
                    "decision_id": self.execution_repo.order_states()[-1].decision_id if self.execution_repo.order_states() else None,
                }
            )
        report = self._build_report(
            decision_id=latest_snapshot.decision_id,
            portfolio_snapshot_ref=(
                latest_snapshot_event.event_id
                if latest_snapshot_event is not None
                else f"manual_portfolio_snapshot:{reason}:{latest_snapshot.snapshot_ts.isoformat()}"
            ),
            stored_snapshot=latest_snapshot,
        )
        await self._persist_report(report)
        return report

    def _build_report(
        self,
        *,
        decision_id: str | None,
        portfolio_snapshot_ref: str,
        stored_snapshot: PortfolioSnapshot,
    ) -> ReconciliationReport:
        order_states: list[OrderState] = self.execution_repo.order_states()
        fills: list[FillEvent] = self.execution_repo.fills()
        exchange_snapshot: ExchangeAccountSnapshot | None = self.fetcher.fetch_snapshot()
        reconstructed_snapshot = self._rebuild_snapshot_for_comparison(
            stored_snapshot=stored_snapshot,
            fills=fills,
        )
        exchange_comparison_enabled = any(order.venue == "OKX" for order in order_states) or any(
            fill.venue == "OKX" for fill in fills
        )
        report = self.comparator.compare(
            decision_id=decision_id,
            portfolio_snapshot_ref=portfolio_snapshot_ref,
            order_states=order_states,
            fills=fills,
            stored_snapshot=stored_snapshot,
            reconstructed_snapshot=reconstructed_snapshot,
            exchange_snapshot=exchange_snapshot,
            exchange_comparison_enabled=exchange_comparison_enabled,
            compare_exchange_portfolio=exchange_comparison_enabled and self.bootstrap_portfolio_from_exchange,
        )
        return report

    def _rebuild_snapshot_for_comparison(
        self,
        *,
        stored_snapshot: PortfolioSnapshot,
        fills: list[FillEvent],
    ) -> PortfolioSnapshot:
        if not self.bootstrap_portfolio_from_exchange:
            return self.reconstruction_service.rebuild_snapshot(
                fills=fills,
                price_provider=self.price_provider,
            )

        baseline_snapshot = self._bootstrap_baseline_snapshot()
        if baseline_snapshot is None:
            return self.reconstruction_service.rebuild_snapshot(
                fills=fills,
                price_provider=self.price_provider,
            )

        state = PortfolioState(initial_usdt_balance=self.reconstruction_service.initial_usdt_balance)
        state.load_portfolio_snapshot(baseline_snapshot)
        baseline_ts = baseline_snapshot.snapshot_ts
        for fill in sorted(fills, key=lambda item: (item.ingestion_timestamp, item.fill_id)):
            if fill.ingestion_timestamp >= baseline_ts:
                state.apply_fill(fill)
        return self.reconstruction_service.snapshot_builder.build(
            state=state,
            price_provider=self.price_provider,
        )

    def _bootstrap_baseline_snapshot(self) -> PortfolioSnapshot | None:
        candidates = [
            snapshot
            for snapshot in self.portfolio_repo.history()
            if snapshot.source_fill_id is None and snapshot.source_intent_id is None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item.snapshot_ts, item.created_at))

    async def _persist_report(self, report: ReconciliationReport) -> None:
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
