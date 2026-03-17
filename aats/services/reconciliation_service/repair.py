from __future__ import annotations

from typing import Callable
from dataclasses import dataclass

from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
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
from aats.services.governance_engine.runtime_layers import RecoveryPolicy
from aats.storage.base import EventStore
from aats.storage.base import ExecutionRepository, ReconciliationRepository
from aats.schemas.common import utc_now
from aats.services.runtime_scope import (
    fills_for_scope,
    latest_snapshot_for_scope,
    latest_topic_event_for_scope,
    order_states_for_scope,
    snapshots_for_scope,
    runtime_state_scope,
)
from aats.storage.base import PortfolioRepository


@dataclass(slots=True)
class ReconciliationRepairService:
    portfolio_repo: PortfolioRepository | None = None
    execution_repo: ExecutionRepository | None = None
    reconstruction_service: PortfolioReconstructionService | None = None
    price_provider: Callable[[str], float] | None = None
    runtime_scope: object | None = None

    def configure(
        self,
        *,
        portfolio_repo: PortfolioRepository,
        execution_repo: ExecutionRepository,
        reconstruction_service: PortfolioReconstructionService,
        price_provider: Callable[[str], float],
        runtime_scope,
    ) -> None:
        self.portfolio_repo = portfolio_repo
        self.execution_repo = execution_repo
        self.reconstruction_service = reconstruction_service
        self.price_provider = price_provider
        self.runtime_scope = runtime_scope

    def repair(self, report: ReconciliationReport) -> PortfolioSnapshot | None:
        if (
            self.portfolio_repo is None
            or self.execution_repo is None
            or self.reconstruction_service is None
            or self.price_provider is None
            or self.runtime_scope is None
        ):
            return None
        if report.order_diff.get("exchange") or report.fill_diff.get("exchange"):
            return None
        if report.balance_diff.get("exchange") or report.position_diff.get("exchange_mismatches"):
            return None
        if report.order_diff.get("reconstructed") or report.fill_diff.get("replayed"):
            return None
        if not report.balance_diff.get("reconstructed") and not report.position_diff.get("reconstructed_mismatches"):
            return None
        rebuilt_snapshot = self.reconstruction_service.rebuild_snapshot(
            fills=fills_for_scope(self.execution_repo, self.runtime_scope),
            price_provider=self.price_provider,
        ).model_copy(
            update={
                "decision_id": report.decision_id,
                "product_type": self.runtime_scope.product_type,
                "margin_mode": self.runtime_scope.margin_mode,
            }
        )
        latest_snapshot = latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope)
        if latest_snapshot is not None and latest_snapshot.model_dump(mode="json") == rebuilt_snapshot.model_dump(mode="json"):
            return None
        self.portfolio_repo.save_snapshot(rebuilt_snapshot)
        return rebuilt_snapshot


class ReconciliationService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
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
        recovery_policy: RecoveryPolicy | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.settings = settings
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
        self.recovery_policy = recovery_policy
        self.metrics = metrics
        self.runtime_scope = runtime_state_scope(settings)
        configure = getattr(self.repair_service, "configure", None)
        if callable(configure):
            configure(
                portfolio_repo=self.portfolio_repo,
                execution_repo=self.execution_repo,
                reconstruction_service=self.reconstruction_service,
                price_provider=self.price_provider,
                runtime_scope=self.runtime_scope,
            )

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        if self._report_exists_for_portfolio_snapshot_ref(envelope.event_id):
            return
        snapshot = PortfolioSnapshot.model_validate(envelope.payload)
        report = self._build_report(
            decision_id=snapshot.decision_id,
            portfolio_snapshot_ref=envelope.event_id,
            stored_snapshot=snapshot,
        )
        await self._persist_report(report)

    async def validate_now(self, *, reason: str = "operator_validate") -> ReconciliationReport:
        latest_snapshot = latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope)
        latest_snapshot_event = latest_topic_event_for_scope(
            self.event_store,
            topics.PORTFOLIO_SNAPSHOTS,
            self.runtime_scope,
        )
        scoped_order_states = order_states_for_scope(self.execution_repo, self.runtime_scope)
        if latest_snapshot is None:
            latest_snapshot = self.reconstruction_service.rebuild_snapshot(
                fills=fills_for_scope(self.execution_repo, self.runtime_scope),
                price_provider=self.price_provider,
            ).model_copy(
                update={
                    "snapshot_ts": utc_now(),
                    "decision_id": (
                        scoped_order_states[-1].decision_id
                        if scoped_order_states
                        else None
                    ),
                    "product_type": self.runtime_scope.product_type,
                    "margin_mode": self.runtime_scope.margin_mode,
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
        order_states = order_states_for_scope(self.execution_repo, self.runtime_scope)
        fills = fills_for_scope(self.execution_repo, self.runtime_scope)
        exchange_snapshot: ExchangeAccountSnapshot | None = self.fetcher.fetch_snapshot()
        baseline_snapshot = self._bootstrap_baseline_snapshot()
        trusted_exchange_portfolio_baseline = (
            self.bootstrap_portfolio_from_exchange and baseline_snapshot is not None
        )
        reconstructed_snapshot = self._rebuild_snapshot_for_comparison(
            stored_snapshot=stored_snapshot,
            fills=fills,
        )
        exchange_comparison_enabled = exchange_snapshot is not None and (
            trusted_exchange_portfolio_baseline
            or any(order.venue == "OKX" for order in order_states)
            or any(fill.venue == "OKX" for fill in fills)
        )
        compare_exchange_portfolio = (
            exchange_snapshot is not None
            and self.bootstrap_portfolio_from_exchange
            and (self.recovery_policy.exchange_portfolio_comparison_enabled if self.recovery_policy is not None else True)
        )
        report = self.comparator.compare(
            decision_id=decision_id,
            portfolio_snapshot_ref=portfolio_snapshot_ref,
            product_type=self.runtime_scope.product_type,
            margin_mode=self.runtime_scope.margin_mode,
            allowed_symbols=list(self.runtime_scope.allowed_symbols),
            order_states=order_states,
            fills=fills,
            stored_snapshot=stored_snapshot,
            reconstructed_snapshot=reconstructed_snapshot,
            exchange_snapshot=exchange_snapshot,
            exchange_comparison_enabled=exchange_comparison_enabled,
            compare_exchange_portfolio=compare_exchange_portfolio,
            accepted_exchange_fill_ids=self._accepted_exchange_fill_ids(
                exchange_snapshot=exchange_snapshot,
                local_fills=fills,
            ),
            trusted_exchange_portfolio_baseline=trusted_exchange_portfolio_baseline,
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
            ).model_copy(
                update={
                    "product_type": self.runtime_scope.product_type,
                    "margin_mode": self.runtime_scope.margin_mode,
                }
            )

        baseline_snapshot = self._bootstrap_baseline_snapshot()
        if baseline_snapshot is None:
            return self.reconstruction_service.rebuild_snapshot(
                fills=fills,
                price_provider=self.price_provider,
            ).model_copy(
                update={
                    "product_type": self.runtime_scope.product_type,
                    "margin_mode": self.runtime_scope.margin_mode,
                }
            )

        state = PortfolioState(
            initial_usdt_balance=self.reconstruction_service.initial_usdt_balance,
            default_product_type=self.runtime_scope.product_type,
            default_margin_mode=self.runtime_scope.margin_mode,
        )
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
            for snapshot in snapshots_for_scope(self.portfolio_repo, self.runtime_scope)
            if snapshot.source_fill_id is None and snapshot.source_intent_id is None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.snapshot_ts, item.created_at))

    def _accepted_exchange_fill_ids(
        self,
        *,
        exchange_snapshot: ExchangeAccountSnapshot | None,
        local_fills: list[FillEvent],
    ) -> set[str]:
        latest_baseline = latest_topic_event_for_scope(
            self.event_store,
            topics.ACCOUNT_BASELINES,
            self.runtime_scope,
        )
        accepted_ids: set[str] = set()
        if latest_baseline is not None:
            fills = latest_baseline.payload.get("fills")
            if isinstance(fills, list):
                for fill in fills:
                    if isinstance(fill, dict):
                        fill_id = fill.get("fill_id")
                        if isinstance(fill_id, str) and fill_id:
                            accepted_ids.add(fill_id)

        if (
            exchange_snapshot is None
            or not exchange_snapshot.fills
            or len(exchange_snapshot.fills) < self.settings.okx_fill_fetch_limit
        ):
            return accepted_ids

        visible_fill_timestamps = [
            fill.fill_ts
            for fill in exchange_snapshot.fills
            if fill.fill_ts is not None
        ]
        if not visible_fill_timestamps:
            return accepted_ids
        oldest_visible_fill_ts = min(visible_fill_timestamps)
        for fill in local_fills:
            if fill.venue != "OKX":
                continue
            if fill.exchange_timestamp < oldest_visible_fill_ts:
                accepted_ids.add(fill.fill_id)
        return accepted_ids

    def _report_exists_for_portfolio_snapshot_ref(self, portfolio_snapshot_ref: str) -> bool:
        return any(
            report.portfolio_snapshot_ref == portfolio_snapshot_ref
            for report in self.reconciliation_repo.history_for_scope(scope=self.runtime_scope)
        )

    async def _persist_report(self, report: ReconciliationReport) -> None:
        self.reconciliation_repo.save_report(report)
        repaired_snapshot: PortfolioSnapshot | None = None
        if report.severity != "CLEAN":
            if self.metrics is not None:
                self.metrics.increment("reconciliation_mismatches")
            repaired_snapshot = self.repair_service.repair(report)
        await publish_model(
            bus=self.bus,
            topic=topics.RECONCILIATION_REPORTS,
            key="portfolio",
            payload_model=report,
            source_component="reconciliation_service",
        )
        if repaired_snapshot is not None:
            await publish_model(
                bus=self.bus,
                topic=topics.PORTFOLIO_SNAPSHOTS,
                key="portfolio",
                payload_model=repaired_snapshot,
                source_component="reconciliation_service",
            )
