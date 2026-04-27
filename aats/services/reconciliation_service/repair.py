from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Callable
from dataclasses import dataclass

from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.execution import FillEvent
from aats.schemas.exchange import AccountBaselineSnapshot, ExchangeAccountSnapshot
from aats.schemas.exit_execution import ExitExecutionIntent
from aats.schemas.operator import ProcessingFailureRecord
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.services.fill_ordering import fill_processing_sort_key
from aats.services.portfolio_service.snapshot_cache import PORTFOLIO_SNAPSHOT_CACHE_SOURCE_COMPONENT
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.bootstrap.telemetry import traced
from aats.services.reconciliation_service.comparator import StateComparator
from aats.services.reconciliation_service.fetcher import ExchangeStateFetcher
from aats.services.governance_engine.runtime_layers import RecoveryPolicy
from aats.services.recovery_control.reconciliation_classifier import RecoveryReconciliationClassifier
from aats.storage.base import EventStore
from aats.storage.base import ExecutionRepository, ReconciliationRepository
from aats.schemas.common import utc_now
from aats.services.execution_engine.exit_intent_aggregator import (
    augment_reconciliation_report_with_exit_execution,
    refresh_exit_execution_intents,
)
from aats.services.runtime_scope import (
    fills_for_scope,
    latest_baseline_for_scope,
    latest_snapshot_for_scope,
    latest_topic_event_for_scope,
    order_states_for_scope,
    runtime_state_scope,
)
from aats.storage.base import ExitExecutionRepository, PortfolioRepository


# ---------------------------------------------------------------------------
#  共用辅助函数：baseline-aware snapshot 重建
# ---------------------------------------------------------------------------


def rebuild_snapshot_from_baseline(
    *,
    reconstruction_service: PortfolioReconstructionService,
    runtime_scope,
    price_provider: Callable[[str], Decimal],
    baseline_snapshot: PortfolioSnapshot,
    fills: list[FillEvent],
) -> PortfolioSnapshot:
    """Replay only post-baseline fills on top of *baseline_snapshot*.

    This is the single authoritative implementation of baseline-aware
    snapshot reconstruction.  Both ``ReconciliationRepairService`` and
    ``ReconciliationService`` delegate here to avoid logic duplication.
    """
    state = PortfolioState(
        initial_usdt_balance=reconstruction_service.initial_usdt_balance,
        default_product_type=runtime_scope.product_type,
        default_margin_mode=runtime_scope.margin_mode,
    )
    state.load_portfolio_snapshot(baseline_snapshot)
    baseline_ts = baseline_snapshot.snapshot_ts
    for fill in sorted(fills, key=fill_processing_sort_key):
        if fill.ingestion_timestamp >= baseline_ts:
            state.apply_fill(fill)
    return reconstruction_service.snapshot_builder.build(
        state=state,
        price_provider=price_provider,
    )


@dataclass(slots=True)
class ReconciliationRepairService:
    portfolio_repo: PortfolioRepository | None = None
    execution_repo: ExecutionRepository | None = None
    exit_execution_repo: ExitExecutionRepository | None = None
    reconstruction_service: PortfolioReconstructionService | None = None
    price_provider: Callable[[str], Decimal] | None = None
    runtime_scope: object | None = None
    settings: AATSSettings | None = None

    def configure(
        self,
        *,
        settings: AATSSettings,
        portfolio_repo: PortfolioRepository,
        execution_repo: ExecutionRepository,
        exit_execution_repo: ExitExecutionRepository | None,
        reconstruction_service: PortfolioReconstructionService,
        price_provider: Callable[[str], Decimal],
        runtime_scope,
    ) -> None:
        self.settings = settings
        self.portfolio_repo = portfolio_repo
        self.execution_repo = execution_repo
        self.exit_execution_repo = exit_execution_repo
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
        scoped_fills = fills_for_scope(self.execution_repo, self.runtime_scope)
        if not scoped_fills:
            return None
        rebuilt_snapshot = self._rebuild_snapshot_baseline_aware(
            fills=scoped_fills,
        ).model_copy(
            update={
                "decision_id": report.decision_id,
                "snapshot_origin": "local_repair",
                "product_type": self.runtime_scope.product_type,
                "margin_mode": self.runtime_scope.margin_mode,
            }
        )
        latest_snapshot = latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope)
        if latest_snapshot is not None and latest_snapshot.model_dump(mode="json") == rebuilt_snapshot.model_dump(mode="json"):
            return None
        self.portfolio_repo.save_snapshot(rebuilt_snapshot)
        return rebuilt_snapshot

    def _rebuild_snapshot_baseline_aware(
        self,
        *,
        fills: list[FillEvent],
    ) -> PortfolioSnapshot:
        """Rebuild portfolio snapshot, respecting baseline when available.

        When bootstrap_portfolio_from_exchange is enabled and a baseline
        snapshot exists, only fills ingested *after* the baseline timestamp
        are replayed on top of the baseline state.  This prevents
        pre-baseline fills (whose net may be non-zero) from corrupting
        the reconstructed snapshot.
        """
        use_baseline = (
            self.settings is not None
            and getattr(self.settings, "bootstrap_portfolio_from_exchange", False)
        )
        if use_baseline:
            baseline_snapshot = latest_baseline_for_scope(
                self.portfolio_repo, self.runtime_scope,
            )
            if baseline_snapshot is not None:
                return rebuild_snapshot_from_baseline(
                    reconstruction_service=self.reconstruction_service,
                    runtime_scope=self.runtime_scope,
                    price_provider=self.price_provider,
                    baseline_snapshot=baseline_snapshot,
                    fills=fills,
                )
        # Fallback: full fill replay (no baseline available or not enabled)
        return self.reconstruction_service.rebuild_snapshot(
            fills=fills,
            price_provider=self.price_provider,
        )

    def refresh_exit_execution_truth(self) -> list[ExitExecutionIntent]:
        if (
            self.execution_repo is None
            or self.exit_execution_repo is None
            or self.settings is None
        ):
            return []
        return refresh_exit_execution_intents(
            execution_repo=self.execution_repo,
            exit_execution_repo=self.exit_execution_repo,
            settings=self.settings,
            scope=self.runtime_scope,
        )


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
        price_provider: Callable[[str], Decimal],
        exit_execution_repo: ExitExecutionRepository | None = None,
        bootstrap_portfolio_from_exchange: bool = False,
        recovery_policy: RecoveryPolicy | None = None,
        metrics: MetricsRegistry | None = None,
        reconciliation_classifier: RecoveryReconciliationClassifier | None = None,
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
        self.reconciliation_classifier = reconciliation_classifier
        self.runtime_scope = runtime_state_scope(settings)
        configure_comparator = getattr(self.comparator, "configure", None)
        if callable(configure_comparator):
            configure_comparator(settings=self.settings)
        configure = getattr(self.repair_service, "configure", None)
        if callable(configure):
            configure(
                settings=self.settings,
                portfolio_repo=self.portfolio_repo,
                execution_repo=self.execution_repo,
                exit_execution_repo=exit_execution_repo,
                reconstruction_service=self.reconstruction_service,
                price_provider=self.price_provider,
                runtime_scope=self.runtime_scope,
            )

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        if envelope.source_component == PORTFOLIO_SNAPSHOT_CACHE_SOURCE_COMPONENT:
            # cache-only broadcasts keep gateway hot state fresh for direct
            # save_snapshot() paths. They are not durable reconciliation inputs.
            return
        # _report_exists_for_portfolio_snapshot_ref 会扫 reconciliation_repo
        # history。_build_report 更重：里面会做 fetcher.fetch_snapshot（同步
        # 网络调用）、多次 DB 读以及重算 snapshot。两处都在 event loop 线程
        # 上跑显然不合理，全部丢线程池。
        exists = await asyncio.to_thread(
            self._report_exists_for_portfolio_snapshot_ref,
            envelope.event_id,
        )
        if exists:
            return
        snapshot = PortfolioSnapshot.model_validate(envelope.payload)
        report = await asyncio.to_thread(
            self._build_report,
            decision_id=snapshot.decision_id,
            portfolio_snapshot_ref=envelope.event_id,
            stored_snapshot=snapshot,
        )
        await self._persist_report(report)

    async def handle_processing_failure(self, message: dict) -> None:
        envelope = parse_envelope(message)
        failure = ProcessingFailureRecord.model_validate(envelope.payload)
        if failure.subsystem != "portfolio_service" or failure.stage != "portfolio_snapshot_persist":
            return
        if not failure.retriable:
            return
        await self.repair_missing_portfolio_snapshot(reason="processing_failure_repair")
        await self.validate_now(reason="processing_failure_repair")

    async def repair_missing_portfolio_snapshot(
        self,
        *,
        reason: str = "background_refresh",
    ) -> PortfolioSnapshot | None:
        fills = await asyncio.to_thread(fills_for_scope, self.execution_repo, self.runtime_scope)
        if not fills:
            return None

        latest_fill = max(fills, key=fill_processing_sort_key)
        latest_snapshot = await asyncio.to_thread(latest_snapshot_for_scope, self.portfolio_repo, self.runtime_scope)
        if latest_snapshot is not None:
            if (
                latest_snapshot.source_fill_id == latest_fill.fill_id
                and latest_snapshot.snapshot_ts >= latest_fill.ingestion_timestamp
            ):
                return None
            newer_fills = [
                fill
                for fill in fills
                if fill.ingestion_timestamp > latest_snapshot.snapshot_ts
            ]
            if not newer_fills and latest_snapshot.snapshot_ts >= latest_fill.ingestion_timestamp:
                return None

        repaired_snapshot = self._rebuild_snapshot_for_comparison(
            stored_snapshot=latest_snapshot,
            fills=fills,
        ).model_copy(
            update={
                "decision_id": latest_fill.decision_id,
                "source_intent_id": latest_fill.intent_id,
                "source_fill_id": latest_fill.fill_id,
                "snapshot_origin": "recovery_rebuild",
                "product_type": self.runtime_scope.product_type,
                "margin_mode": self.runtime_scope.margin_mode,
            }
        )
        try:
            await asyncio.to_thread(self.portfolio_repo.save_snapshot, repaired_snapshot)
            if self.metrics is not None:
                self.metrics.increment("portfolio_snapshot_repairs")
            await publish_model(
                bus=self.bus,
                topic=topics.PORTFOLIO_SNAPSHOTS,
                key="portfolio",
                payload_model=repaired_snapshot,
                source_component="reconciliation_service",
            )
        except Exception as exc:
            await self._emit_snapshot_repair_failure(
                latest_fill=latest_fill,
                stage="portfolio_snapshot_repair",
                message=f"{reason}: {exc}",
            )
            raise
        return repaired_snapshot

    @traced("reconciliation.validate_now")
    async def validate_now(self, *, reason: str = "operator_validate") -> ReconciliationReport:
        latest_snapshot = await asyncio.to_thread(latest_snapshot_for_scope, self.portfolio_repo, self.runtime_scope)
        latest_snapshot_event = await asyncio.to_thread(
            latest_topic_event_for_scope,
            self.event_store,
            topics.PORTFOLIO_SNAPSHOTS,
            self.runtime_scope,
        )
        scoped_order_states = await asyncio.to_thread(order_states_for_scope, self.execution_repo, self.runtime_scope)
        if latest_snapshot is None:
            latest_snapshot = self.reconstruction_service.rebuild_snapshot(
                fills=await asyncio.to_thread(fills_for_scope, self.execution_repo, self.runtime_scope),
                price_provider=self.price_provider,
            ).model_copy(
                update={
                    "snapshot_ts": utc_now(),
                    "decision_id": (
                        scoped_order_states[-1].decision_id
                        if scoped_order_states
                        else None
                    ),
                    "snapshot_origin": "manual_rebuild",
                    "product_type": self.runtime_scope.product_type,
                    "margin_mode": self.runtime_scope.margin_mode,
                }
            )
        # validate_now 来自 HTTP handler / operator 命令，_build_report 内部
        # 会打一次 fetcher.fetch_snapshot（同步网络）+ 多次 DB 读。不丢线程
        # 池的话 operator 点击 "立即校验" 会直接卡住 event loop。
        #
        # 主动发起的对账（background_refresh、operator_validate、
        # processing_failure_repair、operator_rebaseline、resume_check 等）
        # 不归属任何具体决策。若挂 latest_snapshot.decision_id，会让"最近一条
        # 成交所在的决策"的 decision_audit_records 每 60s 被 upsert 一次，
        # audit revisions 无限膨胀，且在 recent_decisions 排序里永远冒泡到
        # 顶端。事件驱动的对账（handle_portfolio_snapshot）仍保留归属。
        report = await asyncio.to_thread(
            self._build_report,
            decision_id=None,
            portfolio_snapshot_ref=(
                latest_snapshot_event.event_id
                if latest_snapshot_event is not None
                else f"manual_portfolio_snapshot:{reason}:{latest_snapshot.snapshot_ts.isoformat()}"
            ),
            stored_snapshot=latest_snapshot,
        )
        report = await self._persist_report(report)
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
        baseline_snapshot = latest_baseline_for_scope(
            self.portfolio_repo, self.runtime_scope,
        )
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
            exchange_bills_summary=self._exchange_bills_summary(
                account_baseline=self._latest_account_baseline(),
            ),
        )
        latest_generation = None
        latest_generation_getter = getattr(
            self.reconciliation_repo,
            "latest_baseline_generation_for_scope",
            None,
        )
        if callable(latest_generation_getter):
            latest_generation = latest_generation_getter(scope=self.runtime_scope)
        latest_ack_watermark = None
        latest_ack_getter = getattr(
            self.reconciliation_repo,
            "latest_exchange_ack_watermark_for_scope",
            None,
        )
        if callable(latest_ack_getter):
            latest_ack_watermark = latest_ack_getter(scope=self.runtime_scope)
        if latest_generation is not None or latest_ack_watermark is not None:
            report = report.model_copy(
                update={
                    "baseline_generation_id": (
                        None if latest_generation is None else latest_generation.generation_id
                    ),
                    "exchange_ack_watermark_id": (
                        None if latest_ack_watermark is None else latest_ack_watermark.watermark_id
                    ),
                }
            )
        if self.reconciliation_classifier is not None:
            report = self.reconciliation_classifier.annotate(report)
        return report

    def _exchange_bills_summary(
        self,
        *,
        account_baseline: AccountBaselineSnapshot | None = None,
    ) -> dict[str, object]:
        account_service = getattr(self.fetcher, "account_service", None)
        summary_getter = getattr(account_service, "recent_bills_summary", None)
        if not callable(summary_getter):
            return {}
        acknowledged_watermark = None
        latest_ack_watermark_for_scope = getattr(
            self.reconciliation_repo,
            "latest_exchange_ack_watermark_for_scope",
            None,
        )
        if callable(latest_ack_watermark_for_scope):
            acknowledged_watermark = latest_ack_watermark_for_scope(scope=self.runtime_scope)
        if account_baseline is None:
            account_baseline = self._latest_account_baseline()
        summary_since_getter = getattr(account_service, "recent_bills_summary_since", None)
        if (
            acknowledged_watermark is not None
            and callable(summary_since_getter)
            and acknowledged_watermark.latest_bill_ts is not None
        ):
            summary = summary_since_getter(since_ts=acknowledged_watermark.latest_bill_ts)
        elif (
            account_baseline is not None
            and account_baseline.baseline_kind == "operator_rebaseline"
            and callable(summary_since_getter)
        ):
            summary = summary_since_getter(since_ts=account_baseline.imported_at)
        else:
            summary = summary_getter()
            latest_bill_ts = summary.get("latest_bill_ts") if isinstance(summary, dict) else None
            if (
                account_baseline is not None
                and account_baseline.baseline_kind == "operator_rebaseline"
                and isinstance(latest_bill_ts, datetime)
                and latest_bill_ts <= account_baseline.imported_at
            ):
                return {
                    "available": False,
                    "count": 0,
                    "latest_bill_id": None,
                    "latest_bill_ts": None,
                    "currencies": [],
                    "top_categories": [],
                    "funding_fee_summary": {
                        "available": False,
                        "count": 0,
                        "latest_bill_ts": None,
                        "currencies": [],
                        "net_total_by_currency": {},
                        "absolute_total_by_currency": {},
                        "current_position_notional_usd": None,
                        "funding_fee_bps_proxy": None,
                    },
                    "last_error": summary.get("last_error") if isinstance(summary, dict) else None,
                }
        return summary if isinstance(summary, dict) else {}

    def _latest_account_baseline(self) -> AccountBaselineSnapshot | None:
        latest_baseline = latest_topic_event_for_scope(
            self.event_store,
            topics.ACCOUNT_BASELINES,
            self.runtime_scope,
        )
        if latest_baseline is None:
            return None
        try:
            return AccountBaselineSnapshot.model_validate(latest_baseline.payload)
        except Exception:
            return None

    def _rebuild_snapshot_for_comparison(
        self,
        *,
        stored_snapshot: PortfolioSnapshot | None,
        fills: list[FillEvent],
    ) -> PortfolioSnapshot:
        if self.settings.portfolio_ledger_truth_enabled and stored_snapshot is not None:
            return stored_snapshot

        def full_replay_snapshot() -> PortfolioSnapshot:
            return self.reconstruction_service.rebuild_snapshot(
                fills=fills,
                price_provider=self.price_provider,
            ).model_copy(
                update={
                    "snapshot_origin": "manual_rebuild",
                    "product_type": self.runtime_scope.product_type,
                    "margin_mode": self.runtime_scope.margin_mode,
                }
            )

        if not self.bootstrap_portfolio_from_exchange:
            return full_replay_snapshot()

        baseline_snapshot = latest_baseline_for_scope(
            self.portfolio_repo, self.runtime_scope,
        )
        if baseline_snapshot is None:
            return full_replay_snapshot()

        return rebuild_snapshot_from_baseline(
            reconstruction_service=self.reconstruction_service,
            runtime_scope=self.runtime_scope,
            price_provider=self.price_provider,
            baseline_snapshot=baseline_snapshot,
            fills=fills,
        )

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
            if fill.exchange_timestamp <= oldest_visible_fill_ts:
                accepted_ids.add(fill.fill_id)
        return accepted_ids

    def _report_exists_for_portfolio_snapshot_ref(self, portfolio_snapshot_ref: str) -> bool:
        return any(
            report.portfolio_snapshot_ref == portfolio_snapshot_ref
            for report in self.reconciliation_repo.history_for_scope(scope=self.runtime_scope)
        )

    async def _persist_report(self, report: ReconciliationReport) -> ReconciliationReport:
        try:
            # 原实现把 refresh_exit_execution_truth / save_report / repair 三个
            # 同步 DB 阶段全压在 event loop 线程里——重度失配时 repair 还要
            # 重建整个 snapshot，明显阻塞主协程。拆成 sync helper 后整块丢到
            # 线程池，publish_model 留在协程里异步走。
            report_to_save, repaired_snapshot = await asyncio.to_thread(
                self._persist_report_sync,
                report,
            )
            await publish_model(
                bus=self.bus,
                topic=topics.RECONCILIATION_REPORTS,
                key="portfolio",
                payload_model=report_to_save,
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
            return report_to_save
        except Exception as exc:
            await self._emit_processing_failure(report=report, stage="reconciliation_persist", message=str(exc))
            raise

    def _persist_report_sync(
        self,
        report: ReconciliationReport,
    ) -> tuple[ReconciliationReport, PortfolioSnapshot | None]:
        refresh_exit_execution_truth = getattr(self.repair_service, "refresh_exit_execution_truth", None)
        refreshed_exit_execution: list[ExitExecutionIntent] = []
        if callable(refresh_exit_execution_truth):
            refreshed_exit_execution = list(refresh_exit_execution_truth())
        report_to_save = augment_reconciliation_report_with_exit_execution(
            report=report,
            parent_intents=refreshed_exit_execution,
        )
        if self.reconciliation_classifier is not None:
            report_to_save = self.reconciliation_classifier.annotate(report_to_save)
        self.reconciliation_repo.save_report(report_to_save)
        repaired_snapshot: PortfolioSnapshot | None = None
        if report_to_save.severity != "CLEAN":
            if self.metrics is not None:
                self.metrics.increment("reconciliation_mismatches")
            repaired_snapshot = self.repair_service.repair(report_to_save)
        return report_to_save, repaired_snapshot

    async def _emit_processing_failure(
        self,
        *,
        report: ReconciliationReport,
        stage: str,
        message: str,
    ) -> None:
        if self.metrics is not None:
            self.metrics.increment("processing_failures")
        try:
            await publish_model(
                bus=self.bus,
                topic=topics.PROCESSING_FAILURES,
                key="portfolio",
                payload_model=ProcessingFailureRecord(
                    subsystem="reconciliation_service",
                    stage=stage,
                    severity="error",
                    message=message,
                    decision_id=report.decision_id,
                    reconciliation_id=report.reconciliation_id,
                    product_type=report.product_type,
                    margin_mode=report.margin_mode,
                    retriable=True,
                    observed_at=utc_now(),
                ),
                source_component="reconciliation_service",
            )
        except Exception:
            pass

    async def _emit_snapshot_repair_failure(
        self,
        *,
        latest_fill: FillEvent,
        stage: str,
        message: str,
    ) -> None:
        if self.metrics is not None:
            self.metrics.increment("processing_failures")
        try:
            await publish_model(
                bus=self.bus,
                topic=topics.PROCESSING_FAILURES,
                key=latest_fill.symbol,
                payload_model=ProcessingFailureRecord(
                    subsystem="reconciliation_service",
                    stage=stage,
                    severity="error",
                    message=message,
                    decision_id=latest_fill.decision_id,
                    intent_id=latest_fill.intent_id,
                    order_id=latest_fill.client_order_id,
                    fill_id=latest_fill.fill_id,
                    symbol=latest_fill.symbol,
                    product_type=latest_fill.product_type,
                    margin_mode=latest_fill.margin_mode,
                    retriable=True,
                    observed_at=utc_now(),
                ),
                source_component="reconciliation_service",
            )
        except Exception:
            pass
