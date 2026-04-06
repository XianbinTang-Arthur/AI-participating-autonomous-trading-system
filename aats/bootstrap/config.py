from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

import logging

from aats.bootstrap.logging import get_logger, log_event

_log = logging.getLogger(__name__)
from aats.bootstrap.managed_profiles import MANAGED_PROFILE_DERIVED_ENV_KEYS, load_managed_profile_values
from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import (
    AATSSettings,
    DEPRECATED_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY,
)
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope, parse_payload, publish_model
from aats.schemas.decision import DecisionOutcome, PositionTarget
from aats.schemas.execution import LegOrderIntent, order_intent_from_leg_order_intent
from aats.schemas.governance import PolicyDecision, RiskDecision
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
from aats.services.execution_engine.okx_account import (
    OKXAccountService,
    derivatives_position_mode_contract,
)
from aats.services.execution_engine.okx_adapter import OKXExecutionAdapter
from aats.services.execution_engine.okx_private_websocket import OKXPrivateWebSocketClient
from aats.services.execution_engine.baseline_import import AccountBaselineImportService
from aats.services.execution_engine.bundle_status import (
    apply_strategy_bundle_status_reason_codes,
    derive_strategy_bundle_status,
)
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
from aats.services.portfolio_service.outbox import PostgresPortfolioOutboxPublisher
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
from aats.services.recovery_control.startup_recovery import (
    apply_startup_exit_execution_review_overlay,
    persist_startup_exit_execution_state_snapshot,
    startup_refresh_exit_execution_truth,
)
from aats.services.runtime_scope import latest_matching_snapshot, runtime_state_scope, scoped_portfolio_event
from aats.services.strategy_engines.sleeve_execution_permission import non_protective_entry_execution_guard
from aats.services.strategy_engines.coordinator import StrategyCoordinatorService
from aats.services.strategy_engines.overlay_parent_exposure import overlay_parent_exposure_record
from aats.services.strategy_engines.sleeve_pnl_projection import SleevePnLProjectionService
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioBalanceDelta
from aats.services.portfolio_service.decimals import to_decimal
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
    ExitExecutionRepository,
    FillOutcomeRepository,
    FundingFeeRepository,
    SleevePnLRepository,
    ExecutionRepository,
    ExecutionObligationRepository,
    OperatorUserRepository,
    PortfolioRepository,
    ReconciliationRepository,
    StrategyRuntimeRepository,
)
from aats.storage.event_store import InMemoryEventStore
from aats.storage.exit_execution_repo import InMemoryExitExecutionRepository
from aats.storage.exit_execution_repo_postgres import PostgresExitExecutionRepository
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.execution_repo import InMemoryExecutionRepository
from aats.storage.execution_repo_converged_postgres import ConvergedPostgresExecutionRepository
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.fill_outcome_repo import InMemoryFillOutcomeRepository
from aats.storage.fill_outcome_repo_postgres import PostgresFillOutcomeRepository
from aats.storage.funding_fee_repo import InMemoryFundingFeeRepository
from aats.storage.funding_fee_repo_postgres import PostgresFundingFeeRepository
from aats.storage.sleeve_pnl_repo import InMemorySleevePnLRepository
from aats.storage.sleeve_pnl_repo_postgres import PostgresSleevePnLRepository
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
from aats.storage.strategy_profile_repo import InMemoryStrategyProfileRepository
from aats.storage.strategy_profile_repo_postgres import PostgresStrategyProfileRepository
from aats.storage.strategy_sleeve_repo import InMemoryStrategySleeveRepository
from aats.storage.strategy_sleeve_repo_postgres import PostgresStrategySleeveRepository
from aats.storage.strategy_runtime_repo import InMemoryStrategyRuntimeRepository
from aats.storage.strategy_runtime_repo_postgres import PostgresStrategyRuntimeRepository
from aats.storage.session import (
    DatabaseRuntime,
    apply_current_migrations,
    create_database_runtime,
    create_schema,
    scoped_runtime_lock_key,
    validate_runtime_schema,
)
from aats.schemas.system import RecoveryStatus
from aats.schemas.common import new_id, utc_now
from aats.schemas.operator import ExecutionErrorSummary, ProcessingFailureRecord
from aats.schemas.runtime_profiles import RuntimeProfileResolution
from aats.schemas.strategy_runtime import StrategyExecutionBundle
from aats.storage.base import StrategyProfileRepository, StrategySleeveRepository


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


# ── load_settings 分层旁路记录 ─────────────────────────────────────
# build_runtime() 中的 SettingsProvenanceTracker 可以读取此变量，
# 从而追踪 load_settings 内部的 YAML/managed + env 分层。
# 该变量仅在 load_settings() 调用时写入，不参与合并逻辑。
_load_settings_layers: dict[str, dict[str, Any]] = {}


def load_settings() -> AATSSettings:
    if "AATS_STRATEGY_SLEEVE_AUTO_PARALLEL_ENABLED" in os.environ:
        raise ValueError(
            "strategy_sleeve_auto_parallel_enabled_has_been_removed_use_strategy_sleeve_auto_execution_enabled"
        )
    sources, init_kwargs = AATSSettings._settings_init_sources()
    explicit_overrides = AATSSettings._settings_build_values(sources, init_kwargs)
    env_template_profile = explicit_overrides.get("env_template_profile")
    environment = explicit_overrides.get(
        "environment",
        AATSSettings.model_fields["environment"].default,
    )
    config_profile = explicit_overrides.get(
        "config_profile",
        AATSSettings.model_fields["config_profile"].default,
    )
    source_values: dict[str, Any] = {}
    # When startup runs through one of the managed .env profile templates, that
    # template path, runtime semantics come from managed profile defaults plus
    # a dedicated strategy tuning file. Legacy YAML overlays remain available
    # only for non-managed/manual config_profile startup paths.
    if env_template_profile is None:
        source_values = load_yaml_config(environment, config_profile)
    else:
        source_values = load_managed_profile_values(env_template_profile)
    for container in (source_values, explicit_overrides):
        if DEPRECATED_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY in container:
            raise ValueError(
                "strategy_sleeve_auto_parallel_enabled_has_been_removed_use_strategy_sleeve_auto_execution_enabled"
            )
    if env_template_profile is not None:
        derived_field_names = {
            key.removeprefix("AATS_").lower()
            for key in MANAGED_PROFILE_DERIVED_ENV_KEYS
        }
        ignored_override_fields = sorted(
            field_name
            for field_name in explicit_overrides
            if field_name != "env_template_profile" and field_name in derived_field_names
        )
        if ignored_override_fields:
            log_event(
                get_logger("aats.config"),
                "managed_profile_ignored_deprecated_env_overrides",
                env_template_profile=env_template_profile,
                ignored_fields=ignored_override_fields,
            )
        explicit_overrides = {
            field_name: value
            for field_name, value in explicit_overrides.items()
            if field_name == "env_template_profile" or field_name not in derived_field_names
        }
    # ── 旁路记录各层数据（供 provenance tracker 使用）───────────────
    _load_settings_layers.clear()
    _load_settings_layers["source_type"] = {
        "env_template_profile": env_template_profile,
        "environment": environment,
        "config_profile": config_profile,
    }
    _load_settings_layers["yaml_or_managed"] = dict(source_values)
    _load_settings_layers["env_overrides"] = dict(explicit_overrides)
    return AATSSettings.model_validate({**source_values, **explicit_overrides})


@dataclass(slots=True)
class StorageBackends:
    event_store: EventStore
    audit_repo: AuditRepository
    portfolio_repo: PortfolioRepository
    fill_outcome_repo: FillOutcomeRepository
    sleeve_pnl_repo: SleevePnLRepository
    execution_repo: ExecutionRepository
    obligation_repo: ExecutionObligationRepository
    reconciliation_repo: ReconciliationRepository
    operator_repo: OperatorUserRepository
    strategy_profile_repo: StrategyProfileRepository
    strategy_sleeve_repo: StrategySleeveRepository
    strategy_runtime_repo: StrategyRuntimeRepository
    exit_execution_repo: ExitExecutionRepository | None = None
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
    strategy_coordinator: Any | None
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
    sleeve_pnl_repo: SleevePnLRepository
    execution_repo: ExecutionRepository
    obligation_repo: ExecutionObligationRepository
    reconciliation_repo: ReconciliationRepository
    operator_repo: OperatorUserRepository
    strategy_profile_repo: StrategyProfileRepository
    strategy_sleeve_repo: StrategySleeveRepository
    strategy_runtime_repo: StrategyRuntimeRepository
    recovery_status: RecoveryStatus
    sleeve_auto_execution_config_source: str = "strategy_sleeve_auto_execution_enabled"
    sleeve_auto_execution_uses_deprecated_key: bool = False
    exit_execution_repo: ExitExecutionRepository | None = None
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
    sleeve_pnl_projection_service: SleevePnLProjectionService | None = None
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
                await self._evaluate_derivatives_live_guard_after_refresh()
            except Exception as exc:
                await self._record_background_failure(subsystem="account_refresh", exc=exc)
            await asyncio.sleep(self.settings.okx_account_refresh_interval_seconds)

    async def _evaluate_derivatives_live_guard_after_refresh(self) -> None:
        if self.derivatives_live_guard_service is None:
            return
        await asyncio.to_thread(self.derivatives_live_guard_service.evaluate_now)

    async def _sync_funding_fees_after_refresh(self) -> None:
        if self.funding_fee_sync_service is None:
            return
        result = await asyncio.to_thread(
            self.funding_fee_sync_service.sync_recent_bills,
            rows=self.account_service.latest_recent_bills(),
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
        )
        if result.posted_count <= 0:
            return
        if self.sleeve_pnl_projection_service is not None:
            await asyncio.to_thread(
                self.sleeve_pnl_projection_service.rebuild_scope,
                scope=runtime_state_scope(self.settings),
            )
        if hasattr(self.portfolio_service, "bootstrap_snapshot"):
            await self.portfolio_service.bootstrap_snapshot(snapshot_origin="local_repair")

    async def _sync_execution_loop(self) -> None:
        while True:
            try:
                await self.order_manager.sync_exchange_state()
            except Exception as exc:
                await self._record_background_failure(subsystem="execution_sync", exc=exc)
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
                await self._record_background_failure(subsystem="reconciliation_refresh", exc=exc)
            await asyncio.sleep(interval_seconds)

    async def _flush_execution_outbox_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                if self.execution_outbox_publisher is not None:
                    await self.execution_outbox_publisher.flush_pending()
                backoff = 1.0
            except Exception as exc:
                await self._record_background_failure(subsystem="execution_outbox_flush", exc=exc)
                backoff = min(backoff * 2, 30.0)
            await asyncio.sleep(backoff)

    async def _process_execution_commands_loop(self) -> None:
        interval_seconds = max(0.1, float(self.settings.execution_command_poll_interval_seconds))
        while True:
            try:
                if self.execution_command_processor is not None:
                    await self.execution_command_processor.process_pending()
            except Exception as exc:
                await self._record_background_failure(subsystem="execution_command_flow", exc=exc)
            await asyncio.sleep(interval_seconds)

    async def _monitor_phase1_shadow_loop(self) -> None:
        interval_seconds = max(
            1.0,
            min(self.settings.reconciliation_stale_after_seconds / 4.0, 5.0),
        )
        while True:
            try:
                await asyncio.to_thread(self._record_phase1_shadow_state)
            except Exception as exc:
                await self._record_background_failure(subsystem="phase1_shadow_monitor", exc=exc)
            await asyncio.sleep(interval_seconds)

    async def _monitor_trial_guard_loop(self) -> None:
        interval_seconds = max(1.0, float(self.settings.trial_guard_poll_interval_seconds))
        while True:
            try:
                if self.trial_guard_service is not None:
                    await asyncio.to_thread(self.trial_guard_service.evaluate_now)
            except Exception as exc:
                await self._record_background_failure(subsystem="trial_guard_monitor", exc=exc)
            await asyncio.sleep(interval_seconds)

    async def _record_background_failure(self, *, subsystem: str, exc: Exception) -> None:
        message = f"{subsystem}_failed: {exc}"
        log_event(
            self.logger,
            "background_loop_failed",
            level="error",
            subsystem=subsystem,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        error_type = type(exc).__name__
        await asyncio.to_thread(self._record_background_failure_sync, subsystem=subsystem, message=message, error_type=error_type)

    def _record_background_failure_sync(self, *, subsystem: str, message: str, error_type: str) -> None:
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
                    details={"error_type": error_type},
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
    if settings.operator_session_configured and not settings.operator_session_cookie_secure:
        raise ValueError(f"{error_prefix}_requires_secure_operator_session_cookie")


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


def _validate_exchange_position_mode_contract(
    *,
    settings: AATSSettings,
    snapshot,
) -> None:
    contract = derivatives_position_mode_contract(
        settings=settings,
        snapshot=snapshot,
    )
    startup_error_code = contract.get("startup_error_code")
    if startup_error_code not in {None, ""}:
        raise ValueError(str(startup_error_code))


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
        except Exception as exc:
            _log.warning("PortfolioBalanceDelta.model_validate failed, skipping event: %s", exc)
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
            sleeve_pnl_repo=InMemorySleevePnLRepository(),
            execution_repo=InMemoryExecutionRepository(),
            exit_execution_repo=InMemoryExitExecutionRepository(),
            obligation_repo=InMemoryExecutionObligationRepository(),
            outbox_repo=None,
            reconciliation_repo=InMemoryReconciliationRepository(),
            operator_repo=InMemoryOperatorUserRepository(),
            strategy_profile_repo=InMemoryStrategyProfileRepository(),
            strategy_sleeve_repo=InMemoryStrategySleeveRepository(),
            strategy_runtime_repo=InMemoryStrategyRuntimeRepository(),
            phase1_shadow=Phase1ShadowSubsystem(),
            funding_fee_repo=InMemoryFundingFeeRepository(),
        )

    if not settings.database_url:
        raise ValueError("AATS_DATABASE_URL must be configured when storage_mode=postgres")

    database_runtime = create_database_runtime(settings.database_url)
    if settings.database_auto_create_schema:
        create_schema(database_runtime)
    apply_current_migrations(database_runtime)
    validate_runtime_schema(database_runtime)
    if settings.database_single_runtime_guard_enabled:
        database_runtime.acquire_single_runtime_lock(
            scoped_runtime_lock_key(
                database_url=settings.database_url,
                base_lock_key=settings.database_runtime_lock_key,
            )
        )

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
    sleeve_pnl_repo = PostgresSleevePnLRepository(database_runtime.session_factory)
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
        sleeve_pnl_repo=sleeve_pnl_repo,
        execution_repo=execution_repo,
        exit_execution_repo=PostgresExitExecutionRepository(database_runtime.session_factory),
        obligation_repo=PostgresExecutionObligationRepository(database_runtime.session_factory),
        outbox_repo=PostgresOutboxRepository(database_runtime.session_factory),
        reconciliation_repo=PostgresReconciliationRepository(database_runtime.session_factory),
        operator_repo=PostgresOperatorUserRepository(database_runtime.session_factory),
        strategy_profile_repo=PostgresStrategyProfileRepository(database_runtime.session_factory),
        strategy_sleeve_repo=PostgresStrategySleeveRepository(database_runtime.session_factory),
        strategy_runtime_repo=PostgresStrategyRuntimeRepository(database_runtime.session_factory),
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
    settings: AATSSettings,
    mode_controller: RuntimeModeController,
    runtime_layering: RuntimeLayering,
    account_service: OKXAccountService,
    policy_engine: PolicyEngine,
    risk_engine: RiskEngine,
    execution_planner: ExecutionPlanner,
    market_gateway: MarketDataGateway,
    kill_switch: KillSwitch,
    metrics: MetricsRegistry,
    bus: InMemoryEventBus,
    event_store: EventStore,
    execution_repo: ExecutionRepository,
    strategy_runtime_repo: StrategyRuntimeRepository | None = None,
):
    def _exposure_side(quantity: Decimal) -> str:
        if quantity > Decimal("1e-12"):
            return "long"
        if quantity < Decimal("-1e-12"):
            return "short"
        return "flat"

    def _allocation_event_ref(*, allocation_id: str | None) -> str | None:
        if not allocation_id:
            return None
        scope = runtime_state_scope(settings)
        for event in event_store.by_topic_scoped(
            topics.PORTFOLIO_ALLOCATION_DECISIONS,
            scope=scope,
            limit=50,
        ):
            if str(event.payload.get("allocation_id") or "") == str(allocation_id):
                return event.event_id
        return None

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

    async def _publish_finalized_decision_outcome(
        *,
        target: PositionTarget,
        policy_decision: PolicyDecision,
        risk_decision: RiskDecision | None,
        execution_continues: bool,
        extra_blocked_reasons: list[str] | None = None,
    ) -> None:
        finalized_outcome = _finalize_decision_outcome(
            target=target,
            policy_decision=policy_decision,
            risk_decision=risk_decision,
            execution_continues=execution_continues,
            extra_blocked_reasons=extra_blocked_reasons,
        )
        if finalized_outcome is None:
            return
        outcome_envelope = await publish_model(
            bus=bus,
            topic=topics.DECISION_OUTCOMES,
            key=target.symbol,
            payload_model=finalized_outcome,
            source_component="decision_engine",
        )
        overlay_parent_record = overlay_parent_exposure_record(
            decision_id=finalized_outcome.decision_id,
            product_type=target.product_type,
            strategy_family=finalized_outcome.selected_strategy_family,
            strategy_sleeve_id=finalized_outcome.selected_strategy_sleeve_id,
            allocation_id=finalized_outcome.allocation_id,
            source_stage="decision_outcome",
            source_ref=outcome_envelope.event_id,
            parent_exposure=finalized_outcome.overlay_parent_exposure,
        )
        if overlay_parent_record is not None:
            await publish_model(
                bus=bus,
                topic=topics.OVERLAY_PARENT_EXPOSURES,
                key=target.symbol,
                payload_model=overlay_parent_record,
                source_component="decision_engine",
            )

    def _reference_price_for_target(target: PositionTarget) -> Decimal | None:
        target_reference_price = (
            abs(target.target_notional / target.target_position_qty)
            if abs(target.target_position_qty) > Decimal("1e-12")
            else None
        )
        current_reference_price = (
            abs(target.current_notional / target.current_position_qty)
            if abs(target.current_position_qty) > Decimal("1e-12")
            else None
        )
        return next(
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

    def _plan_for_target(
        *,
        target: PositionTarget,
        risk_decision: RiskDecision,
    ):
        reference_price = _reference_price_for_target(target)
        account_snapshot = account_service.latest_snapshot()
        account_configuration = None if account_snapshot is None else account_snapshot.account_configuration
        instrument_rule = account_service.instrument_metadata(target.symbol)
        return execution_planner.build_plan(
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
            instrument_rule=instrument_rule,
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
            strategy_family=target.strategy_family,
            strategy_sleeve_id=target.strategy_sleeve_id,
            allocation_id=target.allocation_id,
            strategy_bundle_id=target.strategy_bundle_id,
            strategy_pair_id=target.strategy_pair_id,
            strategy_opportunity_kind=target.strategy_opportunity_kind,
            strategy_execution_mode=target.strategy_execution_mode,
            strategy_state_phase=target.strategy_state_phase,
            ai_execution_parameter_suggestion=target.ai_execution_parameter_suggestion,
        )

    def _position_intent_for_target(*, current_qty: Decimal, target_qty: Decimal) -> str:
        if current_qty > Decimal("1e-12"):
            if target_qty > current_qty:
                return "scale_in_long"
            if target_qty > Decimal("1e-12"):
                return "reduce_long"
            if target_qty < Decimal("-1e-12"):
                return "reverse_to_short"
            return "close_long"
        if current_qty < Decimal("-1e-12"):
            if target_qty < current_qty:
                return "scale_in_short"
            if target_qty < Decimal("-1e-12"):
                return "reduce_short"
            if target_qty > Decimal("1e-12"):
                return "reverse_to_long"
            return "close_short"
        if target_qty > Decimal("1e-12"):
            return "open_long"
        if target_qty < Decimal("-1e-12"):
            return "open_short"
        return "hold"

    def _strategy_leg_target(*, base_target: PositionTarget, leg) -> PositionTarget:
        current_qty = to_decimal(leg.current_position_qty or Decimal("0"))
        target_qty = to_decimal(
            leg.target_position_qty if leg.target_position_qty is not None else current_qty
        )
        reference_price = to_decimal(leg.reference_price or Decimal("0"))
        return PositionTarget(
            decision_id=base_target.decision_id,
            symbol=leg.symbol,
            current_position_qty=current_qty,
            target_position_qty=target_qty,
            delta_position_qty=target_qty - current_qty,
            current_notional=abs(current_qty) * reference_price,
            target_notional=abs(target_qty) * reference_price,
            rebalance_reason=f"{base_target.strategy_family}_{leg.role}",
            urgency=base_target.urgency,
            max_slippage_tolerance_bps=base_target.max_slippage_tolerance_bps,
            source_mix={str(getattr(leg, "family", None) or base_target.strategy_family): 1.0},
            decision_expiry_ts=base_target.decision_expiry_ts,
            product_type=leg.product_type,
            current_exposure_side=_exposure_side(current_qty),
            target_exposure_side=_exposure_side(target_qty),
            position_intent=_position_intent_for_target(current_qty=current_qty, target_qty=target_qty),
            target_leverage=float(getattr(leg, "target_leverage", 1.0) or 1.0),
            margin_mode=getattr(leg, "margin_mode", "cash"),
            leverage_bias=base_target.leverage_bias,
            expected_signal_edge_bps=base_target.expected_signal_edge_bps,
            expected_cost_bps=base_target.expected_cost_bps,
            expected_net_edge_bps=base_target.expected_net_edge_bps,
            strategy_family=str(getattr(leg, "family", None) or base_target.strategy_family),
            strategy_sleeve_id=leg.strategy_sleeve_id or base_target.strategy_sleeve_id,
            strategy_route_action=base_target.strategy_route_action,
            strategy_pair_id=getattr(leg, "pair_id", None) or base_target.strategy_pair_id,
            strategy_opportunity_kind=getattr(leg, "opportunity_kind", None) or base_target.strategy_opportunity_kind,
            strategy_execution_mode=getattr(leg, "execution_mode", None) or base_target.strategy_execution_mode,
            strategy_state_phase=getattr(leg, "state_phase", None) or base_target.strategy_state_phase,
            strategy_reason_codes=list(base_target.strategy_reason_codes),
            strategy_blocking_reasons=list(base_target.strategy_blocking_reasons),
            strategy_headline=base_target.strategy_headline,
            allocation_id=leg.allocation_id or base_target.allocation_id,
            strategy_bundle_id=base_target.strategy_bundle_id,
            guardrail_flags=list(base_target.guardrail_flags),
            ai_execution_parameter_suggestion=base_target.ai_execution_parameter_suggestion,
            ai_decision_intent=base_target.ai_decision_intent,
            profile_control_decision=base_target.profile_control_decision,
        )

    def _strategy_leg_execution_semantics(*, leg) -> dict[str, Any] | None:
        pos_side = str(getattr(leg, "pos_side", "") or "").strip().lower()
        action = str(getattr(leg, "action", "") or "").strip().lower()
        if pos_side not in {"long", "short"} or action not in {"open", "reduce", "close"}:
            return None
        current_qty = to_decimal(getattr(leg, "current_position_qty", None) or Decimal("0"))
        target_qty = to_decimal(
            getattr(leg, "target_position_qty", None)
            if getattr(leg, "target_position_qty", None) is not None
            else current_qty
        )
        delta_qty = to_decimal(
            getattr(leg, "delta_position_qty", None)
            if getattr(leg, "delta_position_qty", None) is not None
            else target_qty - current_qty
        )
        quantity = abs(delta_qty)
        if quantity <= Decimal("1e-12"):
            return None
        side = str(getattr(leg, "side", "") or "").strip().lower()
        if side not in {"buy", "sell"}:
            opening = delta_qty > 0
            side = "buy" if (pos_side == "long" and opening) or (pos_side == "short" and not opening) else "sell"
        return {
            "pos_side": pos_side,
            "action": action,
            "side": side,
            "quantity": quantity,
            "position_intent": _position_intent_for_target(current_qty=current_qty, target_qty=target_qty),
        }

    def _plan_for_strategy_leg(
        *,
        base_target: PositionTarget,
        leg,
        leg_target: PositionTarget,
        policy_decision: PolicyDecision,
    ) -> dict[str, Any]:
        account_snapshot = account_service.latest_snapshot()
        account_configuration = None if account_snapshot is None else account_snapshot.account_configuration
        configured_position_mode = (
            "long_short_mode"
            if str(getattr(leg, "product_type", "") or "") == "derivatives"
            and str(settings.derivatives_position_mode or "").strip().lower() == "hedge"
            else None
        )
        position_mode = (
            configured_position_mode
            if account_configuration is None
            else (account_configuration.position_mode or configured_position_mode)
        )
        instrument_rule = account_service.instrument_metadata(leg.symbol)
        reference_price = next(
            (
                candidate
                for candidate in (
                    to_decimal(getattr(leg, "reference_price", None) or Decimal("0")),
                    _reference_price_for_target(leg_target),
                    market_gateway.latest_price(leg.symbol),
                )
                if candidate is not None and candidate > Decimal("1e-12")
            ),
            None,
        )
        semantics = _strategy_leg_execution_semantics(leg=leg)
        if (
            semantics is not None
            and str(getattr(leg, "product_type", "") or "") == "derivatives"
            and position_mode == "long_short_mode"
        ):
            provisional_plan = execution_planner.build_leg_plan(
                decision_id=base_target.decision_id,
                execution_chain_id=getattr(leg, "execution_chain_id", None),
                symbol=leg.symbol,
                side=semantics["side"],
                pos_side=semantics["pos_side"],
                action=semantics["action"],
                quantity=semantics["quantity"],
                urgency=getattr(leg, "execution_policy_urgency", None) or base_target.urgency,
                max_slippage_tolerance_bps=base_target.max_slippage_tolerance_bps,
                reference_price=reference_price,
                product_type=str(getattr(leg, "product_type", "derivatives") or "derivatives"),
                target_leverage=float(getattr(leg, "target_leverage", 1.0) or 1.0),
                margin_mode=str(getattr(leg, "margin_mode", "cross") or "cross"),
                position_mode=position_mode,
                instrument_rule=instrument_rule,
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
                td_mode=str(getattr(leg, "margin_mode", "cross") or "cross"),
                strategy_family=str(getattr(leg, "family", None) or base_target.strategy_family),
                strategy_sleeve_id=getattr(leg, "strategy_sleeve_id", None) or base_target.strategy_sleeve_id,
                allocation_id=getattr(leg, "allocation_id", None) or base_target.allocation_id,
                strategy_bundle_id=base_target.strategy_bundle_id,
                strategy_leg_role=getattr(leg, "role", None) or "primary",
                strategy_pair_id=getattr(leg, "pair_id", None) or base_target.strategy_pair_id,
                strategy_opportunity_kind=getattr(leg, "opportunity_kind", None) or base_target.strategy_opportunity_kind,
                strategy_execution_mode=getattr(leg, "execution_mode", None) or base_target.strategy_execution_mode,
                strategy_state_phase=getattr(leg, "state_phase", None) or base_target.strategy_state_phase,
                position_intent=semantics["position_intent"],
                execution_style_preference=getattr(leg, "execution_style_preference", None),
                order_type_preference=getattr(leg, "order_type_preference", None),
                time_in_force_preference=getattr(leg, "time_in_force_preference", None),
                limit_offset_bps_preference=getattr(leg, "limit_offset_bps_preference", None),
                ai_execution_parameter_suggestion=base_target.ai_execution_parameter_suggestion,
            )
            if provisional_plan is None:
                _log.warning(
                    "strategy_leg_plan skip: build_leg_plan 返回 None | decision=%s symbol=%s pos_side=%s action=%s qty=%s",
                    base_target.decision_id,
                    getattr(leg, "symbol", "?"),
                    getattr(semantics, "get", lambda k, d=None: semantics.get(k, d))("pos_side", "?") if isinstance(semantics, dict) else "?",
                    getattr(semantics, "get", lambda k, d=None: semantics.get(k, d))("action", "?") if isinstance(semantics, dict) else "?",
                    getattr(semantics, "get", lambda k, d=None: semantics.get(k, d))("quantity", "?") if isinstance(semantics, dict) else "?",
                )
                return {
                    "policy": policy_decision,
                    "risk": None,
                    "plan": None,
                    "intent": None,
                }
            provisional_intent = execution_planner.build_leg_intent(plan=provisional_plan)
            if provisional_intent is None:
                _log.warning(
                    "strategy_leg_intent skip: build_leg_intent 返回 None | decision=%s symbol=%s pos_side=%s qty=%s",
                    base_target.decision_id,
                    provisional_plan.symbol,
                    provisional_plan.pos_side,
                    provisional_plan.quantity,
                )
                return {
                    "policy": policy_decision,
                    "risk": None,
                    "plan": provisional_plan,
                    "intent": None,
                }
            risk_decision = risk_engine.evaluate_leg_order(provisional_intent)
            final_plan = provisional_plan.model_copy(
                update={
                    "required_initial_margin": risk_decision.required_initial_margin,
                    "projected_margin_usage": risk_decision.projected_margin_usage,
                    "projected_notional": risk_decision.projected_notional,
                    "risk_budget_multiplier": risk_decision.risk_budget_multiplier,
                    "risk_budget_state": dict(risk_decision.risk_budget_state),
                    "execution_aggressiveness_multiplier": risk_decision.execution_aggressiveness_multiplier,
                    "execution_aggressiveness_state": dict(risk_decision.execution_aggressiveness_state),
                    "only_reduce_required": risk_decision.only_reduce_required,
                    "risk_limit_breached": risk_decision.risk_limit_breached,
                    "liquidation_buffer_remaining": risk_decision.liquidation_buffer_remaining,
                }
            )
            final_intent = execution_planner.build_leg_intent(plan=final_plan)
            return {
                "policy": policy_decision,
                "risk": risk_decision,
                "plan": final_plan,
                "intent": final_intent,
            }

        risk_decision = risk_engine.evaluate(target=leg_target) if policy_decision.execution_allowed else None
        plan = None if risk_decision is None else _plan_for_target(target=leg_target, risk_decision=risk_decision)
        intent = None if plan is None else execution_planner.build_intent(plan=plan)
        return {
            "policy": policy_decision,
            "risk": risk_decision,
            "plan": plan,
            "intent": intent,
        }

    def _aggregate_policy_decision(
        *,
        target: PositionTarget,
        leg_results: list[dict[str, Any]],
        executed_leg_results: list[dict[str, Any]] | None = None,
        partial_execution_allowed: bool = False,
    ) -> PolicyDecision:
        effective_leg_results = (
            executed_leg_results
            if partial_execution_allowed and executed_leg_results
            else leg_results
        )
        rejection_reasons = list(
            dict.fromkeys(
                reason
                for item in effective_leg_results
                for reason in (item["policy"].rejection_reasons or [])
                if reason
            )
        )
        if partial_execution_allowed:
            execution_allowed = bool(executed_leg_results)
            submission_allowed = bool(executed_leg_results) and all(
                item["policy"].submission_allowed for item in (executed_leg_results or [])
            )
        else:
            execution_allowed = all(item["policy"].execution_allowed for item in leg_results)
            submission_allowed = all(item["policy"].submission_allowed for item in leg_results)
        dry_run_only = any(item["policy"].dry_run_only for item in effective_leg_results) and not submission_allowed
        return PolicyDecision(
            decision_id=target.decision_id,
            mode=mode_controller.mode,
            allowed=execution_allowed,
            execution_allowed=execution_allowed,
            submission_allowed=submission_allowed,
            dry_run_only=dry_run_only,
            requires_human_approval=any(
                item["policy"].requires_human_approval for item in effective_leg_results
            ),
            allowed_symbols=list(settings.expanded_allowed_symbols()),
            allowed_execution_styles=["market", "limit"],
            max_notional_override=settings.max_notional_per_symbol,
            forced_degrade_mode="paper_live" if dry_run_only else None,
            rejection_reasons=rejection_reasons,
        )

    def _independent_partial_execution_supported(
        *,
        target: PositionTarget,
        leg_results: list[dict[str, Any]],
    ) -> bool:
        leg_execution_modes = {
            str(getattr(item["leg"], "execution_mode", "") or "").strip().lower()
            for item in leg_results
        }
        leg_overlay_modes = {
            str(getattr(item["leg"], "overlay_mode", "") or "").strip().lower()
            for item in leg_results
        }
        return len(leg_results) > 1 and (
            str(target.strategy_execution_mode or "").strip().lower() == "independent_books"
            or any(mode.startswith("independent_") for mode in leg_execution_modes)
            or "independent" in leg_overlay_modes
        )

    def _leg_result_execution_allowed(item: dict[str, Any]) -> bool:
        policy_decision = item["policy"]
        risk_decision = item["risk"]
        if not policy_decision.execution_allowed:
            return False
        if risk_decision is None or not risk_decision.approved or risk_decision.halt_required:
            return False
        if item.get("plan") is None:
            return False
        return True

    def _independent_leg_priority(
        *,
        target: PositionTarget,
        item: dict[str, Any],
        original_index: int,
    ) -> tuple[int, float, int]:
        leg = item["leg"]
        action = str(getattr(leg, "action", "") or "").strip().lower()
        if action in {"close", "reduce"}:
            return (0, 0.0, original_index)
        overlay_decision = target.hedge_overlay_decision
        if str(getattr(leg, "pos_side", "") or "").strip().lower() == "short":
            score = float(getattr(overlay_decision, "short_leg_score", 0.0) or 0.0)
        else:
            score = float(getattr(overlay_decision, "long_leg_score", 0.0) or 0.0)
        return (1, -score, original_index)

    def _apply_bundle_risk_rejection(
        *,
        item: dict[str, Any],
        rejection_reasons: list[str],
    ) -> None:
        unique_reasons = list(dict.fromkeys(item for item in rejection_reasons if item))
        leg = item["leg"]
        item["leg"] = leg.model_copy(
            update={
                "risk_approved": False,
                "risk_rejection_reasons": list(
                    dict.fromkeys(
                        [
                            *(leg.risk_rejection_reasons or []),
                            *unique_reasons,
                        ]
                    )
                ),
                "risk_constraints_applied": list(
                    dict.fromkeys(
                        [
                            *(leg.risk_constraints_applied or []),
                            "bundle_leg_risk_constraints_applied",
                            *unique_reasons,
                        ]
                    )
                ),
            }
        )

    def _select_bundle_safe_leg_subset(
        *,
        target: PositionTarget,
        candidate_leg_results: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], RiskDecision | None, list[str]]:
        accepted: list[dict[str, Any]] = []
        accepted_bundle_risk: RiskDecision | None = None
        blocked_reasons: list[str] = []
        ordered_candidates = sorted(
            enumerate(candidate_leg_results),
            key=lambda pair: _independent_leg_priority(
                target=target,
                item=pair[1],
                original_index=pair[0],
            ),
        )
        for _index, item in ordered_candidates:
            candidate_intents = [accepted_item["intent"] for accepted_item in accepted]
            intent = item.get("intent")
            if intent is None:
                continue
            candidate_intents.append(intent)
            candidate_bundle_risk = risk_engine.evaluate_leg_order_bundle(candidate_intents)
            if candidate_bundle_risk.approved and not candidate_bundle_risk.halt_required:
                accepted.append(item)
                accepted_bundle_risk = candidate_bundle_risk
                continue
            reasons = list(candidate_bundle_risk.rejection_reasons or ["bundle_leg_risk_constraints_applied"])
            blocked_reasons.extend(reasons)
            _apply_bundle_risk_rejection(item=item, rejection_reasons=reasons)
        return accepted, accepted_bundle_risk, list(dict.fromkeys(item for item in blocked_reasons if item))

    def _effective_bundle_target_qty(
        *,
        target: PositionTarget,
        executed_leg_results: list[dict[str, Any]],
    ) -> Decimal:
        return to_decimal(target.current_position_qty) + sum(
            (
                to_decimal(item["leg"].delta_position_qty or Decimal("0"))
                for item in executed_leg_results
            ),
            start=Decimal("0"),
        )

    def _aggregate_risk_decision(
        *,
        target: PositionTarget,
        leg_results: list[dict[str, Any]],
        executed_leg_results: list[dict[str, Any]] | None = None,
        partial_execution_allowed: bool = False,
        bundle_risk_decision: RiskDecision | None = None,
    ) -> RiskDecision:
        effective_leg_results = (
            executed_leg_results
            if partial_execution_allowed and executed_leg_results
            else leg_results
        )
        evaluated_risks = [item["risk"] for item in effective_leg_results if item["risk"] is not None]
        capped_notional = sum(
            (
                to_decimal(risk.capped_target_notional or Decimal("0"))
                for risk in evaluated_risks
            ),
            start=Decimal("0"),
        )
        required_initial_margin = sum(
            (
                to_decimal(risk.required_initial_margin or Decimal("0"))
                for risk in evaluated_risks
            ),
            start=Decimal("0"),
        )
        projected_notional = sum(
            (
                to_decimal(risk.projected_notional or Decimal("0"))
                for risk in evaluated_risks
            ),
            start=Decimal("0"),
        )
        projected_margin_usage_candidates = [
            to_decimal(risk.projected_margin_usage)
            for risk in evaluated_risks
            if risk.projected_margin_usage is not None
        ]
        liquidation_buffer_candidates = [
            to_decimal(risk.liquidation_buffer_remaining)
            for risk in evaluated_risks
            if risk.liquidation_buffer_remaining is not None
        ]
        rejection_reasons = list(
            dict.fromkeys(
                reason
                for risk in evaluated_risks
                for reason in (risk.rejection_reasons or [])
                if reason
            )
        )
        constraints_applied = list(
            dict.fromkeys(
                reason
                for risk in evaluated_risks
                for reason in (risk.constraints_applied or [])
                if reason
            )
        )
        risk_budget_multiplier = min(
            (to_decimal(risk.risk_budget_multiplier) for risk in evaluated_risks),
            default=Decimal("1"),
        )
        execution_aggressiveness_multiplier = min(
            (to_decimal(risk.execution_aggressiveness_multiplier) for risk in evaluated_risks),
            default=Decimal("1"),
        )
        if bundle_risk_decision is not None:
            capped_notional = to_decimal(bundle_risk_decision.capped_target_notional or capped_notional)
            required_initial_margin = to_decimal(bundle_risk_decision.required_initial_margin or required_initial_margin)
            projected_notional = to_decimal(bundle_risk_decision.projected_notional or projected_notional)
            projected_margin_usage_candidates = (
                []
                if bundle_risk_decision.projected_margin_usage is None
                else [to_decimal(bundle_risk_decision.projected_margin_usage)]
            )
            liquidation_buffer_candidates = (
                []
                if bundle_risk_decision.liquidation_buffer_remaining is None
                else [to_decimal(bundle_risk_decision.liquidation_buffer_remaining)]
            )
            rejection_reasons = list(
                dict.fromkeys(
                    [
                        *(bundle_risk_decision.rejection_reasons or []),
                        *rejection_reasons,
                    ]
                )
            )
            constraints_applied = list(
                dict.fromkeys(
                    [
                        *(bundle_risk_decision.constraints_applied or []),
                        *constraints_applied,
                    ]
                )
            )
            risk_budget_multiplier = min(
                risk_budget_multiplier,
                to_decimal(bundle_risk_decision.risk_budget_multiplier),
            )
            execution_aggressiveness_multiplier = min(
                execution_aggressiveness_multiplier,
                to_decimal(bundle_risk_decision.execution_aggressiveness_multiplier),
            )
        partial_leg_execution = (
            partial_execution_allowed
            and bool(executed_leg_results)
            and len(executed_leg_results or []) < len(leg_results)
        )
        if partial_leg_execution:
            constraints_applied = list(
                dict.fromkeys(
                    [
                        *constraints_applied,
                        "strategy_bundle_partial_leg_execution",
                    ]
                )
            )
        approved = len(evaluated_risks) == len(effective_leg_results) and bool(effective_leg_results) and all(
            risk.approved for risk in evaluated_risks
        )
        if bundle_risk_decision is not None:
            approved = approved and bundle_risk_decision.approved
        return RiskDecision(
            decision_id=target.decision_id,
            approved=approved,
            modified=partial_leg_execution
            or any(risk.modified for risk in evaluated_risks)
            or (bundle_risk_decision.modified if bundle_risk_decision is not None else False),
            capped_target_position_qty=(
                _effective_bundle_target_qty(target=target, executed_leg_results=executed_leg_results or [])
                if partial_execution_allowed and executed_leg_results
                else (
                    bundle_risk_decision.capped_target_position_qty
                    if bundle_risk_decision is not None
                    else target.target_position_qty
                )
            ),
            capped_target_notional=capped_notional,
            required_initial_margin=required_initial_margin,
            projected_margin_usage=max(projected_margin_usage_candidates, default=None),
            projected_notional=projected_notional,
            current_open_order_count=(
                bundle_risk_decision.current_open_order_count
                if bundle_risk_decision is not None
                else max((risk.current_open_order_count for risk in evaluated_risks), default=0)
            ),
            risk_budget_multiplier=risk_budget_multiplier,
            risk_budget_state={"bundle": True},
            execution_aggressiveness_multiplier=execution_aggressiveness_multiplier,
            execution_aggressiveness_state={"bundle": True},
            constraints_applied=constraints_applied,
            risk_score=max(
                [
                    *(float(risk.risk_score) for risk in evaluated_risks),
                    *(
                        []
                        if bundle_risk_decision is None
                        else [float(bundle_risk_decision.risk_score)]
                    ),
                ],
                default=0.0,
            ),
            flatten_required=any(risk.flatten_required for risk in evaluated_risks) or (
                bundle_risk_decision.flatten_required if bundle_risk_decision is not None else False
            ),
            halt_required=any(risk.halt_required for risk in evaluated_risks) or (
                bundle_risk_decision.halt_required if bundle_risk_decision is not None else False
            ),
            only_reduce_required=any(risk.only_reduce_required for risk in evaluated_risks) or (
                bundle_risk_decision.only_reduce_required if bundle_risk_decision is not None else False
            ),
            leg_only_reduce_constraints=(
                list(bundle_risk_decision.leg_only_reduce_constraints)
                if bundle_risk_decision is not None and bundle_risk_decision.leg_only_reduce_constraints
                else [
                    constraint
                    for risk in evaluated_risks
                    for constraint in getattr(risk, "leg_only_reduce_constraints", [])
                ]
            ),
            risk_limit_breached=any(risk.risk_limit_breached for risk in evaluated_risks) or (
                bundle_risk_decision.risk_limit_breached if bundle_risk_decision is not None else False
            ),
            liquidation_buffer_remaining=min(liquidation_buffer_candidates, default=None),
            current_derivatives_exposure=(
                bundle_risk_decision.current_derivatives_exposure
                if bundle_risk_decision is not None
                else None
            ),
            projected_derivatives_exposure=(
                bundle_risk_decision.projected_derivatives_exposure
                if bundle_risk_decision is not None
                else None
            ),
            derivatives_exposure_limits=(
                bundle_risk_decision.derivatives_exposure_limits
                if bundle_risk_decision is not None
                else None
            ),
            rejection_reasons=rejection_reasons,
        )

    async def handle_position_target(message: dict[str, Any]) -> None:
        target = parse_payload(message, PositionTarget)
        if runtime_layering.environment_capabilities.account_state_source_kind == "exchange":
            await account_service.refresh()

        if target.strategy_execution_legs:
            leg_results: list[dict[str, Any]] = []
            synthetic_current_by_leg_key: dict[tuple[str, str, str, str], Decimal] = {}
            for raw_leg in target.strategy_execution_legs:
                leg_key = (
                    raw_leg.symbol,
                    str(raw_leg.product_type),
                    str(raw_leg.margin_mode),
                    str(getattr(raw_leg, "pos_side", "") or "").strip().lower(),
                )
                current_qty = to_decimal(
                    synthetic_current_by_leg_key.get(
                        leg_key,
                        to_decimal(raw_leg.current_position_qty or Decimal("0")),
                    )
                )
                if raw_leg.delta_position_qty is not None:
                    delta_qty = to_decimal(raw_leg.delta_position_qty)
                else:
                    delta_qty = to_decimal(raw_leg.target_position_qty or current_qty) - current_qty
                target_qty = current_qty + delta_qty
                leg = raw_leg.model_copy(
                    update={
                        "current_position_qty": current_qty,
                        "target_position_qty": target_qty,
                        "delta_position_qty": delta_qty,
                    }
                )
                synthetic_current_by_leg_key[leg_key] = target_qty
                leg_target = _strategy_leg_target(base_target=target, leg=leg)
                policy_decision = policy_engine.evaluate(target=leg_target)
                execution_preview = _plan_for_strategy_leg(
                    base_target=target,
                    leg=leg,
                    leg_target=leg_target,
                    policy_decision=policy_decision,
                )
                risk_decision = execution_preview["risk"]
                leg_results.append(
                    {
                        "leg": leg.model_copy(
                            update={
                                "policy_allowed": policy_decision.execution_allowed,
                                "policy_rejection_reasons": list(policy_decision.rejection_reasons or []),
                                "risk_approved": None if risk_decision is None else risk_decision.approved,
                                "risk_rejection_reasons": [] if risk_decision is None else list(risk_decision.rejection_reasons or []),
                                "risk_constraints_applied": [] if risk_decision is None else list(risk_decision.constraints_applied or []),
                            }
                        ),
                        "target": leg_target,
                        "policy": policy_decision,
                        "risk": risk_decision,
                        "plan": execution_preview["plan"],
                        "intent": execution_preview["intent"],
                    }
                )

            partial_execution_allowed = _independent_partial_execution_supported(
                target=target,
                leg_results=leg_results,
            )
            executable_leg_results = (
                [item for item in leg_results if _leg_result_execution_allowed(item)]
                if partial_execution_allowed
                else []
            )
            bundle_risk_decision = None
            bundle_partial_block_reasons: list[str] = []
            if partial_execution_allowed and executable_leg_results:
                bundle_leg_intents = []
                for item in executable_leg_results:
                    intent = item.get("intent")
                    if intent is None:
                        plan = item.get("plan")
                        if plan is None:
                            _leg = item.get("leg")
                            _log.warning(
                                "bundle_leg skip: plan 和 intent 均为 None | decision=%s symbol=%s pos_side=%s",
                                target.decision_id,
                                getattr(_leg, "symbol", "?") if _leg else "?",
                                getattr(_leg, "pos_side", "?") if _leg else "?",
                            )
                            continue
                        intent = (
                            execution_planner.build_leg_intent(plan=plan)
                            if hasattr(plan, "leg_intent_id")
                            else execution_planner.build_intent(plan=plan)
                        )
                        item["intent"] = intent
                    if intent is not None:
                        bundle_leg_intents.append(intent)
                bundle_risk_decision = risk_engine.evaluate_leg_order_bundle(
                    bundle_leg_intents
                )
                if not bundle_risk_decision.approved or bundle_risk_decision.halt_required:
                    (
                        executable_leg_results,
                        accepted_bundle_risk_decision,
                        bundle_partial_block_reasons,
                    ) = _select_bundle_safe_leg_subset(
                        target=target,
                        candidate_leg_results=executable_leg_results,
                    )
                    if executable_leg_results and accepted_bundle_risk_decision is not None:
                        bundle_risk_decision = accepted_bundle_risk_decision
                    else:
                        executable_leg_results = []
            aggregate_policy = _aggregate_policy_decision(
                target=target,
                leg_results=leg_results,
                executed_leg_results=executable_leg_results,
                partial_execution_allowed=partial_execution_allowed,
            )
            await publish_model(
                bus=bus,
                topic=topics.POLICY_DECISIONS,
                key=target.symbol,
                payload_model=aggregate_policy,
                source_component="governance_engine",
            )
            aggregate_risk = _aggregate_risk_decision(
                target=target,
                leg_results=leg_results,
                executed_leg_results=executable_leg_results,
                partial_execution_allowed=partial_execution_allowed,
                bundle_risk_decision=bundle_risk_decision,
            )
            await publish_model(
                bus=bus,
                topic=topics.RISK_DECISIONS,
                key=target.symbol,
                payload_model=aggregate_risk,
                source_component="governance_engine",
            )
            extra_blocked_reasons: list[str] = []
            if kill_switch.halted:
                extra_blocked_reasons.append("kill_switch_active")
            if (
                partial_execution_allowed
                and executable_leg_results
                and len(executable_leg_results) < len(leg_results)
            ):
                extra_blocked_reasons.append("strategy_bundle_partial_leg_execution")
                extra_blocked_reasons.extend(bundle_partial_block_reasons)
            bundle_execution_allowed = (
                aggregate_policy.execution_allowed
                and aggregate_risk.approved
                and not aggregate_risk.halt_required
                and not kill_switch.halted
            )
            published_legs = [item["leg"] for item in leg_results]
            execution_plan_refs: list[str] = []
            order_intent_refs: list[str] = []
            bundle_status = "blocked"
            if bundle_execution_allowed:
                execution_leg_results = executable_leg_results if partial_execution_allowed else leg_results
                for item in execution_leg_results:
                    risk_decision = item["risk"]
                    if risk_decision is None:
                        continue
                    plan = item.get("plan")
                    if plan is None:
                        continue
                    plan = plan.model_copy(
                        update={
                            "strategy_leg_role": item["leg"].role,
                            "strategy_sleeve_id": item["leg"].strategy_sleeve_id or target.strategy_sleeve_id,
                            "allocation_id": item["leg"].allocation_id or target.allocation_id,
                        }
                    )
                    plan_envelope = await publish_model(
                        bus=bus,
                        topic=topics.EXECUTION_PLANS,
                        key=plan.symbol,
                        payload_model=plan,
                        source_component="execution_engine",
                    )
                    execution_plan_refs.append(plan_envelope.event_id)
                    intent = item.get("intent")
                    if intent is None and hasattr(plan, "leg_intent_id"):
                        intent = execution_planner.build_leg_intent(plan=plan)
                    elif intent is None:
                        intent = execution_planner.build_intent(plan=plan)
                    if intent is None:
                        item["leg"] = item["leg"].model_copy(update={"execution_plan_ref": plan_envelope.event_id})
                        continue
                    intent = intent.model_copy(
                        update={
                            "strategy_leg_role": item["leg"].role,
                            "strategy_sleeve_id": item["leg"].strategy_sleeve_id or target.strategy_sleeve_id,
                            "allocation_id": item["leg"].allocation_id or target.allocation_id,
                        }
                    )
                    publish_intent = (
                        order_intent_from_leg_order_intent(intent)
                        if isinstance(intent, LegOrderIntent)
                        else intent
                    )
                    intent_envelope = await publish_model(
                        bus=bus,
                        topic=topics.ORDER_INTENTS,
                        key=publish_intent.symbol,
                        payload_model=publish_intent,
                        source_component="execution_engine",
                    )
                    metrics.increment("order_intents_generated")
                    order_intent_refs.append(intent_envelope.event_id)
                    item["leg"] = item["leg"].model_copy(
                        update={
                            "execution_plan_ref": plan_envelope.event_id,
                            "order_intent_ref": intent_envelope.event_id,
                        }
                    )
                published_legs = [item["leg"] for item in leg_results]
                if "smart_arbitrage_partial_fill_recovery" in target.strategy_reason_codes:
                    bundle_status = "partial_fill_recovery"
                else:
                    bundle_status = "submitted"
                bundle_order_states = [
                    state
                    for state in execution_repo.order_states()
                    if str(state.strategy_bundle_id or "").strip() == str(target.strategy_bundle_id or "").strip()
                ]
                bundle_status = derive_strategy_bundle_status(
                    order_states=bundle_order_states,
                    previous_status=bundle_status,
                )

            allocation_decision = (
                None
                if strategy_runtime_repo is None or target.allocation_id is None
                else strategy_runtime_repo.get_allocation_decision(target.allocation_id)
            )
            bundle_type = "single_sleeve"
            if target.strategy_family == "smart_arbitrage" and len(published_legs) >= 2:
                bundle_type = "hedge_protected"
            elif allocation_decision is not None:
                if (
                    to_decimal(allocation_decision.hedge_protected_notional) > Decimal("0")
                    or to_decimal(allocation_decision.directional_reduced_notional) > Decimal("0")
                ):
                    bundle_type = "hedge_protected"
                elif len(allocation_decision.approved_families) > 1:
                    bundle_type = "multi_sleeve"
            bundle_priority = "standard"
            if allocation_decision is not None and allocation_decision.budget_snapshots:
                min_priority_rank = min(item.priority_rank for item in allocation_decision.budget_snapshots)
                if min_priority_rank <= 0:
                    bundle_priority = "critical_hedge"
                elif min_priority_rank == 1:
                    bundle_priority = "hedge"
                elif min_priority_rank == 2:
                    bundle_priority = "inventory"
            bundle = StrategyExecutionBundle(
                bundle_id=target.strategy_bundle_id or new_id("bundle"),
                decision_id=target.decision_id,
                family=target.strategy_family,
                participating_families=list(
                    dict.fromkeys(
                        [
                            target.strategy_family,
                            *(
                                str(getattr(leg, "family", "") or "")
                                for leg in published_legs
                                if str(getattr(leg, "family", "") or "")
                            ),
                        ]
                    )
                ),
                strategy_sleeve_id=target.strategy_sleeve_id,
                strategy_sleeve_refs=list(
                    dict.fromkeys(
                        [
                            target.strategy_sleeve_id,
                            *(
                                str(getattr(leg, "strategy_sleeve_id", "") or "")
                                for leg in published_legs
                            ),
                        ]
                    )
                ),
                allocation_id=target.allocation_id,
                product_type=target.product_type,
                margin_mode=target.margin_mode,
                allowed_symbols=settings.expanded_allowed_symbols(),
                route_action=target.strategy_route_action,
                bundle_type=bundle_type,
                bundle_priority=bundle_priority,
                status=bundle_status,
                selected_symbol=target.symbol,
                operator_summary=target.strategy_headline,
                reason_codes=list(
                    dict.fromkeys(
                        apply_strategy_bundle_status_reason_codes(
                            reason_codes=[
                                *target.strategy_reason_codes,
                                *(aggregate_policy.rejection_reasons or []),
                                *(aggregate_risk.rejection_reasons or []),
                                *(extra_blocked_reasons or []),
                            ],
                            status=bundle_status,
                        )
                    )
                ),
                gross_requested_exposure=(
                    Decimal("0")
                    if allocation_decision is None
                    else to_decimal(allocation_decision.portfolio_requested_notional)
                ),
                net_approved_exposure=(
                    Decimal("0")
                    if allocation_decision is None
                    else to_decimal(allocation_decision.portfolio_approved_notional)
                ),
                expected_cost_bps=None if allocation_decision is None else allocation_decision.expected_cost_bps,
                expected_edge_bps=None if allocation_decision is None else allocation_decision.expected_edge_bps,
                budget_snapshot_ids=[] if allocation_decision is None else list(allocation_decision.budget_snapshot_ids),
                allocation_snapshot_ref=_allocation_event_ref(allocation_id=target.allocation_id),
                portfolio_risk_budget_state=(
                    None if allocation_decision is None else allocation_decision.portfolio_risk_budget_state
                ),
                hedge_protected_notional=(
                    Decimal("0")
                    if allocation_decision is None
                    else to_decimal(allocation_decision.hedge_protected_notional)
                ),
                directional_reduced_notional=(
                    Decimal("0")
                    if allocation_decision is None
                    else to_decimal(allocation_decision.directional_reduced_notional)
                ),
                legs=published_legs,
                execution_plan_refs=execution_plan_refs,
                order_intent_refs=order_intent_refs,
            )
            await publish_model(
                bus=bus,
                topic=topics.STRATEGY_EXECUTION_BUNDLES,
                key=target.symbol,
                payload_model=bundle,
                source_component="decision_engine",
            )
            if strategy_runtime_repo is not None:
                strategy_runtime_repo.save_execution_bundle(bundle)
            await _publish_finalized_decision_outcome(
                target=target,
                policy_decision=aggregate_policy,
                risk_decision=aggregate_risk,
                execution_continues=bundle_execution_allowed,
                extra_blocked_reasons=extra_blocked_reasons,
            )
            return

        policy_decision = policy_engine.evaluate(target=target)
        await policy_engine.publish_decision(bus=bus, target=target, decision=policy_decision)
        if not policy_decision.execution_allowed:
            await _publish_finalized_decision_outcome(
                target=target,
                policy_decision=policy_decision,
                risk_decision=None,
                execution_continues=False,
            )
            return

        risk_decision = risk_engine.evaluate(target=target)
        await risk_engine.publish_decision(bus=bus, target=target, decision=risk_decision)
        if not risk_decision.approved or risk_decision.halt_required:
            await _publish_finalized_decision_outcome(
                target=target,
                policy_decision=policy_decision,
                risk_decision=risk_decision,
                execution_continues=False,
            )
            return
        if kill_switch.halted:
            await _publish_finalized_decision_outcome(
                target=target,
                policy_decision=policy_decision,
                risk_decision=risk_decision,
                execution_continues=False,
                extra_blocked_reasons=["kill_switch_active"],
            )
            return

        await _publish_finalized_decision_outcome(
            target=target,
            policy_decision=policy_decision,
            risk_decision=risk_decision,
            execution_continues=True,
        )

        plan = _plan_for_target(target=target, risk_decision=risk_decision)
        if plan is None:
            _log.warning(
                "position_target skip: plan 为 None | decision=%s symbol=%s product=%s target_qty=%s current_qty=%s",
                target.decision_id, target.symbol, target.product_type,
                target.target_position_qty, target.current_position_qty,
            )
            return
        await execution_planner.publish_plan(bus=bus, plan=plan)

        intent = execution_planner.build_intent(plan=plan)
        if intent is None:
            _log.warning(
                "position_target skip: intent 为 None | decision=%s symbol=%s delta=%s product=%s position_mode=%s",
                plan.decision_id, plan.symbol, plan.delta_qty, plan.product_type, plan.position_mode,
            )
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
            raise_on_error=False,
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
        ObserverSubscriptionSpec(
            topics.STRATEGY_COORDINATOR_SNAPSHOTS,
            "audit.handle_strategy_coordinator_snapshot",
            audit_service.handle_strategy_coordinator_snapshot,
        ),
        ObserverSubscriptionSpec(
            topics.STRATEGY_SLEEVE_INTENTS,
            "audit.handle_strategy_sleeve_intent",
            audit_service.handle_strategy_sleeve_intent,
        ),
        ObserverSubscriptionSpec(
            topics.PORTFOLIO_ALLOCATION_DECISIONS,
            "audit.handle_portfolio_allocation_decision",
            audit_service.handle_portfolio_allocation_decision,
        ),
        ObserverSubscriptionSpec(topics.POSITION_TARGETS, "audit.handle_position_target", audit_service.handle_position_target),
        ObserverSubscriptionSpec(topics.DECISION_OUTCOMES, "audit.handle_decision_outcome", audit_service.handle_decision_outcome),
        ObserverSubscriptionSpec(topics.POLICY_DECISIONS, "audit.handle_policy_decision", audit_service.handle_policy_decision),
        ObserverSubscriptionSpec(topics.RISK_DECISIONS, "audit.handle_risk_decision", audit_service.handle_risk_decision),
        ObserverSubscriptionSpec(topics.EXECUTION_PLANS, "audit.handle_execution_plan", audit_service.handle_execution_plan),
        ObserverSubscriptionSpec(
            topics.STRATEGY_EXECUTION_BUNDLES,
            "audit.handle_strategy_execution_bundle",
            audit_service.handle_strategy_execution_bundle,
        ),
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
                raise_on_error=False,
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
        # ── Settings Provenance 追踪 ─────────────────────────────
        from aats.bootstrap.settings_provenance import SettingsProvenanceTracker
        _provenance = SettingsProvenanceTracker()
        # 补充 load_settings 内部分层（仅当 settings 来自 load_settings 时）
        _layers = _load_settings_layers if settings is None else {}
        if _layers.get("yaml_or_managed"):
            # hardcoded defaults — 使用 model_validate({}) 隔离环境变量
            _defaults_baseline = AATSSettings.model_validate({}).model_dump(mode="python")
            _provenance.snapshot("hardcoded_defaults", _defaults_baseline)
            # YAML / managed profile 层
            _yaml_merged = {**_defaults_baseline, **_layers["yaml_or_managed"]}
            _provenance.snapshot("strategy_profile", _yaml_merged)
            # env overrides 层
            if _layers.get("env_overrides"):
                _env_merged = {**_yaml_merged, **_layers["env_overrides"]}
                _provenance.snapshot("env_overrides", _env_merged)
        _provenance.snapshot("load_settings", base_settings.model_dump(mode="python"))

        profile_resolution = runtime_profile_resolution(settings=base_settings)
        # ── Active Parameter Set 注入（RDP 整合） ──────────────────
        # 在 profile resolution 之后、settings validate 之前合并。
        # fail-soft: 加载失败不阻断主系统启动。
        _resolved_for_active = profile_resolution.resolved_settings
        _provenance.snapshot("profile_resolution", _resolved_for_active)
        try:
            from aats.bootstrap.active_parameters import apply_active_parameters_to_settings
            _resolved_for_active = apply_active_parameters_to_settings(
                profile_resolution.resolved_settings,
                project_root=Path.cwd(),
            )
            _provenance.snapshot("active_parameters", _resolved_for_active)
        except Exception as _active_param_exc:
            log_event(
                get_logger("aats.bootstrap"),
                "active_parameter_load_failed",
                level="warning",
                error=str(_active_param_exc),
            )
        runtime_settings = AATSSettings.model_validate(_resolved_for_active)
        _provenance.snapshot("final", runtime_settings.model_dump(mode="python"))
        # ── 输出 Provenance 报告 ─────────────────────────────────
        try:
            _provenance.log_report()
            _provenance.log_active_parameter_details()
        except Exception as _prov_exc:
            log_event(
                get_logger("aats.bootstrap"),
                "settings_provenance_report_failed",
                level="warning",
                error=str(_prov_exc),
            )
        runtime_layering = resolve_runtime_layering(runtime_settings)
        state_scope = runtime_state_scope(runtime_settings)
        _validate_runtime_settings(runtime_settings, runtime_layering)
        _validate_operator_auth_settings(runtime_settings, storage)
        seed_strategy_profiles(settings=runtime_settings, repo=storage.strategy_profile_repo)
        entry_execution_guard = non_protective_entry_execution_guard(runtime_settings)
        if entry_execution_guard.get("active"):
            log_event(
                get_logger("aats.bootstrap"),
                "startup_entry_execution_guard_active",
                level="warning",
                warning_code=entry_execution_guard.get("warning_code"),
                status=entry_execution_guard.get("status"),
                summary=entry_execution_guard.get("summary"),
                operator_summary=entry_execution_guard.get("operator_summary"),
            )
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
    baseline_import_service = AccountBaselineImportService(
        event_store=storage.event_store,
        reconciliation_repo=storage.reconciliation_repo,
    )
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
    strategy_coordinator = StrategyCoordinatorService(
        settings=runtime_settings,
        event_store=storage.event_store,
        market_gateway=market_gateway,
        portfolio_repo=storage.portfolio_repo,
        execution_repo=storage.execution_repo,
        position_lot_repo=storage.position_lot_repo,
        account_service=account_service,
        strategy_sleeve_repo=storage.strategy_sleeve_repo,
        strategy_runtime_repo=storage.strategy_runtime_repo,
        reconciliation_repo=storage.reconciliation_repo,
        sleeve_pnl_repo=storage.sleeve_pnl_repo,
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
        strategy_coordinator=strategy_coordinator,
        metrics=metrics,
    )
    decision_trigger = DecisionCycleTrigger(
        orchestrator=decision_engine,
        market_gateway=market_gateway,
        policy=decision_trigger_policy,
        can_trigger=lambda *, symbol: (
            False,
            "kill_switch_active",
        )
        if kill_switch.halted
        else (
            True,
            "ready",
        )
        if runtime_settings.symbol_allowed_for_decision_cycle(symbol)
        else (
            False,
            "symbol_not_enabled_for_decision_cycle",
        ),
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
    portfolio_outbox_publisher = None
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
            execution_command_repo=(
                storage.execution_command_repo
                if isinstance(storage.execution_command_repo, PostgresExecutionCommandRepository)
                else None
            ),
            execution_order_repo=storage.execution_order_repo,
            execution_order_history_repo=storage.execution_order_history_repo,
            execution_fill_repo=storage.execution_fill_repo_v2,
        )
    if (
        storage.database_runtime is not None
        and isinstance(storage.event_store, PostgresEventStore)
        and storage.outbox_repo is not None
        and isinstance(storage.portfolio_repo, PostgresPortfolioRepository)
        and isinstance(storage.fill_outcome_repo, PostgresFillOutcomeRepository)
    ):
        portfolio_outbox_publisher = PostgresPortfolioOutboxPublisher(
            session_factory=storage.database_runtime.session_factory,
            event_store=storage.event_store,
            outbox_repo=storage.outbox_repo,
            bus=bus,
            portfolio_repo=storage.portfolio_repo,
            fill_outcome_repo=storage.fill_outcome_repo,
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
        exit_execution_repo=storage.exit_execution_repo,
        obligation_service=obligation_service,
        execution_outbox_publisher=execution_outbox_publisher,
        persistent_order_service=execution_order_service,
        shadow_execution_service=storage.phase1_execution_shadow_service,
        shadow_execution_order_repo=storage.execution_order_repo,
        shadow_execution_order_history_repo=storage.execution_order_history_repo,
        shadow_execution_fill_repo=storage.execution_fill_repo_v2,
        shadow_ledger_mirror_service=storage.phase1_ledger_mirror_service,
        leg_risk_evaluator=(
            risk_engine.evaluate_leg_order
            if runtime_settings.trading_product_type == "derivatives"
            and runtime_settings.derivatives_position_mode == "hedge"
            else None
        ),
        strategy_runtime_repo=storage.strategy_runtime_repo,
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
            can_execute_command=lambda command: (
                str(command.get("command_type") or "").lower() != "submit"
                or not kill_switch.halted
            ),
            sent_retry_after_seconds=runtime_settings.execution_command_sent_retry_after_seconds,
        )

    portfolio_state = PortfolioState(
        initial_usdt_balance=runtime_settings.initial_usdt_balance,
        default_product_type=runtime_settings.trading_product_type,
        default_margin_mode=runtime_settings.margin_mode,
    )
    sleeve_pnl_projection_service = SleevePnLProjectionService(
        fill_outcome_repo=storage.fill_outcome_repo,
        funding_fee_repo=storage.funding_fee_repo,
        sleeve_pnl_repo=storage.sleeve_pnl_repo,
        execution_repo=storage.execution_repo,
        strategy_sleeve_repo=storage.strategy_sleeve_repo,
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
            sleeve_pnl_projection_service=sleeve_pnl_projection_service,
            portfolio_outbox_publisher=portfolio_outbox_publisher,
            state_scope=state_scope,
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
            execution_repo=storage.execution_repo,
            persistent_lot_book_service=(
                PersistentLotBookService(
                    position_lot_repo=storage.position_lot_repo,
                    lot_event_repo=storage.lot_event_repo,
                    projection_builder=LotBasedProjectionBuilder(),
                )
                if storage.position_lot_repo is not None and storage.lot_event_repo is not None
                else None
            ),
            sleeve_pnl_projection_service=sleeve_pnl_projection_service,
            portfolio_outbox_publisher=portfolio_outbox_publisher,
            state_scope=state_scope,
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
        exit_execution_repo=storage.exit_execution_repo,
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
        strategy_runtime_repo=storage.strategy_runtime_repo,
        reconstruction_service=PortfolioReconstructionService(
            initial_usdt_balance=runtime_settings.initial_usdt_balance,
            snapshot_builder=snapshot_builder,
        ),
        price_provider=market_gateway.latest_price,
        kill_switch=kill_switch,
        bootstrap_portfolio_from_exchange=bootstrap_from_exchange,
        reconciliation_stale_after_seconds=runtime_settings.reconciliation_stale_after_seconds,
        recovery_policy=runtime_layering.recovery_policy,
        fill_outcome_repo=storage.fill_outcome_repo,
        event_store=storage.event_store,
    )
    # OKXExecutionAdapter.client satisfies ExchangeOrderQuerier protocol.
    _exchange_order_client = (
        getattr(execution_adapter, "client", None)
        if isinstance(execution_adapter, OKXExecutionAdapter)
        else None
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
            exchange_order_client=_exchange_order_client,
        )
        if runtime_settings.recovery_reconciliation_execution_ledger_enabled
        else base_recovery_service
    )
    position_target_handler = _build_position_target_handler(
        settings=runtime_settings,
        mode_controller=mode_controller,
        runtime_layering=runtime_layering,
        account_service=account_service,
        policy_engine=policy_engine,
        risk_engine=risk_engine,
        execution_planner=execution_planner,
        market_gateway=market_gateway,
        kill_switch=kill_switch,
        metrics=metrics,
        bus=bus,
        event_store=storage.event_store,
        execution_repo=storage.execution_repo,
        strategy_runtime_repo=storage.strategy_runtime_repo,
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
        if runtime_settings.trading_product_type == "derivatives":
            _validate_exchange_position_mode_contract(
                settings=runtime_settings,
                snapshot=account_snapshot,
            )
        recent_bills_summary_getter = getattr(account_service, "recent_bills_summary", None)
        latest_recent_bills_getter = getattr(account_service, "latest_recent_bills", None)
        exchange_bills_summary = (
            recent_bills_summary_getter()
            if callable(recent_bills_summary_getter)
            else {}
        )
        recent_bills_rows = (
            latest_recent_bills_getter()
            if callable(latest_recent_bills_getter)
            else []
        )
        funding_fee_sync_posted_count = 0
        if funding_fee_sync_service is not None:
            funding_result = funding_fee_sync_service.sync_recent_bills(
                rows=recent_bills_rows,
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
                exchange_bills_summary=exchange_bills_summary,
            )
            imported_baseline = imported.snapshot
            imported_baseline_event_id = imported.event_id
    else:
        imported_baseline = None
        imported_baseline_event_id = None
        funding_fee_sync_posted_count = 0
    # Pre-recovery: query exchange for stuck CREATED/SUBMITTING orders
    # so that downstream recovery sees clean state and avoids unnecessary halts.
    if hasattr(recovery_service, "pre_recover_exchange_reconciliation"):
        await recovery_service.pre_recover_exchange_reconciliation()

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

    if storage.database_runtime is not None and storage.exit_execution_repo is not None:
        refreshed_exit_execution, startup_refresh_notes = startup_refresh_exit_execution_truth(
            settings=runtime_settings,
            execution_repo=storage.execution_repo,
            exit_execution_repo=storage.exit_execution_repo,
            scope=state_scope,
        )
        recovery_status = apply_startup_exit_execution_review_overlay(
            base_status=recovery_status,
            parent_intents=refreshed_exit_execution,
            refresh_notes=startup_refresh_notes,
        )
        if startup_refresh_notes:
            recovery_status = recovery_status.model_copy(
                update={"notes": list(dict.fromkeys([*recovery_status.notes, *startup_refresh_notes]))}
            )
        startup_snapshot_notes = persist_startup_exit_execution_state_snapshot(
            reconciliation_repo=storage.reconciliation_repo,
            scope=state_scope,
            status=recovery_status,
            parent_intents=refreshed_exit_execution,
            refresh_notes=startup_refresh_notes,
        )
        if startup_snapshot_notes:
            recovery_status = recovery_status.model_copy(
                update={"notes": list(dict.fromkeys([*recovery_status.notes, *startup_snapshot_notes]))}
            )

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
        sleeve_auto_execution_config_source=base_settings.strategy_sleeve_auto_execution_config_source,
        sleeve_auto_execution_uses_deprecated_key=base_settings.strategy_sleeve_auto_execution_uses_deprecated_key,
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
        strategy_coordinator=strategy_coordinator,
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
        sleeve_pnl_repo=storage.sleeve_pnl_repo,
        execution_repo=storage.execution_repo,
        exit_execution_repo=storage.exit_execution_repo,
        obligation_repo=storage.obligation_repo,
        reconciliation_repo=storage.reconciliation_repo,
        operator_repo=storage.operator_repo,
        strategy_profile_repo=storage.strategy_profile_repo,
        strategy_sleeve_repo=storage.strategy_sleeve_repo,
        strategy_runtime_repo=storage.strategy_runtime_repo,
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
        sleeve_pnl_projection_service=sleeve_pnl_projection_service,
        funding_fee_sync_service=funding_fee_sync_service,
    )
    if runtime.sleeve_pnl_projection_service is not None:
        runtime.sleeve_pnl_projection_service.rebuild_scope(scope=state_scope)
    from aats.services.operator.query_service import OperatorQueryService
    from aats.services.governance_engine.recovery_posture import RecoveryPostureEvaluator

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
    runtime.risk_engine.recovery_status_provider = lambda: RecoveryPostureEvaluator(runtime).finalize_status(
        base_status=runtime.recovery_status
    )
    runtime.decision_engine.strategy_profile_service = StrategyProfileControlService(runtime)
    return runtime
