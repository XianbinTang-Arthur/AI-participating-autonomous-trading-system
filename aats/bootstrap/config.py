from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope, parse_payload, publish_model
from aats.schemas.decision import DecisionOutcome, PositionTarget
from aats.services.ai_service.inference import AIInferenceService
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AssessmentValidator
from aats.services.decision_engine.audit import DecisionAuditService
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.decision_engine.context_builder import DecisionContextBuilder
from aats.services.decision_engine.orchestrator import DecisionOrchestrator
from aats.services.decision_engine.target_position import TargetPositionEngine
from aats.services.decision_engine.trigger import DecisionCycleTrigger
from aats.services.decision_engine.trigger_policy import DecisionTriggerPolicy
from aats.services.execution_engine.exchange_adapter import ExchangeAdapter
from aats.services.execution_engine.okx_account import OKXAccountService
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter
from aats.services.execution_engine.okx_private_websocket import OKXPrivateWebSocketClient
from aats.services.execution_engine.baseline_import import AccountBaselineImportService
from aats.services.execution_engine.obligations import ExecutionObligationService
from aats.services.execution_engine.recovery import ExecutionRecoveryService
from aats.services.execution_engine.okx_rest import OKXRESTClient
from aats.services.execution_control.command_service import ExecutionCommandProcessor
from aats.services.execution_control.monitor import Phase1ShadowMonitor
from aats.services.execution_control.order_service import ExecutionOrderService
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.execution_control.subsystem import Phase1ShadowSubsystem
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.execution_engine.planner import ExecutionPlanner
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.feature_engine.calculator import FeatureCalculator, FeatureEngine
from aats.services.governance_engine.health import SystemHealthService
from aats.services.governance_engine.derivatives_live_guard import DerivativesLiveGuardService
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.services.governance_engine.policy import PolicyEngine
from aats.services.governance_engine.risk import RiskEngine
from aats.services.governance_engine.runtime_layers import (
    EnvironmentCapabilities,
    PolicyProfile,
    RecoveryPolicy,
    RuntimeLayering,
    RuntimeProfile,
    resolve_runtime_layering,
)
from aats.services.governance_engine.trial_guard import ForwardTrialGuardService
from aats.services.market_gateway.gateway import MarketDataGateway
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient
from aats.services.market_gateway.publisher import MarketSnapshotPublisher
from aats.services.operator.accounts import enabled_admin_count
from aats.services.ledger.posting import Phase1LedgerMirrorService
from aats.services.ledger.funding_fee_sync import LedgerFundingFeeSyncService
from aats.services.ledger.lot_projection import LotBasedProjectionBuilder
from aats.services.ledger.persistent_lot_book import PersistentLotBookService
from aats.services.ledger.settlement_posting import LedgerSettlementPostingService
from aats.services.operator.runtime_profiles import runtime_profile_resolution
from aats.services.operator.strategy_profiles import StrategyProfileControlService, seed_strategy_profiles
from aats.services.projections.ledger_portfolio import LedgerBackedPortfolioService
from aats.services.recovery_control import ExecutionLedgerRecoveryService, RecoveryReconciliationClassifier
from aats.services.runtime_scope import latest_matching_snapshot, runtime_state_scope, scoped_portfolio_event
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioBalanceDelta
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator
from aats.services.portfolio_service.positions import PortfolioService, PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder
from aats.services.reconciliation_service.comparator import StateComparator
from aats.services.reconciliation_service.fetcher import ExchangeStateFetcher
from aats.services.reconciliation_service.repair import ReconciliationRepairService, ReconciliationService
from aats.storage.audit_repo import InMemoryAuditRepository
from aats.storage.audit_repo_postgres import PostgresAuditRepository
from aats.storage.base import (
    AuditRepository,
    EventStore,
    FillOutcomeRepository,
    FundingFeeRepository,
    ExecutionRepository,
    ExecutionObligationRepository,
    OperatorUserRepository,
    PortfolioRepository,
    ReconciliationRepository,
    RuntimeProfileRepository,
)
from aats.storage.event_store import InMemoryEventStore
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.fill_outcome_repo import InMemoryFillOutcomeRepository
from aats.storage.fill_outcome_repo_postgres import PostgresFillOutcomeRepository
from aats.storage.funding_fee_repo import InMemoryFundingFeeRepository
from aats.storage.funding_fee_repo_postgres import PostgresFundingFeeRepository
from aats.storage.command_outbox_repo_postgres import PostgresCommandOutboxRepositoryV2
from aats.storage.execution_command_repo_postgres import PostgresExecutionCommandRepository
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.inbox_repo_postgres import PostgresExternalInboxRepository
from aats.storage.ledger_repo_postgres import (
    PostgresLedgerAccountRepository,
    PostgresLedgerEntryRepository,
    PostgresLedgerJournalRepository,
    PostgresSettlementRepository,
)
from aats.storage.lot_repo_postgres import PostgresLotEventRepository, PostgresPositionLotRepository
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository
from aats.storage.operator_repo import InMemoryOperatorUserRepository
from aats.storage.operator_repo_postgres import PostgresOperatorUserRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.portfolio_repo_postgres import PostgresPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository
from aats.storage.reconciliation_repo_postgres import PostgresReconciliationRepository
from aats.storage.reservation_repo_postgres import PostgresReservationRepository
from aats.storage.runtime_profile_repo import InMemoryRuntimeProfileRepository
from aats.storage.runtime_profile_repo_postgres import PostgresRuntimeProfileRepository
from aats.storage.strategy_profile_repo import InMemoryStrategyProfileRepository
from aats.storage.strategy_profile_repo_postgres import PostgresStrategyProfileRepository
from aats.storage.session import DatabaseRuntime, create_database_runtime, create_schema, validate_runtime_schema
from aats.schemas.system import RecoveryStatus
from aats.schemas.common import utc_now
from aats.schemas.operator import ExecutionErrorSummary, ProcessingFailureRecord
from aats.schemas.runtime_profiles import RuntimeProfileResolution
from aats.storage.base import StrategyProfileRepository


SPOT_GUARDED_CONFIG_PROFILES = frozenset({"guarded_spot_dry_run", "guarded_spot_enabled"})
DERIVATIVES_GUARDED_CONFIG_PROFILES = frozenset(
    {"guarded_derivatives_dry_run", "guarded_derivatives_enabled"}
)


def load_yaml_config(environment: str, profile: str, config_dir: str | Path = "configs") -> dict[str, Any]:
    config_path = Path(config_dir)
    merged: dict[str, Any] = {}
    profile_aliases = {
        "guarded_simulated_submit_dry_run": "guarded_simulated_dry_run",
        "guarded_simulated_submit_enabled": "guarded_simulated_enabled",
    }
    candidates = (
        config_path / "base.yaml",
        config_path / f"{environment}.yaml",
        config_path / f"{profile}.yaml",
        config_path / f"{profile_aliases.get(profile, profile)}.yaml",
    )
    for candidate in candidates:
        if candidate.exists():
            merged.update(yaml.safe_load(candidate.read_text(encoding="utf-8")) or {})
    return merged


def resilient_subscription_handler(
    *,
    topic: str,
    name: str,
    handler,
    subscription_class: str = "observer",
    raise_on_error: bool = False,
):
    logger = get_logger("aats.event_bus")

    async def wrapped(message: dict) -> None:
        try:
            await handler(message)
        except Exception as exc:
            payload = message.get("payload") if isinstance(message, dict) else None
            event_id = payload.get("event_id") if isinstance(payload, dict) else None
            log_event(
                logger,
                "noncritical_subscription_failed",
                level="error",
                topic=topic,
                handler=name,
                subscription_class=subscription_class,
                key=message.get("key") if isinstance(message, dict) else None,
                event_id=event_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            if raise_on_error:
                raise

    return wrapped


def load_settings() -> AATSSettings:
    discovered = AATSSettings()
    yaml_values = load_yaml_config(discovered.environment, discovered.config_profile)
    explicit_overrides = {
        field_name: getattr(discovered, field_name)
        for field_name in discovered.model_fields_set
    }
    return AATSSettings.model_validate({**yaml_values, **explicit_overrides})


@dataclass(slots=True)
class StorageBackends:
    event_store: EventStore
    audit_repo: AuditRepository
    portfolio_repo: PortfolioRepository
    fill_outcome_repo: FillOutcomeRepository
    execution_repo: ExecutionRepository
    obligation_repo: ExecutionObligationRepository
    reconciliation_repo: ReconciliationRepository
    operator_repo: OperatorUserRepository
    runtime_profile_repo: RuntimeProfileRepository
    strategy_profile_repo: StrategyProfileRepository
    execution_order_repo: PostgresExecutionOrderRepository | None = None
    execution_order_history_repo: PostgresExecutionOrderHistoryRepository | None = None
    execution_command_repo: PostgresExecutionCommandRepository | None = None
    execution_fill_repo_v2: PostgresExecutionFillRepositoryV2 | None = None
    reservation_repo_v2: PostgresReservationRepository | None = None
    ledger_account_repo: PostgresLedgerAccountRepository | None = None
    ledger_journal_repo: PostgresLedgerJournalRepository | None = None
    ledger_entry_repo: PostgresLedgerEntryRepository | None = None
    settlement_repo: PostgresSettlementRepository | None = None
    position_lot_repo: PostgresPositionLotRepository | None = None
    lot_event_repo: PostgresLotEventRepository | None = None
    external_inbox_repo: PostgresExternalInboxRepository | None = None
    command_outbox_repo_v2: PostgresCommandOutboxRepositoryV2 | None = None
    phase1_execution_shadow_service: Phase1ExecutionShadowService | None = None
    phase1_ledger_mirror_service: Phase1LedgerMirrorService | None = None
    phase1_shadow: Phase1ShadowSubsystem | None = None
    outbox_repo: PostgresOutboxRepository | None = None
    funding_fee_repo: FundingFeeRepository | None = None
    database_runtime: DatabaseRuntime | None = None


@dataclass(frozen=True, slots=True)
class ObserverSubscriptionSpec:
    topic: str
    name: str
    handler: Any


@dataclass(slots=True)
class ApplicationRuntime:
    started_at: Any
    settings: AATSSettings
    runtime_profile_resolution: RuntimeProfileResolution
    runtime_layering: RuntimeLayering
    runtime_profile: RuntimeProfile
    environment_capabilities: EnvironmentCapabilities
    policy_profile: PolicyProfile
    recovery_policy: RecoveryPolicy
    bus: InMemoryEventBus
    event_store: EventStore
    market_gateway: MarketDataGateway
    feature_engine: FeatureEngine
    ai_service: AIInferenceService
    decision_engine: DecisionOrchestrator
    decision_trigger: DecisionCycleTrigger
    decision_trigger_policy: DecisionTriggerPolicy
    execution_planner: ExecutionPlanner
    execution_adapter: ExchangeAdapter
    order_manager: OrderManager
    portfolio_service: PortfolioService
    reconciliation_service: ReconciliationService
    fee_resolver: EffectiveFeeResolver
    policy_engine: PolicyEngine
    risk_engine: RiskEngine
    kill_switch: KillSwitch
    mode_controller: RuntimeModeController
    health_service: SystemHealthService
    account_service: OKXAccountService
    metrics: MetricsRegistry
    audit_repo: AuditRepository
    portfolio_repo: PortfolioRepository
    fill_outcome_repo: FillOutcomeRepository
    execution_repo: ExecutionRepository
    obligation_repo: ExecutionObligationRepository
    reconciliation_repo: ReconciliationRepository
    operator_repo: OperatorUserRepository
    runtime_profile_repo: RuntimeProfileRepository
    strategy_profile_repo: StrategyProfileRepository
    recovery_status: RecoveryStatus
    execution_order_repo: PostgresExecutionOrderRepository | None = None
    execution_order_history_repo: PostgresExecutionOrderHistoryRepository | None = None
    execution_command_repo: PostgresExecutionCommandRepository | None = None
    execution_fill_repo_v2: PostgresExecutionFillRepositoryV2 | None = None
    reservation_repo_v2: PostgresReservationRepository | None = None
    ledger_account_repo: PostgresLedgerAccountRepository | None = None
    ledger_journal_repo: PostgresLedgerJournalRepository | None = None
    ledger_entry_repo: PostgresLedgerEntryRepository | None = None
    settlement_repo: PostgresSettlementRepository | None = None
    position_lot_repo: PostgresPositionLotRepository | None = None
    lot_event_repo: PostgresLotEventRepository | None = None
    external_inbox_repo: PostgresExternalInboxRepository | None = None
    command_outbox_repo_v2: PostgresCommandOutboxRepositoryV2 | None = None
    phase1_execution_shadow_service: Phase1ExecutionShadowService | None = None
    phase1_ledger_mirror_service: Phase1LedgerMirrorService | None = None
    phase1_shadow_monitor: Phase1ShadowMonitor | None = None
    phase1_shadow: Phase1ShadowSubsystem | None = None
    execution_order_service: ExecutionOrderService | None = None
    execution_command_processor: ExecutionCommandProcessor | None = None
    phase1_shadow_alert_state: str | None = None
    trial_guard_service: ForwardTrialGuardService | None = None
    derivatives_live_guard_service: DerivativesLiveGuardService | None = None
    replay_validation_history: list[dict[str, Any]] = field(default_factory=list)
    database_runtime: DatabaseRuntime | None = None
    background_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    execution_outbox_publisher: PostgresExecutionOutboxPublisher | None = None
    funding_fee_repo: FundingFeeRepository | None = None
    funding_fee_sync_service: LedgerFundingFeeSyncService | None = None
    logger: Any = field(default_factory=lambda: get_logger("aats.runtime"))

    async def start_background_tasks(self) -> None:
        if self.settings.market_data_backend == "okx":
            await self.market_gateway.start()
        if self.environment_capabilities.account_state_source_kind == "exchange":
            self.background_tasks.append(
                asyncio.create_task(self.account_service.run_private_ws_forever(), name="aats_okx_private_account_ws")
            )
        self.background_tasks.append(
            asyncio.create_task(self._refresh_reconciliation_loop(), name="aats_reconciliation_refresh")
        )
        if self.environment_capabilities.account_state_source_kind == "exchange":
            self.background_tasks.append(
                asyncio.create_task(self._refresh_account_loop(), name="aats_okx_account_refresh")
            )
        if self.environment_capabilities.execution_adapter_kind == "okx":
            self.background_tasks.append(
                asyncio.create_task(self._sync_execution_loop(), name="aats_okx_execution_sync")
            )
        if self.execution_outbox_publisher is not None:
            self.background_tasks.append(
                asyncio.create_task(self._flush_execution_outbox_loop(), name="aats_execution_outbox_flush")
            )
        if self.execution_command_processor is not None:
            self.background_tasks.append(
                asyncio.create_task(self._process_execution_commands_loop(), name="aats_execution_command_flow")
            )
        if self.phase1_shadow_monitor is not None:
            self.background_tasks.append(
                asyncio.create_task(self._monitor_phase1_shadow_loop(), name="aats_phase1_shadow_monitor")
            )
        if self.trial_guard_service is not None:
            self.background_tasks.append(
                asyncio.create_task(self._monitor_trial_guard_loop(), name="aats_trial_guard_monitor")
            )

    async def stop_background_tasks(self) -> None:
        await self.account_service.stop_private_ws()
        for task in self.background_tasks:
            task.cancel()
        for task in self.background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.background_tasks.clear()
        await self.market_gateway.stop()
        close_rest_client = getattr(self.account_service.client, "aclose", None)
        if callable(close_rest_client):
            await close_rest_client()
        if self.database_runtime is not None:
            self.database_runtime.dispose()

    async def _refresh_account_loop(self) -> None:
        while True:
            try:
                await self.account_service.refresh()
                await self._sync_funding_fees_after_refresh()
                self._evaluate_derivatives_live_guard_after_refresh()
            except Exception as exc:
                self._record_background_failure(subsystem="account_refresh", exc=exc)
            await asyncio.sleep(self.settings.okx_account_refresh_interval_seconds)

    def _evaluate_derivatives_live_guard_after_refresh(self) -> None:
        if self.derivatives_live_guard_service is None:
            return
        self.derivatives_live_guard_service.evaluate_now()

    async def _sync_funding_fees_after_refresh(self) -> None:
        if self.funding_fee_sync_service is None:
            return
        result = self.funding_fee_sync_service.sync_recent_bills(
            rows=self.account_service.latest_recent_bills(),
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
        )
        if result.posted_count <= 0:
            return
        if hasattr(self.portfolio_service, "bootstrap_snapshot"):
            await self.portfolio_service.bootstrap_snapshot(snapshot_origin="local_repair")

    async def _sync_execution_loop(self) -> None:
        while True:
            try:
                await self.order_manager.sync_exchange_state()
            except Exception as exc:
                self._record_background_failure(subsystem="execution_sync", exc=exc)
            await asyncio.sleep(self.settings.okx_execution_sync_interval_seconds)

    async def _refresh_reconciliation_loop(self) -> None:
        interval_seconds = max(
            0.5,
            min(self.settings.reconciliation_stale_after_seconds / 2.0, 60.0),
        )
        while True:
            try:
                await self.reconciliation_service.repair_missing_portfolio_snapshot(reason="background_refresh")
                await self.reconciliation_service.validate_now(reason="background_refresh")
            except Exception as exc:
                self._record_background_failure(subsystem="reconciliation_refresh", exc=exc)
            await asyncio.sleep(interval_seconds)

    async def _flush_execution_outbox_loop(self) -> None:
        while True:
            try:
                if self.execution_outbox_publisher is not None:
                    await self.execution_outbox_publisher.flush_pending()
            except Exception as exc:
                self._record_background_failure(subsystem="execution_outbox_flush", exc=exc)
            await asyncio.sleep(1.0)

    async def _process_execution_commands_loop(self) -> None:
        interval_seconds = max(0.1, float(self.settings.execution_command_poll_interval_seconds))
        while True:
            try:
                if self.execution_command_processor is not None:
                    await self.execution_command_processor.process_pending()
            except Exception as exc:
                self._record_background_failure(subsystem="execution_command_flow", exc=exc)
            await asyncio.sleep(interval_seconds)

    async def _monitor_phase1_shadow_loop(self) -> None:
        interval_seconds = max(
            1.0,
            min(self.settings.reconciliation_stale_after_seconds / 4.0, 5.0),
        )
        while True:
            try:
                self._record_phase1_shadow_state()
            except Exception as exc:
                self._record_background_failure(subsystem="phase1_shadow_monitor", exc=exc)
            await asyncio.sleep(interval_seconds)

    async def _monitor_trial_guard_loop(self) -> None:
        interval_seconds = max(1.0, float(self.settings.trial_guard_poll_interval_seconds))
        while True:
            try:
                if self.trial_guard_service is not None:
                    self.trial_guard_service.evaluate_now()
            except Exception as exc:
                self._record_background_failure(subsystem="trial_guard_monitor", exc=exc)
            await asyncio.sleep(interval_seconds)

    def _record_background_failure(self, *, subsystem: str, exc: Exception) -> None:
        message = f"{subsystem}_failed: {exc}"
        log_event(
            self.logger,
            "background_loop_failed",
            level="error",
            subsystem=subsystem,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        latest = self.event_store.latest(topics.EXECUTION_ERROR_SUMMARIES)
        if latest is not None:
            payload = latest.payload
            if payload.get("subsystem") == subsystem and payload.get("message") == message:
                return
        latest_processing_failure = self.event_store.latest(topics.PROCESSING_FAILURES, key=subsystem)
        self.event_store.append(
            build_envelope(
                topic=topics.EXECUTION_ERROR_SUMMARIES,
                key=subsystem,
                payload_model=ExecutionErrorSummary(
                    subsystem=subsystem,
                    severity="error",
                    message=message,
                    observed_at=utc_now(),
                ),
                source_component="runtime",
            )
        )
        if latest_processing_failure is not None:
            payload = latest_processing_failure.payload
            if payload.get("subsystem") == subsystem and payload.get("message") == message:
                return
        self.event_store.append(
            build_envelope(
                topic=topics.PROCESSING_FAILURES,
                key=subsystem,
                payload_model=ProcessingFailureRecord(
                    subsystem=subsystem,
                    stage="background_loop",
                    severity="error",
                    message=message,
                    retriable=True,
                    observed_at=utc_now(),
                    details={"error_type": type(exc).__name__},
                ),
                source_component="runtime",
            )
        )

    def _record_phase1_shadow_state(self) -> None:
        if self.phase1_shadow_monitor is not None:
            snapshot = self.phase1_shadow_monitor.snapshot()
        elif self.phase1_shadow is not None:
            snapshot = self.phase1_shadow.snapshot()
        else:
            return
        status = str(snapshot.get("status") or "idle")
        previous = self.phase1_shadow_alert_state
        if status == previous:
            return
        self.phase1_shadow_alert_state = status
        if status not in {"degraded", "lagging"}:
            if previous in {"degraded", "lagging"}:
                self.metrics.increment("phase1_shadow_recoveries")
                self.event_store.append(
                    build_envelope(
                        topic=topics.EXECUTION_ERROR_SUMMARIES,
                        key="phase1_shadow",
                        payload_model=ExecutionErrorSummary(
                            subsystem="phase1_shadow",
                            severity="warning",
                            message=f"phase1_shadow_recovered:{snapshot.get('summary')}",
                            observed_at=utc_now(),
                        ),
                        source_component="runtime",
                    )
                )
            return

        severity = "error" if status == "degraded" else "warning"
        self.metrics.increment("phase1_shadow_alerts")
        self.event_store.append(
            build_envelope(
                topic=topics.EXECUTION_ERROR_SUMMARIES,
                key="phase1_shadow",
                payload_model=ExecutionErrorSummary(
                    subsystem="phase1_shadow",
                    severity=severity,
                    message=str(snapshot.get("summary") or f"phase1_shadow_{status}"),
                    observed_at=utc_now(),
                ),
                source_component="runtime",
            )
        )
        self.event_store.append(
            build_envelope(
                topic=topics.PROCESSING_FAILURES,
                key="phase1_shadow",
                payload_model=ProcessingFailureRecord(
                    subsystem="phase1_shadow",
                    stage=f"shadow_{status}",
                    severity=severity,
                    message=str(snapshot.get("summary") or f"phase1_shadow_{status}"),
                    retriable=True,
                    observed_at=utc_now(),
                    details={
                        "status": status,
                        "lag": snapshot.get("lag"),
                        "execution_shadow": snapshot.get("execution_shadow"),
                        "ledger_shadow": snapshot.get("ledger_shadow"),
                    },
                ),
                source_component="runtime",
            )
        )


def _validate_runtime_settings(settings: AATSSettings, runtime_layering: RuntimeLayering) -> None:
    _validate_startup_profile_settings(settings, runtime_layering)
    if (
        runtime_layering.environment_capabilities.persistent_storage_required
        and runtime_layering.environment_capabilities.exchange_submission_enabled
        and settings.storage_mode == "memory"
    ):
        raise ValueError("guarded_simulated_submit_requires_persistent_storage")
    if settings.portfolio_ledger_truth_enabled and settings.storage_mode == "memory":
        raise ValueError("portfolio_ledger_truth_requires_persistent_storage")
    if settings.recovery_reconciliation_execution_ledger_enabled and settings.storage_mode == "memory":
        raise ValueError("recovery_reconciliation_execution_ledger_requires_persistent_storage")
    if settings.recovery_reconciliation_execution_ledger_enabled and not settings.portfolio_ledger_truth_enabled:
        raise ValueError("recovery_reconciliation_execution_ledger_requires_portfolio_ledger_truth")
    if settings.operator_control_plane_execution_ledger_enabled and settings.storage_mode == "memory":
        raise ValueError("operator_control_plane_execution_ledger_requires_persistent_storage")
    if (
        settings.operator_control_plane_execution_ledger_enabled
        and not settings.recovery_reconciliation_execution_ledger_enabled
    ):
        raise ValueError("operator_control_plane_execution_ledger_requires_phase4_recovery")
    if settings.operator_control_plane_execution_ledger_enabled and not settings.execution_command_flow_enabled:
        raise ValueError("operator_control_plane_execution_ledger_requires_execution_command_flow")
    if settings.operator_control_plane_execution_ledger_enabled and settings.operator_unsafe_write_without_auth:
        raise ValueError("operator_control_plane_execution_ledger_disallows_unsafe_write_without_auth")
    if (
        settings.operator_control_plane_execution_ledger_enabled
        and runtime_layering.environment_capabilities.exchange_coupled
        and not settings.operator_auth_enabled
    ):
        raise ValueError("operator_control_plane_execution_ledger_requires_operator_auth")
    if settings.financial_convergence_mode_enabled and settings.storage_mode == "memory":
        raise ValueError("financial_convergence_mode_requires_persistent_storage")
    if settings.financial_convergence_mode_enabled and not settings.execution_command_flow_enabled:
        raise ValueError("financial_convergence_mode_requires_execution_command_flow")
    if settings.financial_convergence_mode_enabled and not settings.portfolio_ledger_truth_enabled:
        raise ValueError("financial_convergence_mode_requires_portfolio_ledger_truth")
    if settings.financial_convergence_mode_enabled and not settings.recovery_reconciliation_execution_ledger_enabled:
        raise ValueError("financial_convergence_mode_requires_phase4_recovery")
    if settings.financial_convergence_mode_enabled and not settings.operator_control_plane_execution_ledger_enabled:
        raise ValueError("financial_convergence_mode_requires_phase5_control_plane")
    if settings.financial_convergence_mode_enabled and settings.event_persistence_mode != "strict":
        raise ValueError("financial_convergence_mode_requires_strict_event_persistence")
    if settings.financial_convergence_mode_enabled and not settings.database_single_runtime_guard_enabled:
        raise ValueError("financial_convergence_mode_requires_single_runtime_guard")


def _validate_startup_profile_settings(settings: AATSSettings, runtime_layering: RuntimeLayering) -> None:
    if settings.startup_profile == "derivatives" and settings.trading_product_type != "derivatives":
        raise ValueError("startup_profile_derivatives_requires_derivatives_product_type")
    if settings.startup_profile == "spot" and settings.trading_product_type != "spot":
        raise ValueError("startup_profile_spot_requires_spot_product_type")

    if settings.startup_profile == "derivatives" and settings.mode == "guarded_live" and settings.execution_backend == "okx":
        if settings.config_profile not in DERIVATIVES_GUARDED_CONFIG_PROFILES:
            raise ValueError("startup_profile_derivatives_requires_dedicated_derivatives_config_profile")
    if settings.startup_profile == "spot" and settings.mode == "guarded_live" and settings.execution_backend == "okx":
        if settings.config_profile not in SPOT_GUARDED_CONFIG_PROFILES:
            raise ValueError("startup_profile_spot_requires_dedicated_spot_config_profile")

    if settings.config_profile in DERIVATIVES_GUARDED_CONFIG_PROFILES:
        if settings.trading_product_type != "derivatives":
            raise ValueError("guarded_derivatives_config_profile_requires_derivatives_product_type")
        if settings.margin_mode == "cash":
            raise ValueError("guarded_derivatives_config_profile_disallows_cash_margin_mode")
    if settings.config_profile in SPOT_GUARDED_CONFIG_PROFILES:
        if settings.trading_product_type != "spot":
            raise ValueError("guarded_spot_config_profile_requires_spot_product_type")
        if settings.margin_mode != "cash":
            raise ValueError("guarded_spot_config_profile_requires_cash_margin_mode")

    exchange_runtime_kind = _exchange_runtime_hardening_kind(settings, runtime_layering)
    if exchange_runtime_kind is None:
        return
    error_prefix = f"{exchange_runtime_kind}_exchange_runtime"
    if settings.execution_backend != "okx":
        raise ValueError(f"{error_prefix}_requires_okx_execution_backend")
    if settings.account_backend != "okx":
        raise ValueError(f"{error_prefix}_requires_okx_account_backend")
    if not settings.account_read_enabled:
        raise ValueError(f"{error_prefix}_requires_account_read_enabled")
    if exchange_runtime_kind == "derivatives" and settings.margin_mode == "cash":
        raise ValueError("derivatives_exchange_runtime_disallows_cash_margin_mode")
    if exchange_runtime_kind == "spot" and settings.margin_mode != "cash":
        raise ValueError("spot_exchange_runtime_requires_cash_margin_mode")
    if settings.storage_mode != "postgres":
        raise ValueError(f"{error_prefix}_requires_postgres_storage")
    if not settings.database_url_configured:
        raise ValueError(f"{error_prefix}_requires_database_url")
    if not settings.database_single_runtime_guard_enabled:
        raise ValueError(f"{error_prefix}_requires_single_runtime_guard")
    if not settings.okx_credentials_configured:
        raise ValueError(f"{error_prefix}_requires_okx_credentials")
    if not settings.operator_auth_enabled:
        raise ValueError(f"{error_prefix}_requires_operator_auth")
    if settings.operator_unsafe_write_without_auth:
        raise ValueError(f"{error_prefix}_disallows_unsafe_operator_write_without_auth")


def _exchange_runtime_hardening_kind(
    settings: AATSSettings,
    runtime_layering: RuntimeLayering,
) -> str | None:
    if not runtime_layering.environment_capabilities.exchange_coupled:
        return None
    if settings.trading_product_type == "derivatives" and (
        settings.startup_profile == "derivatives"
        or settings.config_profile in DERIVATIVES_GUARDED_CONFIG_PROFILES
    ):
        return "derivatives"
    if settings.trading_product_type == "spot" and (
        settings.startup_profile == "spot"
        or settings.config_profile in SPOT_GUARDED_CONFIG_PROFILES
    ):
        return "spot"
    return None


def _validate_operator_auth_settings(settings: AATSSettings, storage: StorageBackends) -> None:
    if settings.storage_mode != "postgres":
        return
    if not settings.operator_auth_enabled:
        return
    if not settings.operator_session_configured:
        return
    if settings.operator_write_api_key:
        return
    if enabled_admin_count(storage.operator_repo) > 0:
        return
    raise ValueError("operator_session_auth_requires_enabled_admin_user")


def _backfill_fill_outcomes_from_event_store(
    *,
    event_store: EventStore,
    fill_outcome_repo: FillOutcomeRepository,
    execution_repo: ExecutionRepository,
) -> None:
    fill_by_id = {fill.fill_id: fill for fill in execution_repo.fills()}
    for event in event_store.by_topic(topics.PORTFOLIO_BALANCE_DELTAS):
        try:
            balance_delta = PortfolioBalanceDelta.model_validate(event.payload)
        except Exception:
            continue
        base_outcome = FillOutcomeRecord.from_balance_delta(balance_delta)
        fill = fill_by_id.get(base_outcome.fill_id)
        outcome = (
            base_outcome
            if fill is None
            else FillOutcomeRecord.from_fill_and_balance_delta(
                fill=fill,
                balance_delta=balance_delta,
            )
        )
        fill_outcome_repo.save_outcome(outcome)


def build_storage_backends(settings: AATSSettings) -> StorageBackends:
    if settings.storage_mode == "memory":
        return StorageBackends(
            event_store=InMemoryEventStore(),
            audit_repo=InMemoryAuditRepository(),
            portfolio_repo=InMemoryPortfolioRepository(),
            fill_outcome_repo=InMemoryFillOutcomeRepository(),
            execution_repo=InMemoryExecutionRepository(),
            obligation_repo=InMemoryExecutionObligationRepository(),
            outbox_repo=None,
            reconciliation_repo=InMemoryReconciliationRepository(),
            operator_repo=InMemoryOperatorUserRepository(),
            runtime_profile_repo=InMemoryRuntimeProfileRepository(),
            strategy_profile_repo=InMemoryStrategyProfileRepository(),
            phase1_shadow=Phase1ShadowSubsystem(),
            funding_fee_repo=InMemoryFundingFeeRepository(),
        )

    if not settings.database_url:
        raise ValueError("AATS_DATABASE_URL must be configured when storage_mode=postgres")

    database_runtime = create_database_runtime(settings.database_url)
    if settings.database_auto_create_schema:
        create_schema(database_runtime)
    validate_runtime_schema(database_runtime)
    if settings.database_single_runtime_guard_enabled:
        database_runtime.acquire_single_runtime_lock(settings.database_runtime_lock_key)

    execution_order_repo = PostgresExecutionOrderRepository(database_runtime.session_factory)
    execution_order_history_repo = PostgresExecutionOrderHistoryRepository(database_runtime.session_factory)
    execution_command_repo = PostgresExecutionCommandRepository(database_runtime.session_factory)
    execution_fill_repo_v2 = PostgresExecutionFillRepositoryV2(database_runtime.session_factory)
    reservation_repo_v2 = PostgresReservationRepository(database_runtime.session_factory)
    ledger_account_repo = PostgresLedgerAccountRepository(database_runtime.session_factory)
    ledger_journal_repo = PostgresLedgerJournalRepository(database_runtime.session_factory)
    ledger_entry_repo = PostgresLedgerEntryRepository(database_runtime.session_factory)
    settlement_repo = PostgresSettlementRepository(database_runtime.session_factory)
    position_lot_repo = PostgresPositionLotRepository(database_runtime.session_factory)
    lot_event_repo = PostgresLotEventRepository(database_runtime.session_factory)
    funding_fee_repo = PostgresFundingFeeRepository(database_runtime.session_factory)
    external_inbox_repo = PostgresExternalInboxRepository(database_runtime.session_factory)
    command_outbox_repo_v2 = PostgresCommandOutboxRepositoryV2(database_runtime.session_factory)

    if settings.financial_convergence_mode_enabled:
        phase1_execution_shadow_service = None
        phase1_ledger_mirror_service = Phase1LedgerMirrorService(
            reservation_repo=reservation_repo_v2,
            ledger_account_repo=ledger_account_repo,
            ledger_journal_repo=ledger_journal_repo,
            ledger_entry_repo=ledger_entry_repo,
            settlement_repo=settlement_repo,
        )
    else:
        phase1_execution_shadow_service = Phase1ExecutionShadowService(
            execution_order_repo=execution_order_repo,
            execution_order_history_repo=execution_order_history_repo,
            execution_fill_repo=execution_fill_repo_v2,
        )
        phase1_ledger_mirror_service = Phase1LedgerMirrorService(
            reservation_repo=reservation_repo_v2,
            ledger_account_repo=ledger_account_repo,
            ledger_journal_repo=ledger_journal_repo,
            ledger_entry_repo=ledger_entry_repo,
            settlement_repo=settlement_repo,
        )

    legacy_execution_repo = PostgresExecutionRepository(database_runtime.session_factory)
    execution_repo = (
        ConvergedPostgresExecutionRepository(
            database_runtime.session_factory,
            execution_order_repo=execution_order_repo,
            execution_order_history_repo=execution_order_history_repo,
            execution_fill_repo=execution_fill_repo_v2,
        )
        if settings.financial_convergence_mode_enabled
        else legacy_execution_repo
    )

    return StorageBackends(
        execution_order_repo=execution_order_repo,
        execution_order_history_repo=execution_order_history_repo,
        execution_command_repo=execution_command_repo,
        execution_fill_repo_v2=execution_fill_repo_v2,
        reservation_repo_v2=reservation_repo_v2,
        ledger_account_repo=ledger_account_repo,
        ledger_journal_repo=ledger_journal_repo,
        ledger_entry_repo=ledger_entry_repo,
        settlement_repo=settlement_repo,
        position_lot_repo=position_lot_repo,
        lot_event_repo=lot_event_repo,
        external_inbox_repo=external_inbox_repo,
        command_outbox_repo_v2=command_outbox_repo_v2,
        phase1_execution_shadow_service=phase1_execution_shadow_service,
        phase1_ledger_mirror_service=phase1_ledger_mirror_service,
        phase1_shadow=Phase1ShadowSubsystem(
            execution_order_repo=execution_order_repo,
            execution_order_history_repo=execution_order_history_repo,
            execution_command_repo=execution_command_repo,
            execution_fill_repo=execution_fill_repo_v2,
            reservation_repo=reservation_repo_v2,
            ledger_account_repo=ledger_account_repo,
            ledger_journal_repo=ledger_journal_repo,
            ledger_entry_repo=ledger_entry_repo,
            settlement_repo=settlement_repo,
            execution_shadow_service=phase1_execution_shadow_service,
            ledger_mirror_service=phase1_ledger_mirror_service,
            external_inbox_repo=external_inbox_repo,
            command_outbox_repo=command_outbox_repo_v2,
        ),
        event_store=PostgresEventStore(database_runtime.session_factory),
        audit_repo=PostgresAuditRepository(database_runtime.session_factory),
        portfolio_repo=PostgresPortfolioRepository(database_runtime.session_factory),
        fill_outcome_repo=PostgresFillOutcomeRepository(database_runtime.session_factory),
        execution_repo=execution_repo,
        obligation_repo=PostgresExecutionObligationRepository(database_runtime.session_factory),
        outbox_repo=PostgresOutboxRepository(database_runtime.session_factory),
        reconciliation_repo=PostgresReconciliationRepository(database_runtime.session_factory),
        operator_repo=PostgresOperatorUserRepository(database_runtime.session_factory),
        runtime_profile_repo=PostgresRuntimeProfileRepository(database_runtime.session_factory),
        strategy_profile_repo=PostgresStrategyProfileRepository(database_runtime.session_factory),
        funding_fee_repo=funding_fee_repo,
        database_runtime=database_runtime,
    )


def _build_execution_adapter(
    *,
    settings: AATSSettings,
    market_gateway: MarketDataGateway,
    account_service: OKXAccountService,
    obligation_repo: ExecutionObligationRepository | None = None,
    mode_controller: RuntimeModeController,
    environment_capabilities: EnvironmentCapabilities | None = None,
    policy_profile: PolicyProfile | None = None,
    health_service: SystemHealthService | None = None,
) -> ExchangeAdapter:
    resolved_environment = environment_capabilities or mode_controller.environment_capabilities
    resolved_policy = policy_profile or mode_controller.policy_profile
    if resolved_environment.execution_adapter_kind == "okx":
        return OKXExecutionAdapter(
            settings=settings,
            client=OKXRESTClient(settings=settings),
            account_service=account_service,
            mode_controller=mode_controller,
            obligation_repo=obligation_repo,
            environment_capabilities=resolved_environment,
            policy_profile=resolved_policy,
            health_service=health_service,
            price_provider=market_gateway.latest_price,
        )
    return PaperExecutionAdapter(
        price_provider=market_gateway.latest_price,
        taker_fee_bps=settings.paper_taker_fee_bps,
        environment_capabilities=resolved_environment,
    )


def _build_position_target_handler(
    *,
    runtime_layering: RuntimeLayering,
    account_service: OKXAccountService,
    policy_engine: PolicyEngine,
    risk_engine: RiskEngine,
    execution_planner: ExecutionPlanner,
    market_gateway: MarketDataGateway,
    kill_switch: KillSwitch,
    metrics: MetricsRegistry,
    bus: InMemoryEventBus,
):
    def _exposure_side(quantity: Decimal) -> str:
        if quantity > Decimal("1e-12"):
            return "long"
        if quantity < Decimal("-1e-12"):
            return "short"
        return "flat"

    def _final_action(*, current_qty: Decimal, target_qty: Decimal) -> str:
        current_side = _exposure_side(current_qty)
        target_side = _exposure_side(target_qty)
        if target_side == current_side:
            if target_side == "flat":
                return "hold"
            if abs(target_qty) > abs(current_qty) + Decimal("1e-12"):
                return "scale_in"
            if abs(target_qty) + Decimal("1e-12") < abs(current_qty):
                return "reduce"
            return "hold"
        if target_side == "flat":
            return "exit"
        if current_side == "flat":
            return "enter"
        return "reverse"

    def _finalize_decision_outcome(
        *,
        target: PositionTarget,
        policy_decision,
        risk_decision,
        execution_continues: bool,
        extra_blocked_reasons: list[str] | None = None,
    ) -> DecisionOutcome | None:
        outcome = target.decision_outcome
        if outcome is None:
            return None
        current_position_qty = target.current_position_qty
        continued_target_qty = (
            outcome.final_target_qty
            if risk_decision is None
            else risk_decision.capped_target_position_qty
        )
        final_target_qty = continued_target_qty if execution_continues else current_position_qty
        blocked_reasons = list(outcome.decision_blocked_reasons)
        blocked_reasons.extend(list(policy_decision.rejection_reasons or []))
        if risk_decision is not None:
            blocked_reasons.extend(list(risk_decision.rejection_reasons or []))
            blocked_reasons.extend(list(risk_decision.constraints_applied or []))
        blocked_reasons.extend(list(extra_blocked_reasons or []))
        policy_blocked = (not policy_decision.execution_allowed) or ("kill_switch_active" in blocked_reasons)
        return outcome.model_copy(
            update={
                "finalized": True,
                "final_target_qty": final_target_qty,
                "final_direction": _exposure_side(final_target_qty),
                "final_action": _final_action(
                    current_qty=target.current_position_qty,
                    target_qty=final_target_qty,
                ),
                "decision_blocked_reasons": list(dict.fromkeys(item for item in blocked_reasons if item)),
                "policy_blocked": policy_blocked,
                "policy_blocked_reasons": list(dict.fromkeys([
                    *(policy_decision.rejection_reasons or []),
                    *([item for item in (extra_blocked_reasons or []) if item == "kill_switch_active"]),
                ])),
                "risk_capped": False
                if risk_decision is None
                else bool(
                    risk_decision.modified
                    or risk_decision.rejection_reasons
                    or risk_decision.constraints_applied
                ),
                "risk_capped_reasons": []
                if risk_decision is None
                else list(risk_decision.rejection_reasons or [])
                + list(risk_decision.constraints_applied or []),
                "risk_capped_target_qty": None if risk_decision is None else risk_decision.capped_target_position_qty,
            }
        )

    async def handle_position_target(message: dict[str, Any]) -> None:
        target = parse_payload(message, PositionTarget)
        if runtime_layering.environment_capabilities.account_state_source_kind == "exchange":
            await account_service.refresh()

        policy_decision = policy_engine.evaluate(target=target)
        await policy_engine.publish_decision(bus=bus, target=target, decision=policy_decision)
        if not policy_decision.execution_allowed:
            finalized_outcome = _finalize_decision_outcome(
                target=target,
                policy_decision=policy_decision,
                risk_decision=None,
                execution_continues=False,
            )
            if finalized_outcome is not None:
                await publish_model(
                    bus=bus,
                    topic=topics.DECISION_OUTCOMES,
                    key=target.symbol,
                    payload_model=finalized_outcome,
                    source_component="decision_engine",
                )
            return

        risk_decision = risk_engine.evaluate(target=target)
        await risk_engine.publish_decision(bus=bus, target=target, decision=risk_decision)
        if not risk_decision.approved or risk_decision.halt_required:
            finalized_outcome = _finalize_decision_outcome(
                target=target,
                policy_decision=policy_decision,
                risk_decision=risk_decision,
                execution_continues=False,
            )
            if finalized_outcome is not None:
                await publish_model(
                    bus=bus,
                    topic=topics.DECISION_OUTCOMES,
                    key=target.symbol,
                    payload_model=finalized_outcome,
                    source_component="decision_engine",
                )
            return
        if kill_switch.halted:
            finalized_outcome = _finalize_decision_outcome(
                target=target,
                policy_decision=policy_decision,
                risk_decision=risk_decision,
                execution_continues=False,
                extra_blocked_reasons=["kill_switch_active"],
            )
            if finalized_outcome is not None:
                await publish_model(
                    bus=bus,
                    topic=topics.DECISION_OUTCOMES,
                    key=target.symbol,
                    payload_model=finalized_outcome,
                    source_component="decision_engine",
                )
            return

        finalized_outcome = _finalize_decision_outcome(
            target=target,
            policy_decision=policy_decision,
            risk_decision=risk_decision,
            execution_continues=True,
        )
        if finalized_outcome is not None:
            await publish_model(
                bus=bus,
                topic=topics.DECISION_OUTCOMES,
                key=target.symbol,
                payload_model=finalized_outcome,
                source_component="decision_engine",
            )

        target_reference_price = (
            abs(target.target_notional / target.target_position_qty)
            if abs(target.target_position_qty) > 1e-12
            else None
        )
        current_reference_price = (
            abs(target.current_notional / target.current_position_qty)
            if abs(target.current_position_qty) > 1e-12
            else None
        )
        reference_price = next(
            (
                candidate
                for candidate in (
                    target_reference_price,
                    current_reference_price,
                    market_gateway.latest_price(target.symbol),
                )
                if candidate is not None and candidate > 0
            ),
            None,
        )
        account_snapshot = account_service.latest_snapshot()
        account_configuration = (
            None if account_snapshot is None else account_snapshot.account_configuration
        )
        instrument_rule = account_service.instrument_metadata(target.symbol)

        plan = execution_planner.build_plan(
            decision_id=target.decision_id,
            symbol=target.symbol,
            current_position_qty=target.current_position_qty,
            target_position_qty=target.target_position_qty,
            approved_target_position_qty=risk_decision.capped_target_position_qty,
            delta_qty=risk_decision.capped_target_position_qty - target.current_position_qty,
            urgency=target.urgency,
            max_slippage_tolerance_bps=target.max_slippage_tolerance_bps,
            reference_price=reference_price,
            product_type=target.product_type,
            target_leverage=target.target_leverage,
            margin_mode=target.margin_mode,
            td_mode=target.margin_mode,
            position_mode=(
                None
                if account_configuration is None
                else account_configuration.position_mode
            ),
            instrument_family=(
                None
                if instrument_rule is None
                else instrument_rule.instrument_family
            ),
            settle_currency=(
                None
                if instrument_rule is None
                else instrument_rule.settle_currency
            ),
            required_initial_margin=risk_decision.required_initial_margin,
            projected_margin_usage=risk_decision.projected_margin_usage,
            projected_notional=risk_decision.projected_notional,
            risk_budget_multiplier=risk_decision.risk_budget_multiplier,
            risk_budget_state=risk_decision.risk_budget_state,
            execution_aggressiveness_multiplier=risk_decision.execution_aggressiveness_multiplier,
            execution_aggressiveness_state=risk_decision.execution_aggressiveness_state,
            only_reduce_required=risk_decision.only_reduce_required,
            risk_limit_breached=risk_decision.risk_limit_breached,
            liquidation_buffer_remaining=risk_decision.liquidation_buffer_remaining,
            ai_execution_parameter_suggestion=target.ai_execution_parameter_suggestion,
        )
        if plan is None:
            return
        await execution_planner.publish_plan(bus=bus, plan=plan)

        intent = execution_planner.build_intent(plan=plan)
        if intent is None:
            return
        metrics.increment("order_intents_generated")
        await execution_planner.publish_intent(bus=bus, intent=intent)

    return handle_position_target


async def _subscribe_critical_handlers(
    *,
    bus: InMemoryEventBus,
    feature_engine: FeatureEngine,
    decision_trigger: DecisionCycleTrigger,
    order_manager: OrderManager,
    portfolio_service: PortfolioService,
    reconciliation_service: ReconciliationService,
    audit_service: DecisionAuditService,
    position_target_handler,
) -> None:
    await bus.subscribe(topics.MARKET_SNAPSHOTS, feature_engine.handle_market_snapshot)
    await bus.subscribe(topics.FEATURE_SNAPSHOTS, decision_trigger.handle_feature_snapshot)
    await bus.subscribe(topics.ORDER_INTENTS, order_manager.handle_order_intent)
    await bus.subscribe(topics.FILL_EVENTS, portfolio_service.handle_fill_event)
    await bus.subscribe(
        topics.PORTFOLIO_SNAPSHOTS,
        resilient_subscription_handler(
            topic=topics.PORTFOLIO_SNAPSHOTS,
            name="audit.handle_portfolio_snapshot",
            handler=audit_service.handle_portfolio_snapshot,
            subscription_class="pre_reconciliation_observer",
            raise_on_error=True,
        ),
    )
    await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, reconciliation_service.handle_portfolio_snapshot)
    await bus.subscribe(topics.POSITION_TARGETS, position_target_handler)


def _observer_subscription_specs(
    *,
    audit_service: DecisionAuditService,
    ai_service: AIInferenceService,
    reconciliation_service: ReconciliationService,
) -> tuple[ObserverSubscriptionSpec, ...]:
    return (
        ObserverSubscriptionSpec(topics.DECISION_CONTEXTS, "audit.handle_decision_context", audit_service.handle_decision_context),
        ObserverSubscriptionSpec(topics.BASELINE_ASSESSMENTS, "audit.handle_baseline_assessment", audit_service.handle_baseline_assessment),
        ObserverSubscriptionSpec(topics.AI_DECISION_BRIEFS, "audit.handle_ai_decision_brief", audit_service.handle_ai_decision_brief),
        ObserverSubscriptionSpec(topics.AI_ASSESSMENTS, "audit.handle_ai_assessment", audit_service.handle_ai_assessment),
        ObserverSubscriptionSpec(topics.AI_SHADOW_DECISIONS, "audit.handle_ai_shadow_decision", audit_service.handle_ai_shadow_decision),
        ObserverSubscriptionSpec(topics.AI_SHADOW_EVALUATIONS, "audit.handle_ai_shadow_evaluation", audit_service.handle_ai_shadow_evaluation),
        ObserverSubscriptionSpec(topics.POSITION_TARGETS, "audit.handle_position_target", audit_service.handle_position_target),
        ObserverSubscriptionSpec(topics.DECISION_OUTCOMES, "audit.handle_decision_outcome", audit_service.handle_decision_outcome),
        ObserverSubscriptionSpec(topics.POLICY_DECISIONS, "audit.handle_policy_decision", audit_service.handle_policy_decision),
        ObserverSubscriptionSpec(topics.RISK_DECISIONS, "audit.handle_risk_decision", audit_service.handle_risk_decision),
        ObserverSubscriptionSpec(topics.EXECUTION_PLANS, "audit.handle_execution_plan", audit_service.handle_execution_plan),
        ObserverSubscriptionSpec(topics.ORDER_INTENTS, "audit.handle_order_intent", audit_service.handle_order_intent),
        ObserverSubscriptionSpec(topics.ORDER_UPDATES, "audit.handle_order_update", audit_service.handle_order_update),
        ObserverSubscriptionSpec(topics.FILL_EVENTS, "audit.handle_fill_event", audit_service.handle_fill_event),
        ObserverSubscriptionSpec(topics.PORTFOLIO_SNAPSHOTS, "ai.handle_portfolio_snapshot", ai_service.handle_portfolio_snapshot),
        ObserverSubscriptionSpec(topics.RECONCILIATION_REPORTS, "ai.handle_reconciliation_report", ai_service.handle_reconciliation_report),
        ObserverSubscriptionSpec(topics.RECONCILIATION_REPORTS, "audit.handle_reconciliation_report", audit_service.handle_reconciliation_report),
        ObserverSubscriptionSpec(topics.PROCESSING_FAILURES, "reconciliation.handle_processing_failure", reconciliation_service.handle_processing_failure),
    )


async def _subscribe_observer_handlers(
    *,
    bus: InMemoryEventBus,
    specs: tuple[ObserverSubscriptionSpec, ...],
) -> None:
    for spec in specs:
        await bus.subscribe(
            spec.topic,
            resilient_subscription_handler(
                topic=spec.topic,
                name=spec.name,
                handler=spec.handler,
                subscription_class="observer",
                raise_on_error=spec.name == "audit.handle_reconciliation_report",
            ),
        )


async def build_runtime(
    settings: AATSSettings | None = None,
    *,
    bootstrap_portfolio_snapshot: bool = True,
) -> ApplicationRuntime:
    base_settings = settings or load_settings()
    base_runtime_layering = resolve_runtime_layering(base_settings)
    _validate_runtime_settings(base_settings, base_runtime_layering)
    storage = build_storage_backends(base_settings)
    try:
        profile_resolution = runtime_profile_resolution(settings=base_settings, repo=storage.runtime_profile_repo)
        runtime_settings = AATSSettings.model_validate(profile_resolution.resolved_settings)
        runtime_layering = resolve_runtime_layering(runtime_settings)
        state_scope = runtime_state_scope(runtime_settings)
        _validate_runtime_settings(runtime_settings, runtime_layering)
        _validate_operator_auth_settings(runtime_settings, storage)
        seed_strategy_profiles(settings=runtime_settings, repo=storage.strategy_profile_repo)
    except Exception:
        if storage.database_runtime is not None:
            storage.database_runtime.dispose()
        raise
    metrics = MetricsRegistry()
    bus = InMemoryEventBus(
        event_store=storage.event_store,
        persistence_mode=runtime_settings.event_persistence_mode,
    )
    _backfill_fill_outcomes_from_event_store(
        event_store=storage.event_store,
        fill_outcome_repo=storage.fill_outcome_repo,
        execution_repo=storage.execution_repo,
    )

    kill_switch = KillSwitch()
    mode_controller = RuntimeModeController(
        settings=runtime_settings,
        kill_switch=kill_switch,
        runtime_layering=runtime_layering,
    )

    normalizer = MarketSnapshotNormalizer(exchange_name=runtime_settings.exchange_name)
    market_publisher = MarketSnapshotPublisher(bus=bus)
    okx_client = OKXRESTClient(settings=runtime_settings)
    okx_ws_client = (
        OKXPublicWebSocketClient(settings=runtime_settings)
        if runtime_settings.market_data_backend == "okx"
        else None
    )
    market_gateway = MarketDataGateway(
        settings=runtime_settings,
        normalizer=normalizer,
        publisher=market_publisher,
        okx_ws_client=okx_ws_client,
        okx_rest_client=okx_client if runtime_settings.market_data_backend == "okx" else None,
    )
    private_account_ws_client = (
        OKXPrivateWebSocketClient(settings=runtime_settings)
        if runtime_settings.account_backend == "okx" and runtime_settings.account_read_enabled
        else None
    )
    account_service = OKXAccountService(
        settings=runtime_settings,
        client=okx_client,
        private_ws_client=private_account_ws_client,
    )
    fee_resolver = EffectiveFeeResolver(
        settings=runtime_settings,
        account_service=account_service,
    )
    baseline_import_service = AccountBaselineImportService(event_store=storage.event_store)
    bootstrap_from_exchange = runtime_layering.recovery_policy.startup_baseline_import_supported
    execution_adapter = _build_execution_adapter(
        settings=runtime_settings,
        market_gateway=market_gateway,
        account_service=account_service,
        obligation_repo=storage.obligation_repo,
        mode_controller=mode_controller,
        environment_capabilities=runtime_layering.environment_capabilities,
        policy_profile=runtime_layering.policy_profile,
    )
    health_service = SystemHealthService(
        settings=runtime_settings,
        mode_controller=mode_controller,
        kill_switch=kill_switch,
        market_provider=market_gateway,
        account_provider=account_service,
        execution_provider=execution_adapter,
        reconciliation_repo=storage.reconciliation_repo,
        recovery_policy=runtime_layering.recovery_policy,
    )
    if isinstance(execution_adapter, OKXExecutionAdapter):
        execution_adapter.health_service = health_service

    snapshot_builder = PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator())
    feature_engine = FeatureEngine(bus=bus, calculator=FeatureCalculator())
    phase1_shadow_monitor = Phase1ShadowMonitor(
        execution_repo=storage.execution_repo,
        obligation_repo=storage.obligation_repo,
        state_scope=state_scope,
        execution_shadow_service=storage.phase1_execution_shadow_service,
        ledger_mirror_service=storage.phase1_ledger_mirror_service,
        execution_order_repo=storage.execution_order_repo,
        execution_fill_repo=storage.execution_fill_repo_v2,
        reservation_repo=storage.reservation_repo_v2,
    )
    phase1_shadow = storage.phase1_shadow or Phase1ShadowSubsystem(
        execution_order_repo=storage.execution_order_repo,
        execution_order_history_repo=storage.execution_order_history_repo,
        execution_command_repo=storage.execution_command_repo,
        execution_fill_repo=storage.execution_fill_repo_v2,
        reservation_repo=storage.reservation_repo_v2,
        ledger_account_repo=storage.ledger_account_repo,
        ledger_journal_repo=storage.ledger_journal_repo,
        ledger_entry_repo=storage.ledger_entry_repo,
        settlement_repo=storage.settlement_repo,
        position_lot_repo=storage.position_lot_repo,
        lot_event_repo=storage.lot_event_repo,
        external_inbox_repo=storage.external_inbox_repo,
        command_outbox_repo=storage.command_outbox_repo_v2,
        execution_shadow_service=storage.phase1_execution_shadow_service,
        ledger_mirror_service=storage.phase1_ledger_mirror_service,
    )
    phase1_shadow.monitor = phase1_shadow_monitor
    health_service.phase1_shadow_provider = phase1_shadow_monitor
    reconciliation_classifier = (
        RecoveryReconciliationClassifier()
        if runtime_settings.recovery_reconciliation_execution_ledger_enabled
        else None
    )
    ai_service = AIInferenceService(
        settings=runtime_settings,
        event_store=storage.event_store,
        bus=bus,
        execution_repo=storage.execution_repo,
        prompt_builder=PromptBuilder(),
        validator=AssessmentValidator(),
        fee_resolver=fee_resolver,
    )
    decision_trigger_policy = DecisionTriggerPolicy(settings=runtime_settings)
    decision_engine = DecisionOrchestrator(
        bus=bus,
        context_builder=DecisionContextBuilder(
            settings=runtime_settings,
            event_store=storage.event_store,
            portfolio_repo=storage.portfolio_repo,
            execution_repo=storage.execution_repo,
            mode_controller=mode_controller,
            health_service=health_service,
        ),
        baseline_strategy=BaselineStrategy(event_store=storage.event_store),
        ai_service=ai_service,
        target_engine=TargetPositionEngine(settings=runtime_settings, fee_resolver=fee_resolver),
        metrics=metrics,
    )
    decision_trigger = DecisionCycleTrigger(
        orchestrator=decision_engine,
        market_gateway=market_gateway,
        policy=decision_trigger_policy,
        can_trigger=lambda *, symbol: (not kill_switch.halted, "kill_switch_active" if kill_switch.halted else "ready"),
    )
    audit_service = DecisionAuditService(bus=bus, audit_repo=storage.audit_repo)

    policy_engine = PolicyEngine(
        settings=runtime_settings,
        kill_switch=kill_switch,
        mode_controller=mode_controller,
        health_service=health_service,
        environment_capabilities=runtime_layering.environment_capabilities,
        policy_profile=runtime_layering.policy_profile,
    )
    risk_engine = RiskEngine(
        settings=runtime_settings,
        account_service=account_service,
        health_service=health_service,
        trigger_policy=decision_trigger_policy,
        price_provider=market_gateway.latest_price,
        mode_controller=mode_controller,
        obligation_repo=storage.obligation_repo,
        environment_capabilities=runtime_layering.environment_capabilities,
        policy_profile=runtime_layering.policy_profile,
        fee_resolver=fee_resolver,
        reconciliation_repo=storage.reconciliation_repo,
    )
    execution_planner = ExecutionPlanner(settings=runtime_settings)
    obligation_service = ExecutionObligationService(
        settings=runtime_settings,
        obligation_repo=storage.obligation_repo,
        account_snapshot_loader=lambda: account_service.refresh(),
        price_provider=market_gateway.latest_price,
        fee_resolver=fee_resolver,
    )
    execution_outbox_publisher = None
    if (
        storage.database_runtime is not None
        and isinstance(storage.obligation_repo, PostgresExecutionObligationRepository)
        and isinstance(storage.event_store, PostgresEventStore)
        and storage.outbox_repo is not None
        and hasattr(storage.execution_repo, "save_order_state_in_session")
        and hasattr(storage.execution_repo, "save_fill_in_session")
    ):
        execution_outbox_publisher = PostgresExecutionOutboxPublisher(
            session_factory=storage.database_runtime.session_factory,
            event_store=storage.event_store,
            execution_repo=storage.execution_repo,
            obligation_repo=storage.obligation_repo,
            outbox_repo=storage.outbox_repo,
            bus=bus,
        )
    execution_order_service = None
    execution_command_processor = None
    if runtime_settings.execution_command_flow_enabled and storage.execution_command_repo is not None:
        execution_order_service = ExecutionOrderService(
            execution_command_repo=storage.execution_command_repo,
            execution_order_repo=storage.execution_order_repo,
            execution_order_history_repo=storage.execution_order_history_repo,
        )
    order_manager = OrderManager(
        settings=runtime_settings,
        bus=bus,
        adapter=execution_adapter,
        execution_repo=storage.execution_repo,
        obligation_service=obligation_service,
        execution_outbox_publisher=execution_outbox_publisher,
        persistent_order_service=execution_order_service,
        shadow_execution_service=storage.phase1_execution_shadow_service,
        shadow_execution_order_repo=storage.execution_order_repo,
        shadow_execution_order_history_repo=storage.execution_order_history_repo,
        shadow_execution_fill_repo=storage.execution_fill_repo_v2,
        shadow_ledger_mirror_service=storage.phase1_ledger_mirror_service,
        kill_switch=kill_switch,
    )
    if execution_order_service is not None and storage.execution_command_repo is not None:
        execution_command_processor = ExecutionCommandProcessor(
            execution_command_repo=storage.execution_command_repo,
            submit_executor=lambda intent, client_order_id=None: order_manager.process_submit_command(
                intent=intent,
                client_order_id=client_order_id,
            ),
            cancel_executor=lambda client_order_id: order_manager.process_cancel_command(
                client_order_id=client_order_id,
            ),
        )

    portfolio_state = PortfolioState(
        initial_usdt_balance=runtime_settings.initial_usdt_balance,
        default_product_type=runtime_settings.trading_product_type,
        default_margin_mode=runtime_settings.margin_mode,
    )
    if (
        runtime_settings.portfolio_ledger_truth_enabled
        and storage.ledger_account_repo is not None
        and storage.ledger_journal_repo is not None
        and storage.ledger_entry_repo is not None
    ):
        portfolio_service = LedgerBackedPortfolioService(
            bus=bus,
            state=portfolio_state,
            snapshot_builder=snapshot_builder,
            portfolio_repo=storage.portfolio_repo,
            fill_outcome_repo=storage.fill_outcome_repo,
            price_provider=market_gateway.latest_price,
            execution_repo=storage.execution_repo,
            settlement_posting_service=LedgerSettlementPostingService(
                ledger_account_repo=storage.ledger_account_repo,
                ledger_journal_repo=storage.ledger_journal_repo,
                ledger_entry_repo=storage.ledger_entry_repo,
                reservation_repo=storage.reservation_repo_v2,
            ),
            persistent_lot_book_service=(
                PersistentLotBookService(
                    position_lot_repo=storage.position_lot_repo,
                    lot_event_repo=storage.lot_event_repo,
                    projection_builder=LotBasedProjectionBuilder(),
                )
                if storage.position_lot_repo is not None and storage.lot_event_repo is not None
                else None
            ),
            initial_usdt_balance=runtime_settings.initial_usdt_balance,
            metrics=metrics,
        )
    else:
        portfolio_service = PortfolioService(
            bus=bus,
            state=portfolio_state,
            snapshot_builder=snapshot_builder,
            portfolio_repo=storage.portfolio_repo,
            fill_outcome_repo=storage.fill_outcome_repo,
            price_provider=market_gateway.latest_price,
            metrics=metrics,
        )
    funding_fee_sync_service = (
        LedgerFundingFeeSyncService(
            funding_fee_repo=storage.funding_fee_repo,
            ledger_account_repo=storage.ledger_account_repo,
            ledger_journal_repo=storage.ledger_journal_repo,
            ledger_entry_repo=storage.ledger_entry_repo,
        )
        if (
            storage.funding_fee_repo is not None
            and storage.ledger_account_repo is not None
            and storage.ledger_journal_repo is not None
            and storage.ledger_entry_repo is not None
        )
        else None
    )

    reconciliation_service = ReconciliationService(
        settings=runtime_settings,
        bus=bus,
        fetcher=ExchangeStateFetcher(account_service=account_service),
        comparator=StateComparator(),
        repair_service=ReconciliationRepairService(),
        reconciliation_repo=storage.reconciliation_repo,
        execution_repo=storage.execution_repo,
        portfolio_repo=storage.portfolio_repo,
        event_store=storage.event_store,
        reconstruction_service=PortfolioReconstructionService(
            initial_usdt_balance=runtime_settings.initial_usdt_balance,
            snapshot_builder=snapshot_builder,
        ),
        price_provider=market_gateway.latest_price,
        bootstrap_portfolio_from_exchange=bootstrap_from_exchange,
        recovery_policy=runtime_layering.recovery_policy,
        metrics=metrics,
        reconciliation_classifier=reconciliation_classifier,
    )
    base_recovery_service = ExecutionRecoveryService(
        settings=runtime_settings,
        execution_repo=storage.execution_repo,
        obligation_repo=storage.obligation_repo,
        portfolio_repo=storage.portfolio_repo,
        reconciliation_repo=storage.reconciliation_repo,
        reconstruction_service=PortfolioReconstructionService(
            initial_usdt_balance=runtime_settings.initial_usdt_balance,
            snapshot_builder=snapshot_builder,
        ),
        price_provider=market_gateway.latest_price,
        kill_switch=kill_switch,
        bootstrap_portfolio_from_exchange=bootstrap_from_exchange,
        reconciliation_stale_after_seconds=runtime_settings.reconciliation_stale_after_seconds,
        recovery_policy=runtime_layering.recovery_policy,
    )
    recovery_service = (
        ExecutionLedgerRecoveryService(
            settings=runtime_settings,
            base_recovery_service=base_recovery_service,
            reconciliation_repo=storage.reconciliation_repo,
            portfolio_repo=storage.portfolio_repo,
            kill_switch=kill_switch,
            reconciliation_classifier=reconciliation_classifier or RecoveryReconciliationClassifier(),
            execution_order_repo=storage.execution_order_repo,
            execution_command_repo=storage.execution_command_repo,
        )
        if runtime_settings.recovery_reconciliation_execution_ledger_enabled
        else base_recovery_service
    )
    position_target_handler = _build_position_target_handler(
        runtime_layering=runtime_layering,
        account_service=account_service,
        policy_engine=policy_engine,
        risk_engine=risk_engine,
        execution_planner=execution_planner,
        market_gateway=market_gateway,
        kill_switch=kill_switch,
        metrics=metrics,
        bus=bus,
    )
    await _subscribe_critical_handlers(
        bus=bus,
        feature_engine=feature_engine,
        decision_trigger=decision_trigger,
        order_manager=order_manager,
        portfolio_service=portfolio_service,
        reconciliation_service=reconciliation_service,
        audit_service=audit_service,
        position_target_handler=position_target_handler,
    )
    await _subscribe_observer_handlers(
        bus=bus,
        specs=_observer_subscription_specs(
            audit_service=audit_service,
            ai_service=ai_service,
            reconciliation_service=reconciliation_service,
        ),
    )

    if runtime_layering.environment_capabilities.account_state_source_kind == "exchange":
        account_snapshot = await account_service.refresh(force=True)
        funding_fee_sync_posted_count = 0
        if funding_fee_sync_service is not None:
            funding_result = funding_fee_sync_service.sync_recent_bills(
                rows=account_service.latest_recent_bills(),
                product_type=state_scope.product_type,
                margin_mode=state_scope.margin_mode,
            )
            funding_fee_sync_posted_count = funding_result.posted_count
        imported_baseline = None
        imported_baseline_event_id = None
        latest_scoped_snapshot = latest_matching_snapshot(storage.portfolio_repo.history(), state_scope)
        if (
            bootstrap_from_exchange
            and account_snapshot is not None
            and latest_scoped_snapshot is None
        ):
            imported = baseline_import_service.import_snapshot(
                exchange_snapshot=account_snapshot,
                portfolio_state=portfolio_service.state,
                product_type=state_scope.product_type,
                margin_mode=state_scope.margin_mode,
                allowed_symbols=state_scope.allowed_symbols,
            )
            imported_baseline = imported.snapshot
            imported_baseline_event_id = imported.event_id
    else:
        imported_baseline = None
        imported_baseline_event_id = None
        funding_fee_sync_posted_count = 0
    recovery_artifacts = recovery_service.recover(
        portfolio_state=portfolio_service.state,
        account_baseline=imported_baseline,
        account_baseline_event_id=imported_baseline_event_id,
    )
    if recovery_artifacts.rebuilt_snapshot is not None:
        await publish_model(
            bus=bus,
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=recovery_artifacts.rebuilt_snapshot,
            source_component="recovery_service",
        )
    if (
        bootstrap_portfolio_snapshot
        and latest_matching_snapshot(storage.portfolio_repo.history(), state_scope) is None
        and not recovery_artifacts.rebuilt_snapshot_saved
    ):
        await portfolio_service.bootstrap_snapshot(
            snapshot_origin="exchange_import" if imported_baseline is not None else "runtime_bootstrap"
        )
        recovery_status = recovery_artifacts.status.model_copy(
            update={"recovered_snapshot_available": True}
        )
    else:
        recovery_status = recovery_artifacts.status

    if (
        funding_fee_sync_posted_count > 0
        and hasattr(portfolio_service, "bootstrap_snapshot")
        and latest_matching_snapshot(storage.portfolio_repo.history(), state_scope) is not None
    ):
        await portfolio_service.bootstrap_snapshot(snapshot_origin="local_repair")

    latest_scoped_portfolio_snapshot = latest_matching_snapshot(storage.portfolio_repo.history(), state_scope)
    if (
        latest_scoped_portfolio_snapshot is not None
        and scoped_portfolio_event(storage.event_store.by_topic(topics.PORTFOLIO_SNAPSHOTS), state_scope) is None
    ):
        await publish_model(
            bus=bus,
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=latest_scoped_portfolio_snapshot,
            source_component="runtime",
        )

    runtime = ApplicationRuntime(
        started_at=utc_now(),
        settings=runtime_settings,
        runtime_profile_resolution=profile_resolution,
        runtime_layering=runtime_layering,
        runtime_profile=runtime_layering.runtime_profile,
        environment_capabilities=runtime_layering.environment_capabilities,
        policy_profile=runtime_layering.policy_profile,
        recovery_policy=runtime_layering.recovery_policy,
        bus=bus,
        event_store=storage.event_store,
        market_gateway=market_gateway,
        feature_engine=feature_engine,
        ai_service=ai_service,
        decision_engine=decision_engine,
        decision_trigger=decision_trigger,
        decision_trigger_policy=decision_trigger_policy,
        execution_planner=execution_planner,
        execution_adapter=execution_adapter,
        order_manager=order_manager,
        portfolio_service=portfolio_service,
        reconciliation_service=reconciliation_service,
        fee_resolver=fee_resolver,
        policy_engine=policy_engine,
        risk_engine=risk_engine,
        kill_switch=kill_switch,
        mode_controller=mode_controller,
        health_service=health_service,
        account_service=account_service,
        metrics=metrics,
        audit_repo=storage.audit_repo,
        portfolio_repo=storage.portfolio_repo,
        fill_outcome_repo=storage.fill_outcome_repo,
        execution_repo=storage.execution_repo,
        obligation_repo=storage.obligation_repo,
        reconciliation_repo=storage.reconciliation_repo,
        operator_repo=storage.operator_repo,
        runtime_profile_repo=storage.runtime_profile_repo,
        strategy_profile_repo=storage.strategy_profile_repo,
        execution_order_repo=storage.execution_order_repo,
        execution_order_history_repo=storage.execution_order_history_repo,
        execution_command_repo=storage.execution_command_repo,
        execution_fill_repo_v2=storage.execution_fill_repo_v2,
        reservation_repo_v2=storage.reservation_repo_v2,
        ledger_account_repo=storage.ledger_account_repo,
        ledger_journal_repo=storage.ledger_journal_repo,
        ledger_entry_repo=storage.ledger_entry_repo,
        settlement_repo=storage.settlement_repo,
        external_inbox_repo=storage.external_inbox_repo,
        command_outbox_repo_v2=storage.command_outbox_repo_v2,
        phase1_execution_shadow_service=storage.phase1_execution_shadow_service,
        phase1_ledger_mirror_service=storage.phase1_ledger_mirror_service,
        phase1_shadow_monitor=phase1_shadow_monitor,
        phase1_shadow=phase1_shadow,
        execution_order_service=execution_order_service,
        execution_command_processor=execution_command_processor,
        recovery_status=recovery_status,
        database_runtime=storage.database_runtime,
        execution_outbox_publisher=execution_outbox_publisher,
        funding_fee_repo=storage.funding_fee_repo,
        funding_fee_sync_service=funding_fee_sync_service,
    )
    from aats.services.operator.query_service import OperatorQueryService

    runtime.derivatives_live_guard_service = DerivativesLiveGuardService(
        settings=runtime.settings,
        kill_switch=runtime.kill_switch,
        account_service=runtime.account_service,
        event_store=runtime.event_store,
        metrics=runtime.metrics,
    )
    runtime.derivatives_live_guard_service.evaluate_now()
    runtime.health_service.runtime_guard_provider = runtime.derivatives_live_guard_service
    runtime.risk_engine.live_runtime_guard_provider = runtime.derivatives_live_guard_service
    runtime.trial_guard_service = ForwardTrialGuardService(
        settings=runtime.settings,
        kill_switch=runtime.kill_switch,
        event_store=runtime.event_store,
        metrics=runtime.metrics,
        profitability_provider=lambda limit: OperatorQueryService(runtime).profitability_overview(limit=limit),
        anomaly_provider=lambda limit: OperatorQueryService(runtime).execution_anomaly_report(limit=limit),
    )
    runtime.trial_guard_service.evaluate_now()
    runtime.risk_engine.trial_guard_provider = runtime.trial_guard_service
    runtime.decision_engine.strategy_profile_service = StrategyProfileControlService(runtime)
    return runtime
