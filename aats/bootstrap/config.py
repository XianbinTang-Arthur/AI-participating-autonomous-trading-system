from __future__ import annotations

import asyncio
import logging
import math
import os
import random as _random
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Literal

import yaml

from aats.bootstrap.logging import get_logger, log_event

from aats.bootstrap.managed_profiles import MANAGED_PROFILE_DERIVED_ENV_KEYS, load_managed_profile_values
from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.telemetry import TelemetryConfig, configure_telemetry
from aats.bootstrap.settings import (
    AATSSettings,
    DEPRECATED_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY,
    EVENT_BUS_BACKEND_IN_MEMORY,
    PROCESS_ROLE_DECISION,
    PROCESS_ROLE_EXECUTION,
    PROCESS_ROLE_GATEWAY,
    PROCESS_ROLE_MARKET,
    PROCESS_ROLE_MONOLITH,
)
from aats.bus.base import EventBus, MessageHandler
from aats.bus.memory_bus import InMemoryEventBus
from aats.bus.nats_bus import (
    DEFAULT_STREAM_SPECS,
    HybridBusRouting,
    HybridEventBus,
    NatsBusConfig,
    NatsEventBus,
    build_nats_streams_from_env,
)
from aats.events import topics
from aats.events.envelopes import build_envelope, parse_payload, publish_model
from aats.schemas.decision import DecisionOutcome, PositionTarget
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.execution import LegOrderIntent, order_intent_from_leg_order_intent
from aats.schemas.governance import PolicyDecision, RiskDecision
from aats.services.ai_service.inference import AIInferenceService
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AssessmentValidator
from aats.services.decision_engine.audit import DecisionAuditService
from aats.services.decision_engine.baseline import BaselineStrategy
from aats.services.decision_engine.feature_resolver import FeatureSnapshotResolver
from aats.services.decision_engine.context_builder import DecisionContextBuilder
from aats.services.decision_engine.orchestrator import DecisionOrchestrator
from aats.services.decision_engine.target_position import (
    TargetPositionEngine,
    finalize_position_sizing_breakdown,
    log_position_sizing_breakdown,
)
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
from aats.services.execution_engine.account_snapshot_cache import AccountSnapshotCache
from aats.services.execution_engine.obligation_cache import ObligationHotStateCache
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.execution_engine.orderbook_snapshot_refs import default_orderbook_snapshot_read_source
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.execution_engine.exit_execution_writer import ExitExecutionWriter
from aats.services.portfolio_service.outbox import PostgresPortfolioOutboxPublisher
from aats.services.portfolio_service.snapshot_cache import PortfolioSnapshotCache
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.execution_engine.planner import ExecutionPlanner
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.feature_engine.calculator import FeatureCalculator, FeatureEngine
from aats.services.feature_engine.long_short_poller import LongShortRatioPoller
from aats.services.feature_engine.regime import RegimeClassifier
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
from aats.services.governance_engine.abort_hooks import (
    AbortHookConfig,
    AbortHookService,
)
from aats.services.governance_engine.drift_score import DriftInputs
from aats.services.governance_engine.trial_guard import ForwardTrialGuardService
from aats.services.market_gateway.gateway import MarketDataGateway
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient
from aats.services.market_gateway.publisher import MarketSnapshotPublisher
from aats.services.operator.accounts import create_operator_user, enabled_admin_count
from aats.services.operator.command_bridge import (
    OperatorCommandClient,
    OperatorCommandWorker,
)
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
from aats.services.portfolio_service.initial_balance import effective_portfolio_initial_usdt_balance
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
from aats.storage.stream_snapshot_cache import StreamSnapshotCache
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
from aats.storage.hot_state_store import (
    HotStateStore,
    RedisHotStateConfig,
    build_hot_state_store,
)
from aats.storage.session import (
    DatabaseRuntime,
    apply_current_migrations,
    create_database_runtime,
    create_schema,
    scoped_runtime_lock_key,
    validate_current_migrations,
    validate_runtime_schema,
)
from aats.schemas.system import RecoveryStatus
from aats.schemas.common import new_id, utc_now
from aats.schemas.operator import ExecutionErrorSummary, ProcessingFailureRecord
from aats.schemas.runtime_profiles import RuntimeProfileResolution
from aats.schemas.strategy_runtime import StrategyExecutionBundle
from aats.storage.base import StrategyProfileRepository, StrategySleeveRepository

if TYPE_CHECKING:
    from aats.storage.housekeeping import DatabaseHousekeeping

_log = logging.getLogger(__name__)


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
    execution_truth_repo: ExecutionRepository | None = None
    reconciliation_execution_repo: ExecutionRepository | None = None


@dataclass(frozen=True, slots=True)
class ObserverSubscriptionSpec:
    topic: str
    name: str
    handler: Any


@dataclass(frozen=True, slots=True)
class CriticalBackgroundTaskFailure:
    """关键长期 task 非预期结束或成功进度超时的安全监督快照。"""

    task_name: str
    failure_kind: Literal[
        "exception",
        "cancelled",
        "unexpected_completion",
        "stalled",
    ]
    error_type: str | None = None
    stalled_seconds: float | None = None
    timeout_seconds: float | None = None


@dataclass(slots=True)
class CriticalBackgroundTaskProgress:
    """固定周期关键 task 的进程内成功进度 deadline。"""

    timeout_seconds: float
    last_success_monotonic: float


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
    bus: EventBus
    event_store: EventStore
    # Stage 6 Slice 6.1：跨进程共享的热状态 KV 存储。memory backend 是
    # monolith 默认值（零外部依赖）；4 进程拓扑下设 redis backend 让
    # gateway 能同步问询 execution/decision 的最新状态。
    # 设计文档：docs/task/stage_6_redis_hot_state_design.md
    hot_state_store: HotStateStore
    market_gateway: MarketDataGateway
    # Stage 3 多进程切片化：以下 slice 字段在 process_role 不需要时为 None。
    # 例如 gateway role 会让 feature_engine / ai_service / decision_engine / ...
    # 全部为 None；execution role 会让 feature_engine / decision 相关字段为 None。
    # monolith / None role 下所有字段都非 None（向后兼容现状）。
    feature_engine: FeatureEngine | None
    ai_service: AIInferenceService | None
    decision_engine: DecisionOrchestrator | None
    strategy_coordinator: Any | None
    decision_trigger: DecisionCycleTrigger | None
    decision_trigger_policy: DecisionTriggerPolicy | None
    execution_planner: ExecutionPlanner | None
    execution_adapter: ExchangeAdapter
    order_manager: OrderManager | None
    portfolio_service: PortfolioService | None
    reconciliation_service: ReconciliationService | None
    fee_resolver: EffectiveFeeResolver
    policy_engine: PolicyEngine | None
    risk_engine: RiskEngine | None
    # Stage 6 Slice 6.4：合并的 KillSwitch 类同时承担本地 sync read/write 与
    # 跨进程同步边车（hot_state_store + bus 由 bootstrap 注入）。slice 6.2 引入的
    # KillSwitchSyncService 已被合并到本类，5 个写入点 (W1-W5) 不再需要 if/else
    # fallback。详见 docs/task/stage_6_slice_6_4_kill_switch_unification_design.md。
    kill_switch: KillSwitch
    # Stage 6 Slice 6.2：跨进程 portfolio_snapshot 缓存边车。bootstrap 时从 Redis
    # hydrate 最近一份 snapshot；订阅 NATS portfolio.snapshots topic 让 4 个进程
    # 的 latest snapshot 视图保持同步。query_service._latest_scoped_snapshot 用
    # cache 优先 + portfolio_repo fallback。设计文档：
    # docs/task/stage_6_slice_6_3_portfolio_snapshot_design.md
    portfolio_snapshot_cache: PortfolioSnapshotCache
    # Stage 6 Slice 6.5：跨进程 obligation 缓存边车。同 6.3 sidecar 模板：bootstrap
    # 时从 Redis 读 index key + get_many hydrate 本地 dict；订阅
    # NATS execution.obligation_updates topic 让 4 个进程的 obligation 视图保持
    # 同步。execution 在每次 save_obligation 之后 best-effort publish(obligation)
    # 广播；decision 的 risk.py active_obligations() 读路径优先 cache → fallback
    # obligation_repo Postgres SELECT。设计文档：
    # docs/task/stage_6_slice_6_5_obligation_hot_state_design.md
    obligation_hot_state_cache: ObligationHotStateCache
    account_snapshot_cache: AccountSnapshotCache
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
    execution_truth_repo: ExecutionRepository | None = None
    sleeve_auto_execution_config_source: str = "strategy_sleeve_auto_execution_enabled"
    sleeve_auto_execution_uses_deprecated_key: bool = False
    # 高频流式快照（market.snapshots / features.snapshots）的进程内缓存。
    # 替代 Postgres event_store 为这两个 topic 提供 latest/recent 查询，
    # 避免 event_store 表膨胀。由 bus 的 publish/receive 路径更新。
    stream_snapshot_cache: StreamSnapshotCache | None = None
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
    abort_hook_service: AbortHookService | None = None
    derivatives_live_guard_service: DerivativesLiveGuardService | None = None
    replay_validation_history: list[dict[str, Any]] = field(default_factory=list)
    database_runtime: DatabaseRuntime | None = None
    background_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    critical_background_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    critical_background_task_progress: dict[
        str, CriticalBackgroundTaskProgress
    ] = field(default_factory=dict)
    background_failure_messages: dict[str, str] = field(default_factory=dict)
    execution_outbox_publisher: PostgresExecutionOutboxPublisher | None = None
    funding_fee_repo: FundingFeeRepository | None = None
    sleeve_pnl_projection_service: SleevePnLProjectionService | None = None
    funding_fee_sync_service: LedgerFundingFeeSyncService | None = None
    # P2-1：审计批量写服务引用。stop_background_tasks 需要调 stop_batch_writer
    # 刷入所有缓冲 records。monolith / decision role 下非 None。
    audit_service: DecisionAuditService | None = None
    # P3-1 / P3-2：数据库定期清理。清理已发布 outbox 行 + 归档表老化行。
    housekeeping: "DatabaseHousekeeping | None" = None
    # Stage 4：4 进程 execution role 下本地构造的 RiskEngine，仅用于
    # OrderManager.leg_risk_evaluator。monolith 下为 None（复用 decision
    # slice 的 risk_engine）。_bootstrap_derivatives_live_runtime_guards
    # 会向此实例注入 live_runtime_guard_provider / trial_guard_provider /
    # recovery_status_provider 三个安全信号 provider。
    execution_leg_risk_engine: RiskEngine | None = None
    # Slice 4-proc operator command proxy：gateway 端 client 与 execution 端
    # worker 的 sidecar 装配字段。monolith / market / decision role 下均为
    # None。gateway 在 build_runtime 末尾装 client；execution 装 worker；
    # 详见 docs/task/slice_4proc_operator_command_proxy_fix_design.md §4/§5。
    operator_command_client: OperatorCommandClient | None = None
    operator_command_worker: OperatorCommandWorker | None = None
    # AI command proxy：gateway→decision 方向。UI 的 AI 运行模式切换、AI
    # review restore / degrade-to-baseline 这三个 POST mutate 必须在装了
    # ai_service 的 decision 进程执行。gateway 装 client、decision 装 worker，
    # 用 AI_COMMAND_* topic 与 execution 的 OPERATOR_COMMAND_* 隔离。
    ai_command_client: OperatorCommandClient | None = None
    ai_command_worker: OperatorCommandWorker | None = None
    # Finding 3: guard signal 跨进程缓存。execution 侧 publish，decision 侧 read。
    # monolith 下为 None（guard service 直接注入 risk_engine）。
    guard_signal_caches: dict[str, Any] | None = None
    _guard_signal_publish_task: Any | None = None
    logger: Any = field(default_factory=lambda: get_logger("aats.runtime"))
    # build_runtime 解析后的 effective_process_role。settings.process_role 可能
    # 与 kwarg 传入值不一致（kwarg 优先），运行时门禁必须读此字段。
    process_role: str | None = None
    # P2.7 — Long-Short ratio poller (仅 flag=True 时由 _build_market_slice
    # 构造，否则 None). start_background_tasks 按 None 跳过启动.
    long_short_poller: LongShortRatioPoller | None = None

    def register_background_task(
        self,
        task: asyncio.Task[Any],
        *,
        name: str | None = None,
        critical: bool = False,
        owned_by_runtime: bool = True,
        progress_timeout_seconds: float | None = None,
    ) -> asyncio.Task[Any]:
        """登记 task 的所有权与 criticality，拒绝同名静默覆盖。"""
        timeout_seconds = ApplicationRuntime._validated_critical_progress_timeout(
            critical=critical,
            progress_timeout_seconds=progress_timeout_seconds,
        )
        if not critical:
            if owned_by_runtime and task not in self.background_tasks:
                self.background_tasks.append(task)
            return task

        task_name = str(name or task.get_name()).strip()
        if not task_name:
            raise ValueError("critical background task requires a non-empty name")
        registry = getattr(self, "critical_background_tasks", None)
        if registry is None:
            registry = {}
            self.critical_background_tasks = registry
        existing = registry.get(task_name)
        if existing is not None and existing is not task:
            raise RuntimeError(
                f"critical background task name already registered: {task_name}"
            )
        if timeout_seconds is not None:
            progress_registry = getattr(
                self,
                "critical_background_task_progress",
                None,
            )
            if progress_registry is None:
                progress_registry = {}
                self.critical_background_task_progress = progress_registry
            existing_progress = progress_registry.get(task_name)
            if (
                existing_progress is not None
                and existing_progress.timeout_seconds != timeout_seconds
            ):
                raise RuntimeError(
                    "critical background task progress timeout already registered: "
                    f"{task_name}"
                )
            if existing_progress is None:
                progress_registry[task_name] = CriticalBackgroundTaskProgress(
                    timeout_seconds=timeout_seconds,
                    last_success_monotonic=time.monotonic(),
                )
        if owned_by_runtime and task not in self.background_tasks:
            self.background_tasks.append(task)
        registry[task_name] = task
        return task

    def create_background_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
        critical: bool = False,
        progress_timeout_seconds: float | None = None,
    ) -> asyncio.Task[Any]:
        ApplicationRuntime._validated_critical_progress_timeout(
            critical=critical,
            progress_timeout_seconds=progress_timeout_seconds,
        )
        task = asyncio.create_task(coroutine, name=name)
        return self.register_background_task(
            task,
            name=name,
            critical=critical,
            owned_by_runtime=True,
            progress_timeout_seconds=progress_timeout_seconds,
        )

    @staticmethod
    def _validated_critical_progress_timeout(
        *,
        critical: bool,
        progress_timeout_seconds: float | None,
    ) -> float | None:
        if progress_timeout_seconds is None:
            return None
        if not critical:
            raise ValueError(
                "non-critical background task cannot declare a progress timeout"
            )
        timeout_seconds = float(progress_timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise ValueError(
                "critical background task progress timeout must be finite and positive"
            )
        return timeout_seconds

    def mark_critical_background_task_success(self, task_name: str) -> None:
        """提交固定周期关键 task 的一次成功业务周期。"""

        progress_registry = getattr(
            self,
            "critical_background_task_progress",
            None,
        ) or {}
        progress = progress_registry.get(task_name)
        if progress is None:
            raise RuntimeError(
                f"critical background task progress is not registered: {task_name}"
            )
        progress.last_success_monotonic = time.monotonic()

    def critical_background_task_failure(
        self,
    ) -> CriticalBackgroundTaskFailure | None:
        """返回首个已结束或成功进度超时的关键 task，不泄漏异常正文。"""
        registry = getattr(self, "critical_background_tasks", None) or {}
        progress_registry = getattr(
            self,
            "critical_background_task_progress",
            None,
        ) or {}
        now = time.monotonic()
        for task_name in sorted(registry):
            task = registry[task_name]
            if task.done():
                if task.cancelled():
                    return CriticalBackgroundTaskFailure(
                        task_name=task_name,
                        failure_kind="cancelled",
                        error_type="CancelledError",
                    )
                exception = task.exception()
                if exception is None:
                    return CriticalBackgroundTaskFailure(
                        task_name=task_name,
                        failure_kind="unexpected_completion",
                    )
                return CriticalBackgroundTaskFailure(
                    task_name=task_name,
                    failure_kind="exception",
                    error_type=type(exception).__name__,
                )
            progress = progress_registry.get(task_name)
            if progress is None:
                continue
            stalled_seconds = max(
                0.0,
                now - progress.last_success_monotonic,
            )
            if stalled_seconds >= progress.timeout_seconds:
                return CriticalBackgroundTaskFailure(
                    task_name=task_name,
                    failure_kind="stalled",
                    stalled_seconds=round(stalled_seconds, 3),
                    timeout_seconds=round(progress.timeout_seconds, 3),
                )
        return None

    async def wait_for_critical_background_task_failure(
        self,
    ) -> CriticalBackgroundTaskFailure:
        """阻塞到任一关键 task 结束或进度超时；不取消被观察 task。"""
        while True:
            failure = self.critical_background_task_failure()
            if failure is not None:
                return failure
            registry = getattr(self, "critical_background_tasks", None) or {}
            if not registry:
                await asyncio.Future()
            progress_registry = getattr(
                self,
                "critical_background_task_progress",
                None,
            ) or {}
            now = time.monotonic()
            deadline_waits = [
                max(
                    0.0,
                    progress.last_success_monotonic
                    + progress.timeout_seconds
                    - now,
                )
                for task_name, progress in progress_registry.items()
                if task_name in registry and not registry[task_name].done()
            ]
            await asyncio.wait(
                tuple(registry.values()),
                timeout=min(deadline_waits) if deadline_waits else None,
                return_when=asyncio.FIRST_COMPLETED,
            )

    async def start_background_tasks(self) -> None:
        # FS-002 Phase 3L：peer readiness 已完成后，先由 gateway/monolith
        # 建立 generation-scoped 短时交易许可。execution 只在最终提交 fence
        # 读取该许可，绝不能自行续租。任务内部在 TTL 上界到达时 raise，登记为
        # service-owned critical task 复用 FS-006 进程失败监督。
        trading_permission_task = (
            await self.kill_switch.start_trading_permission_lease()
        )
        if trading_permission_task is not None:
            self.register_background_task(
                trading_permission_task,
                critical=True,
                owned_by_runtime=False,
            )

        # Bug-1 时序平滑：在 market_gateway.start() 之前先用 OKX REST 拉一次
        # 历史 K 线灌入 FeatureCalculator 的 RollingCandleState。这样 market
        # 进程一开始推送 tick，feature calculator 就立即走"时序平滑"路径而不是
        # 退化到单 K 线瞬时算法。
        #
        # Best-effort：REST 失败/超时/数据不足 → 静默降级，FeatureCalculator
        # 内部 analyze_with_state 会自动在 state 未 ready 时走 analyze_kline
        # 退化路径，不阻断启动。
        if (
            self.settings.strategy_baseline_timeseries_smoothing_enabled
            and self.settings.market_data_backend == "okx"
            and self.feature_engine is not None
            and _slice_active("market", effective_process_role=self.process_role)
        ):
            await self._prewarm_feature_rolling_states()

        # Stage 3 多进程切片化：market_gateway.start() 会开启 OKX WebSocket
        # 和 REST fallback 后台任务。仅 market / monolith 角色需要，否则 4 个
        # 进程各自连一次 OKX，造成 4× 连接和下游 feature / decision 重复计算。
        if self.settings.market_data_backend == "okx" and _slice_active(
            "market", effective_process_role=self.process_role
        ):
            await self.market_gateway.start()
            market_critical_tasks = getattr(
                self.market_gateway,
                "critical_background_tasks",
                None,
            )
            if callable(market_critical_tasks):
                for task in market_critical_tasks():
                    self.register_background_task(
                        task,
                        critical=True,
                        owned_by_runtime=False,
                    )

        # P2.7 — Long-Short ratio poller 后台循环. 仅在 market / monolith 角色
        # 且 flag 开启时启动 (_build_market_slice 已保证 poller 非 None 等价于
        # 开启). 否则 None → 跳过.
        if self.long_short_poller is not None and _slice_active(
            "market", effective_process_role=self.process_role
        ):
            symbols = tuple(self.settings.expanded_allowed_symbols())
            if not symbols:
                symbols = (self.settings.default_symbol,)
            self.background_tasks.append(
                asyncio.create_task(
                    self.long_short_poller.run_forever(symbols=symbols),
                    name="aats_long_short_ratio_poller",
                )
            )
        # Stage 3 多进程切片化：OKX 私有 WS 和账户刷新循环仅在 execution /
        # monolith 角色启动。execution 是账户状态的权威来源；其余角色不需要
        # 实时账户快照，让它们各自连 OKX 只会 4× 放大连接和 REST 配额，且
        # 各进程形成的本地快照会互相漂移。未来 Stage 7 可通过 hot-state / bus
        # 将 exchange account snapshot 共享给 gateway 等只读角色。
        if self.environment_capabilities.account_state_source_kind == "exchange" and _slice_active(
            "execution", effective_process_role=self.process_role
        ):
            self.create_background_task(
                self.account_service.run_private_ws_forever(),
                name="aats_okx_private_account_ws",
                critical=True,
            )
        # Stage 3 多进程切片化：reconciliation/order_manager 在 gateway/market/decision
        # role 下不存在，对应 background loop 必须按字段是否非 None 来决定是否启动。
        if self.reconciliation_service is not None:
            reconciliation_interval_seconds = max(
                0.5,
                min(
                    self.settings.reconciliation_stale_after_seconds / 2.0,
                    60.0,
                ),
            )
            self.create_background_task(
                self._refresh_reconciliation_loop(),
                name="aats_reconciliation_refresh",
                critical=True,
                progress_timeout_seconds=self._critical_progress_timeout_seconds(
                    reconciliation_interval_seconds
                ),
            )
        if self.environment_capabilities.account_state_source_kind == "exchange" and _slice_active(
            "execution", effective_process_role=self.process_role
        ):
            self.create_background_task(
                self._refresh_account_loop(),
                name="aats_okx_account_refresh",
                critical=True,
                progress_timeout_seconds=self._critical_progress_timeout_seconds(
                    float(self.settings.okx_account_refresh_interval_seconds)
                ),
            )
        if (
            self.environment_capabilities.execution_adapter_kind == "okx"
            and self.order_manager is not None
        ):
            self.create_background_task(
                self._sync_execution_loop(),
                name="aats_okx_execution_sync",
                critical=True,
                progress_timeout_seconds=self._critical_progress_timeout_seconds(
                    float(self.settings.okx_execution_sync_interval_seconds)
                ),
            )
        if self.execution_outbox_publisher is not None:
            self.create_background_task(
                self._flush_execution_outbox_loop(),
                name="aats_execution_outbox_flush",
                critical=True,
                progress_timeout_seconds=self._critical_progress_timeout_seconds(
                    30.0
                ),
            )
        if self.execution_command_processor is not None:
            self.create_background_task(
                self._process_execution_commands_loop(),
                name="aats_execution_command_flow",
                critical=True,
                progress_timeout_seconds=self._critical_progress_timeout_seconds(
                    max(
                        0.1,
                        float(
                            self.settings.execution_command_poll_interval_seconds
                        ),
                    )
                ),
            )
        if self.phase1_shadow_monitor is not None:
            self.create_background_task(
                self._monitor_phase1_shadow_loop(),
                name="aats_phase1_shadow_monitor",
                critical=True,
                progress_timeout_seconds=self._critical_progress_timeout_seconds(
                    max(
                        1.0,
                        min(
                            self.settings.reconciliation_stale_after_seconds
                            / 4.0,
                            5.0,
                        ),
                    )
                ),
            )
        if self.trial_guard_service is not None:
            self.create_background_task(
                self._monitor_trial_guard_loop(),
                name="aats_trial_guard_monitor",
                critical=True,
                progress_timeout_seconds=self._critical_progress_timeout_seconds(
                    max(
                        1.0,
                        float(self.settings.trial_guard_poll_interval_seconds),
                    )
                ),
            )
        # 策略档位自动换档：与主决策链路解耦。strategy_profile_service 驻
        # decision/monolith role（_attach_strategy_profile_service 装配），
        # 每逢 :00 / :30 触发 evaluate_now()，最多每小时 2 次 AI 调用，避免
        # 每个 decision tick 都打 OpenAI/DeepSeek 账单。auto_control_enabled
        # 运行时可切换，loop 不退出，下次 boundary 按 flag 决定是否短路。
        _strategy_profile_service = (
            getattr(self.decision_engine, "strategy_profile_service", None)
            if self.decision_engine is not None
            else None
        )
        if _strategy_profile_service is not None:
            self.background_tasks.append(
                asyncio.create_task(
                    self._run_profile_auto_switch_loop(_strategy_profile_service),
                    name="aats_strategy_profile_auto_switch",
                )
            )
        # StreamSnapshotCache 定期 flush：将 latest + recent 快照 best-effort
        # 写入 Redis，供下次 bootstrap 恢复。高频 topic 不落 Postgres。
        # 仅 market / monolith 角色运行 flush——这两个角色是 MARKET_SNAPSHOTS
        # 和 FEATURE_SNAPSHOTS 的 producer，持有最新数据；其他角色是 consumer，
        # 若也 flush 会用落后于 producer 的旧快照覆盖 Redis 中的新值。
        if self.stream_snapshot_cache is not None and _slice_active(
            "market", effective_process_role=self.process_role
        ):
            self.background_tasks.append(
                asyncio.create_task(self._flush_stream_cache_loop(), name="aats_stream_cache_flush")
            )
        # P3-1 / P3-2：数据库定期清理后台任务。仅在 execution / monolith 角色下
        # 运行——这两个角色是 outbox 写入的主要来源。
        _housekeeping = getattr(self, "housekeeping", None)
        if _housekeeping is not None and _slice_active(
            "execution", effective_process_role=self.process_role
        ):
            self.background_tasks.append(
                asyncio.create_task(
                    self._housekeeping_loop(), name="aats_db_housekeeping"
                )
            )
        # Stage 9 checklist-4：AbortHookService 后台 loop。service.start() 自己
        # 管 asyncio.Task 生命周期（不会 append 到 background_tasks），我们只
        # 调 start()；stop() 在 stop_background_tasks 里镜像处理。
        # getattr 兜底：某些单测用 __new__ 绕过 dataclass __init__ 生成 minimal
        # runtime，此时字段默认值没被应用，直接访问会抛 AttributeError。
        try:
            abort_hook_service = getattr(self, "abort_hook_service", None)
            if abort_hook_service is not None:
                await abort_hook_service.start()
                abort_hook_task = getattr(
                    abort_hook_service,
                    "background_task",
                    None,
                )
                if abort_hook_task is not None:
                    self.register_background_task(
                        abort_hook_task,
                        critical=True,
                        owned_by_runtime=False,
                    )
        except Exception as exc:
            log_event(
                self.logger,
                "abort_hook_service_start_failed",
                level="error",
                error_type=type(exc).__name__,
            )
            raise

        decision_trigger = getattr(self, "decision_trigger", None)
        decision_background_task = getattr(
            decision_trigger,
            "background_task",
            None,
        )
        if decision_background_task is not None:
            self.register_background_task(
                decision_background_task,
                critical=True,
                owned_by_runtime=False,
            )
        guard_signal_task = getattr(self, "_guard_signal_publish_task", None)
        if guard_signal_task is not None:
            self.register_background_task(
                guard_signal_task,
                critical=True,
                owned_by_runtime=True,
            )
        # G-1 修复：MetricsRegistry → OTel Counter 桥接。定期把进程内计数器
        # 同步到 PrometheusMetricReader，供 Prometheus server 采集、Grafana 查询。
        # OTel 未安装时 bridge 静默跳过（返回 None → task 直接退出）。
        try:
            _metrics = getattr(self, "metrics", None)
            if _metrics is not None:
                from aats.bootstrap.metrics_bridge import start_metrics_bridge_loop
                self.background_tasks.append(
                    asyncio.create_task(
                        start_metrics_bridge_loop(_metrics),
                        name="aats_metrics_bridge",
                    )
                )
        except Exception as exc:
            log_event(
                self.logger,
                "metrics_bridge_start_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _prewarm_feature_rolling_states(self) -> None:
        """Bug-1 时序平滑配套：OKX REST 拉历史 K 线灌入 RollingCandleState.

        在 market_gateway.start() 之前调用。Best-effort — REST 失败不 raise，
        FeatureCalculator.analyze_with_state 会自动走 analyze_kline 退化路径。

        触发条件（start_background_tasks 已验证，本函数不再二次判）:
          - settings.strategy_baseline_timeseries_smoothing_enabled 为 True
          - market_data_backend == "okx"
          - 当前 process 是 market / monolith
          - feature_engine 非 None
        """
        from aats.services.feature_engine.warmup import (
            collect_state_keys,
            prewarm_many,
        )

        engine = self.feature_engine
        if engine is None:  # 理论上 caller 已守门，防御性编码
            return
        calculator = getattr(engine, "calculator", None)
        register_state = getattr(calculator, "register_rolling_state", None)
        if calculator is None or register_state is None:
            # 非 FeatureCalculator 实例（单测 stub 或未来替换实现）→ 静默跳过
            log_event(
                self.logger,
                "feature_warmup_calculator_not_supported",
                level="debug",
            )
            return

        try:
            symbols_iter = self.settings.expanded_allowed_symbols()
        except Exception:
            symbols_iter = ()
        symbols = tuple(dict.fromkeys(str(s) for s in symbols_iter if s))
        if not symbols and self.settings.default_symbol:
            symbols = (str(self.settings.default_symbol),)
        if not symbols:
            log_event(
                self.logger,
                "feature_warmup_no_symbols",
                level="warning",
            )
            return

        timeframes = tuple(
            tf for tf in self.settings.strategy_baseline_warmup_timeframes if tf
        )
        if not timeframes:
            timeframes = ("15m", "1h")

        keys = collect_state_keys(symbols=symbols, timeframes=timeframes)
        states_by_key = {
            key: register_state(symbol=key[0], timeframe=key[1]) for key in keys
        }
        try:
            results = await prewarm_many(
                states_by_key,
                okx_rest_url=self.settings.okx_rest_url,
                limit=int(self.settings.strategy_baseline_warmup_candle_limit),
                timeout_seconds=float(self.settings.okx_timeout_seconds),
            )
        except Exception as exc:
            # prewarm_many 内部已 best-effort，这里只防御万一的意外（网络堆栈异常等）
            log_event(
                self.logger,
                "feature_warmup_unexpected_failure",
                level="error",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            return

        ready_count = sum(1 for ok in results.values() if ok)
        log_event(
            self.logger,
            "feature_warmup_complete",
            total=len(results),
            ready=ready_count,
            failed=len(results) - ready_count,
            symbols=list(symbols),
            timeframes=list(timeframes),
        )

    async def stop_background_tasks(self) -> None:
        # 关停首先停止控制面续租并撤销短时许可；长期 kill-switch state 不变。
        # 即使后续账户 WS 或其他服务退出耗时，execution 也不能继续依赖旧 lease
        # 接受新的增险提交。kill_switch.stop() 稍后会幂等地再次确认清理。
        try:
            await self.kill_switch.stop_trading_permission_lease()
        except Exception as exc:
            log_event(
                self.logger,
                "kill_switch_permission_lease_shutdown_failed",
                level="warning",
                error_type=type(exc).__name__,
            )
        await self.account_service.stop_private_ws()
        # R2-B1 审查修复: poller.run_forever 被 background_tasks cancel 只能
        # 终止正在 await 的协程点；先显式调 stop() 触发 _stop_event，让 poller
        # 在下一个 while 条件检查处自然退出，避免 cancel 炸在 httpx.AsyncClient
        # 生命周期内部造成资源回收不确定。
        # getattr 兜底: 单测用 __new__ 构造 minimal runtime 时字段默认值不写入,
        # 直接 self.long_short_poller 会 AttributeError (与 abort_hook_service /
        # portfolio_snapshot_cache 等字段的处理方式一致).
        _poller = getattr(self, "long_short_poller", None)
        if _poller is not None:
            try:
                await _poller.stop()
            except Exception as exc:
                log_event(
                    self.logger,
                    "long_short_poller_stop_failed",
                    level="warning",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        for task in self.background_tasks:
            task.cancel()
        for task in self.background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log_event(
                    self.logger,
                    "background_task_shutdown_observed_failure",
                    level="warning",
                    task_name=task.get_name(),
                    error_type=type(exc).__name__,
                )
        self.background_tasks.clear()
        critical_registry = getattr(self, "critical_background_tasks", None)
        if critical_registry is not None:
            critical_registry.clear()
        critical_progress_registry = getattr(
            self,
            "critical_background_task_progress",
            None,
        )
        if critical_progress_registry is not None:
            critical_progress_registry.clear()
        # P2-1：关停审计批量写。必须在 bus.close 和 DB dispose 之前执行，
        # 确保所有缓冲中的 audit records 刷入 DB。getattr 兜底同下方各 cache。
        try:
            _audit_service = getattr(self, "audit_service", None)
            if _audit_service is not None and hasattr(_audit_service, "stop_batch_writer"):
                await _audit_service.stop_batch_writer()
        except Exception as exc:
            log_event(
                self.logger,
                "audit_batch_writer_shutdown_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        # Stage 9 checklist-4：停 AbortHookService，必须放在 kill_switch.stop
        # 之前。service.stop() 只取消自己的 _loop task，不动 kill_switch 状态。
        # 用 getattr 兜底是因为单测里某些 minimal runtime 走 __new__ 绕过 dataclass
        # __init__，字段默认值不会被写进对象，直接 self.abort_hook_service 会抛
        # AttributeError；与下面 kill_switch / portfolio_snapshot_cache 的 try 块一致。
        try:
            abort_hook_service = getattr(self, "abort_hook_service", None)
            if abort_hook_service is not None:
                await abort_hook_service.stop()
        except Exception as exc:
            log_event(
                self.logger,
                "abort_hook_service_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        # 2026-04-20 decision_features_handler_queue_decoupling_sow.md §3.S1
        # 停掉 DecisionCycleTrigger 的 dispatcher task。和 abort_hook_service
        # 同模式：_subscribe_critical_handlers 里 start()，stop_background_tasks
        # 里 stop()。必须在 bus.close 之前（stop() 内部不走 NATS，但保持一致的
        # teardown 顺序便于审计）。getattr 兜底是因为单测 __new__ minimal
        # runtime 场景，字段默认值可能未写入。
        try:
            decision_trigger = getattr(self, "decision_trigger", None)
            if decision_trigger is not None:
                await decision_trigger.stop()
        except Exception as exc:
            log_event(
                self.logger,
                "decision_trigger_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        await self.market_gateway.stop()
        close_rest_client = getattr(self.account_service.client, "aclose", None)
        if callable(close_rest_client):
            await close_rest_client()
        # Stage 6 Slice 6.4：关闭合并后的 KillSwitch sidecar。必须在 bus.close
        # 之前，因为 stop() 内部会撤销 NATS 订阅；本地 KillSwitch 状态保持不变
        # （关闭不代表 resume，下次启动从 Redis 读到的仍然是上一次 halt 状态）。
        try:
            await self.kill_switch.stop()
        except Exception as exc:
            log_event(
                self.logger,
                "kill_switch_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        # Stage 6 Slice 6.3：关闭 portfolio_snapshot_cache。同 6.2 模板：必须在
        # bus.close 之前。stop() 不写 Redis（cache 状态不失效，下次启动会 hydrate）。
        try:
            await self.portfolio_snapshot_cache.stop()
        except Exception as exc:
            log_event(
                self.logger,
                "portfolio_snapshot_cache_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        # Stage 6 Slice 6.5：关闭 obligation_hot_state_cache。与 6.3 同模板：必
        # 须在 bus.close 之前；stop() 不写/清 Redis（cache 状态不失效，下次启动
        # 会 hydrate）。用 getattr 兜底是因为单测里某些 minimal runtime 走
        # __new__ 绕过 dataclass __init__，直接 self.obligation_hot_state_cache
        # 会抛 AttributeError；与 abort_hook_service 同处理方式。
        try:
            obligation_hot_state_cache = getattr(
                self, "obligation_hot_state_cache", None
            )
            if obligation_hot_state_cache is not None:
                await obligation_hot_state_cache.stop()
        except Exception as exc:
            log_event(
                self.logger,
                "obligation_hot_state_cache_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        # 跨进程 account snapshot 缓存：关闭。与 6.5 同模板：必须在 bus.close
        # 之前；stop() 不写/清 Redis。getattr 兜底同上。
        try:
            _account_snapshot_cache = getattr(
                self, "account_snapshot_cache", None
            )
            if _account_snapshot_cache is not None:
                await _account_snapshot_cache.stop()
        except Exception as exc:
            log_event(
                self.logger,
                "account_snapshot_cache_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        # Slice 4-proc operator command proxy：关闭 client / worker。必须在
        # bus.close 之前，让未完成的 invoke() 先抛 OperatorCommandError 出
        # 去，避免 bus 关了之后 pending future 永远不 resolve 导致 HTTP
        # handler 挂死。getattr 兜底：某些单测用 __new__ 构 minimal runtime，
        # 字段默认值不会被写入对象。
        try:
            operator_command_client = getattr(self, "operator_command_client", None)
            if operator_command_client is not None:
                await operator_command_client.stop()
        except Exception as exc:
            log_event(
                self.logger,
                "operator_command_client_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        try:
            operator_command_worker = getattr(self, "operator_command_worker", None)
            if operator_command_worker is not None:
                await operator_command_worker.stop()
        except Exception as exc:
            log_event(
                self.logger,
                "operator_command_worker_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        # Stage 4: 关闭 EventBus（drain 任何 in-flight NATS publish + unsubscribe
        # 所有 JetStream consumer）。InMemoryEventBus 没有 close 方法，跳过；
        # HybridEventBus / NatsEventBus 有 close，会做优雅 drain。
        # 必须在 database_runtime.dispose() 之前完成，因为 publish_envelope 的
        # 双写路径需要 event_store / DB 仍然可用。
        bus_close = getattr(self.bus, "close", None)
        if bus_close is not None:
            try:
                await bus_close()
            except Exception as exc:
                log_event(
                    self.logger,
                    "event_bus_shutdown_close_failed",
                    level="warning",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        # Stage 6 Slice 6.1：关闭 HotStateStore（Redis 客户端 aclose / 内存
        # dict.clear）。在 database_runtime.dispose 之前，与 bus.close 同样
        # 走 best-effort 路径——hot_state 是缓存，关闭失败不能阻塞整体停机。
        try:
            await self.hot_state_store.close()
        except Exception as exc:
            log_event(
                self.logger,
                "hot_state_store_shutdown_close_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        if self.database_runtime is not None:
            self.database_runtime.dispose()

    @staticmethod
    def _critical_progress_timeout_seconds(interval_seconds: float) -> float:
        """固定周期关键 task 的成功进度预算：至少 60 秒或三个周期。"""

        return max(60.0, float(interval_seconds) * 3.0)

    @staticmethod
    def _jittered_sleep_seconds(base_seconds: float, jitter_fraction: float = 0.10) -> float:
        """2026-04-21 A2 · 给固定间隔 loop 加随机 jitter 防 4 进程锁步打 DB。

        背景：gateway/market/decision/execution 4 进程在 deploy 时**同一秒**
        启动，固定间隔的 background loop 会永远在同一个时间窗内触发，造成
        协同 DB 峰值 + Prometheus metric 尖刺。加 jitter 后几次迭代内自然
        decorrelate。

        返回 [base, base * (1+jitter_fraction)) 区间内的随机时长。默认 10%
        jitter 既能快速打散锁步，又不会显著改变 loop 语义。

        不用在超长间隔（如 housekeeping 6h）上：10% 就是 36 分钟浮动。
        不用在已有 exponential backoff 的 loop 上：backoff 本身就是变化的。
        """
        if base_seconds <= 0:
            return base_seconds
        return base_seconds + _random.random() * base_seconds * jitter_fraction

    async def _flush_stream_cache_loop(self) -> None:
        """定期将 StreamSnapshotCache 的 latest 快照 best-effort 写入 Redis。"""
        while True:
            await asyncio.sleep(self._jittered_sleep_seconds(5.0))
            try:
                await self.stream_snapshot_cache.flush_to_hot_state()
            except Exception as exc:
                await self._record_background_failure(subsystem="stream_cache_flush", exc=exc)
            else:
                await self._record_background_recovery(subsystem="stream_cache_flush")

    async def _refresh_account_loop(self) -> None:
        while True:
            try:
                await self.account_service.refresh()
                await self._publish_account_snapshot_to_cache()
                await self._sync_funding_fees_after_refresh()
                await self._evaluate_derivatives_live_guard_after_refresh()
            except Exception as exc:
                await self._record_background_failure(subsystem="account_refresh", exc=exc)
            else:
                await self._record_background_recovery(subsystem="account_refresh")
                self.mark_critical_background_task_success(
                    "aats_okx_account_refresh"
                )
            await asyncio.sleep(
                self._jittered_sleep_seconds(
                    float(self.settings.okx_account_refresh_interval_seconds)
                )
            )

    async def _publish_account_snapshot_to_cache(self) -> None:
        """refresh 成功后把 snapshot 广播到 account_snapshot_cache。

        best-effort：cache.publish 内部 Redis/NATS 写失败会 log warning 不抛。
        执行路径上的 account_service._latest_snapshot 已经由 refresh() 直接更新，
        这里只负责通知跨进程 cache。
        """
        snapshot = self.account_service.latest_snapshot()
        if snapshot is None:
            return
        try:
            await self.account_snapshot_cache.publish(
                snapshot,
                recent_bills=self.account_service.latest_recent_bills(),
            )
        except Exception as exc:
            await self._record_background_failure(
                subsystem="account_snapshot_cache_publish", exc=exc
            )
        else:
            await self._record_background_recovery(
                subsystem="account_snapshot_cache_publish"
            )

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
        # Stage 3：portfolio_service 仅在 execution/monolith role 下存在。
        if self.portfolio_service is not None and hasattr(self.portfolio_service, "bootstrap_snapshot"):
            await self.portfolio_service.bootstrap_snapshot(snapshot_origin="local_repair")

    async def _sync_execution_loop(self) -> None:
        # Stage 3：order_manager 在非 execution/monolith role 下为 None；
        # start_background_tasks 已按 None 跳过本 loop，这里再加一道保险。
        if self.order_manager is None:
            return
        while True:
            try:
                await self.order_manager.sync_exchange_state()
            except Exception as exc:
                await self._record_background_failure(subsystem="execution_sync", exc=exc)
            else:
                await self._record_background_recovery(subsystem="execution_sync")
                self.mark_critical_background_task_success(
                    "aats_okx_execution_sync"
                )
            await asyncio.sleep(
                self._jittered_sleep_seconds(
                    float(self.settings.okx_execution_sync_interval_seconds)
                )
            )

    async def _refresh_reconciliation_loop(self) -> None:
        # Stage 3：reconciliation_service 在非 execution/monolith role 下为 None；
        # start_background_tasks 已按 None 跳过本 loop，这里再加一道保险。
        if self.reconciliation_service is None:
            return
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
            else:
                await self._record_background_recovery(subsystem="reconciliation_refresh")
                self.mark_critical_background_task_success(
                    "aats_reconciliation_refresh"
                )
            await asyncio.sleep(self._jittered_sleep_seconds(interval_seconds))

    async def _flush_execution_outbox_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                if self.execution_outbox_publisher is not None:
                    await self.execution_outbox_publisher.flush_pending()
                await self._record_background_recovery(subsystem="execution_outbox_flush")
                self.mark_critical_background_task_success(
                    "aats_execution_outbox_flush"
                )
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
            else:
                await self._record_background_recovery(subsystem="execution_command_flow")
                self.mark_critical_background_task_success(
                    "aats_execution_command_flow"
                )
            await asyncio.sleep(self._jittered_sleep_seconds(interval_seconds))

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
            else:
                self.mark_critical_background_task_success(
                    "aats_phase1_shadow_monitor"
                )
            await asyncio.sleep(self._jittered_sleep_seconds(interval_seconds))

    async def _monitor_trial_guard_loop(self) -> None:
        interval_seconds = max(1.0, float(self.settings.trial_guard_poll_interval_seconds))
        while True:
            try:
                if self.trial_guard_service is not None:
                    await asyncio.to_thread(self.trial_guard_service.evaluate_now)
            except Exception as exc:
                await self._record_background_failure(subsystem="trial_guard_monitor", exc=exc)
            else:
                await self._record_background_recovery(subsystem="trial_guard_monitor")
                self.mark_critical_background_task_success(
                    "aats_trial_guard_monitor"
                )
            await asyncio.sleep(self._jittered_sleep_seconds(interval_seconds))

    @staticmethod
    def _seconds_until_next_half_hour_boundary(now: datetime) -> float:
        """从 ``now`` 到下一个整点或半点（:00 / :30）之间的秒数。

        Edge cases：
          - now == :00:00.000 → 返回 1800（下一次 :30）
          - now == :30:00.000 → 返回 1800（下一次 :00）
          - now == :29:59.5   → 返回 0.5
        保持每小时恰好两次触发，不依赖具体对齐时刻。
        """
        total_past_hour_seconds = (
            now.minute * 60 + now.second + now.microsecond / 1_000_000
        )
        next_boundary_seconds = 1800 if now.minute < 30 else 3600
        delta = next_boundary_seconds - total_past_hour_seconds
        return delta if delta > 0 else 1800.0

    async def _run_profile_auto_switch_loop(self, service: Any) -> None:
        """档位评估调度循环：每逢 :00 / :30 触发一次 evaluate_now()。

        与主决策链路解耦（原本每个 decision tick 都会触发 AI 推断，API 账单
        线性叠加）；改为 clock-aligned 定时任务后每小时最多两次 AI 调用。

        运行时 ``strategy_profile_auto_control_enabled`` 被运维关闭时 loop 不
        退出，下一次 boundary 醒来直接短路—避免运维每次切换都要重启进程。
        当 operator 手动固定档位导致 ``auto_switch_effective_enabled`` 为 false
        时，loop 仍写入只读评估证据，但禁用 AI recommendation 与自动激活。
        异常只记 failure event，不让 loop 挂掉。
        """
        while True:
            delay = self._seconds_until_next_half_hour_boundary(utc_now())
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            if not self.settings.strategy_profile_auto_control_enabled:
                continue
            auto_activation_enabled = False
            try:
                checker = getattr(service, "auto_switch_effective_enabled", None)
                auto_activation_enabled = (
                    bool(await asyncio.to_thread(checker))
                    if callable(checker)
                    else True
                )
            except Exception as exc:
                await self._record_background_failure(
                    subsystem="strategy_profile_auto_switch_state", exc=exc
                )
                continue
            else:
                await self._record_background_recovery(
                    subsystem="strategy_profile_auto_switch_state"
                )
            try:
                await service.evaluate_now(
                    allow_auto_activation=auto_activation_enabled,
                    use_ai_recommendation=auto_activation_enabled,
                )
                log_event(
                    self.logger,
                    "strategy_profile_auto_switch_scheduled_tick",
                    fired_at=utc_now().isoformat(),
                    allow_auto_activation=auto_activation_enabled,
                    use_ai_recommendation=auto_activation_enabled,
                )
            except Exception as exc:
                await self._record_background_failure(
                    subsystem="strategy_profile_auto_switch", exc=exc
                )
            else:
                await self._record_background_recovery(
                    subsystem="strategy_profile_auto_switch"
                )

    async def _housekeeping_loop(self) -> None:
        """P3-1 / P3-2 + Path B Phase 1：每 6 小时执行一次数据库清理。

        任务：
          - purge_published_outbox (outbox 已发布行 > 7 天)
          - archive_hot_event_store (event_store 热表 > 14 天 → archive)
          - purge_old_archive_events (archive 表 > 90 天)
        """
        _INTERVAL_SECONDS = 6 * 3600  # 6h
        # 首次延迟 60s，避免启动热路径上叠加 DELETE 查询
        await asyncio.sleep(60)
        while True:
            try:
                housekeeping = getattr(self, "housekeeping", None)
                if housekeeping is not None:
                    result = await asyncio.to_thread(housekeeping.run_all)
                    archive_hot = result.get("archive_hot_report") or {}
                    log_event(
                        self.logger,
                        "db_housekeeping_completed",
                        outbox_purged=result.get("outbox_purged", 0),
                        archive_purged=result.get("archive_purged", 0),
                        archive_hot_copied=archive_hot.get("copied_rows", 0),
                        archive_hot_deleted=archive_hot.get("deleted_rows", 0),
                        archive_hot_batches=archive_hot.get("batches", 0),
                        archive_hot_time_ms=archive_hot.get("time_taken_ms", 0),
                    )
                    await self._record_background_recovery(
                        subsystem="db_housekeeping"
                    )
            except Exception as exc:
                await self._record_background_failure(
                    subsystem="db_housekeeping", exc=exc
                )
            await asyncio.sleep(_INTERVAL_SECONDS)

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
        state = getattr(self, "background_failure_messages", None)
        if state is None:
            state = {}
            self.background_failure_messages = state
        state[subsystem] = message
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

    async def _record_background_recovery(self, *, subsystem: str) -> None:
        await asyncio.to_thread(
            self._record_background_recovery_sync,
            subsystem=subsystem,
        )

    def _record_background_recovery_sync(self, *, subsystem: str) -> None:
        state = getattr(self, "background_failure_messages", None)
        if not state or subsystem not in state:
            return
        state.pop(subsystem, None)
        self.event_store.append(
            build_envelope(
                topic=topics.EXECUTION_ERROR_SUMMARIES,
                key=subsystem,
                payload_model=ExecutionErrorSummary(
                    subsystem=subsystem,
                    severity="warning",
                    message=f"{subsystem}_recovered",
                    observed_at=utc_now(),
                ),
                source_component="runtime",
            )
        )
        log_event(
            self.logger,
            "background_loop_recovered",
            subsystem=subsystem,
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
        if _is_dev_simulated_exchange_runtime(settings):
            _log.warning(
                "dev_simulated_exchange_runtime_allows_insecure_cookie "
                "error_prefix=%s "
                "(HTTP dev setup; NOT suitable for prod/live, never run guarded_live here)",
                error_prefix,
            )
        else:
            raise ValueError(f"{error_prefix}_requires_secure_operator_session_cookie")


def _is_dev_simulated_exchange_runtime(settings: AATSSettings) -> bool:
    """dev 环境 + 模拟盘的组合标记，用于 exchange runtime hardening gate 放行。

    这条组合特指：
    - WSL2 docker-compose 4 进程真跑（observation / drill）
    - 本地 scripts/start_api.py --profile spot/derivatives（dev 迭代）

    两条都满足的时候，hardening gate 里"必须启用 secure cookie"和
    "必须有 enabled admin user"两条校验放行，改成 WARNING 日志。

    prod/live 环境一律不放行 —— managed profile 的 spot_live/derivatives_live
    variant 把 environment 硬编码成 "prod"，走不到这个分支；同时
    okx_simulated_trading 默认 False，双保险。

    note: 本 helper 只影响 validator 行为，不改变 managed profile defaults，
    也不对外部 caller 暴露。根因修复 slice
    (docs/task/slice_docker_compose_hardening_fix_design.md) 的工作包 A。
    """
    return (
        settings.environment == "dev"
        and getattr(settings, "okx_simulated_trading", False) is True
    )


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


def _auto_seed_operator_admin_if_configured(storage: StorageBackends) -> bool:
    """当环境变量 AATS_OPERATOR_ADMIN_USERNAME / PASSWORD 存在且数据库无 admin 时自动创建。

    返回 True 表示成功创建了 admin 用户，False 表示未执行（缺少环境变量）。
    """
    username = os.environ.get("AATS_OPERATOR_ADMIN_USERNAME", "").strip()
    password = os.environ.get("AATS_OPERATOR_ADMIN_PASSWORD", "")
    if not username or not password:
        return False
    try:
        created = create_operator_user(
            storage.operator_repo,
            username=username,
            password=password,
            role="admin",
            enabled=True,
        )
        log_event(
            _log, "auto_seeded_operator_admin",
            username=created.username,
            role=created.role,
        )
        return True
    except ValueError as exc:
        # username_conflict → 用户已存在，说明之前建过但可能被 disabled
        log_event(_log, "auto_seed_operator_admin_skipped", level="warning", reason=str(exc))
        return False


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
    # 数据库无 admin → 尝试从环境变量自动创建
    if _auto_seed_operator_admin_if_configured(storage):
        return
    if _is_dev_simulated_exchange_runtime(settings):
        _log.warning(
            "dev_simulated_exchange_runtime_allows_empty_admin_user "
            "(operator console login unavailable; "
            "run scripts/seed_operator_admin.py to enable)"
        )
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
    from aats.services.portfolio_service.fill_projection_writer import save_fill_outcome_direct_legacy_only
    from aats.storage.fill_outcome_repo_postgres import PostgresFillOutcomeRepository

    if isinstance(fill_outcome_repo, PostgresFillOutcomeRepository):
        # Production Postgres projections must be reconstructed through
        # PostgresPortfolioOutboxPublisher/recovery projection paths, not by
        # replaying event-store deltas directly into fill_outcomes during
        # bootstrap.
        return

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
        save_fill_outcome_direct_legacy_only(
            fill_outcome_repo=fill_outcome_repo,
            outcome=outcome,
            source_component="bootstrap_event_store_backfill",
            logger=_log,
        )


def _storage_execution_truth_repo(storage: StorageBackends) -> ExecutionRepository:
    return storage.execution_truth_repo or storage.reconciliation_execution_repo or storage.execution_repo


def build_storage_backends(
    settings: AATSSettings,
    *,
    process_role: str | None = None,
) -> StorageBackends:
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

    database_runtime = create_database_runtime(
        settings.database_url,
        process_role=process_role,
    )
    if settings.database_auto_create_schema:
        create_schema(database_runtime)
        apply_current_migrations(database_runtime)
    else:
        validate_current_migrations(database_runtime)
    validate_runtime_schema(database_runtime)
    if settings.database_single_runtime_guard_enabled:
        database_runtime.acquire_single_runtime_lock(
            scoped_runtime_lock_key(
                database_url=settings.database_url,
                base_lock_key=settings.database_runtime_lock_key,
                process_role=process_role,
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
    orderbook_snapshot_read_source = (
        default_orderbook_snapshot_read_source()
        if process_role in {None, PROCESS_ROLE_MONOLITH, PROCESS_ROLE_EXECUTION}
        else None
    )
    converged_execution_repo = ConvergedPostgresExecutionRepository(
        database_runtime.session_factory,
        execution_order_repo=execution_order_repo,
        execution_order_history_repo=execution_order_history_repo,
        execution_fill_repo=execution_fill_repo_v2,
        orderbook_snapshot_read_source=orderbook_snapshot_read_source,
    )
    # Reconciliation compares exchange state against current execution truth
    # even while live execution still uses the legacy write path.
    execution_repo = converged_execution_repo if settings.financial_convergence_mode_enabled else legacy_execution_repo

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
        execution_truth_repo=converged_execution_repo,
        reconciliation_execution_repo=converged_execution_repo,
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
    bus: EventBus,
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
        sizing_breakdown = finalize_position_sizing_breakdown(
            sizing_breakdown=outcome.sizing_breakdown,
            resolved_target_qty=final_target_qty,
            target_leverage=target.target_leverage,
        )
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
                "sizing_breakdown": sizing_breakdown,
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
        log_position_sizing_breakdown(
            logger=_log,
            decision_id=finalized_outcome.decision_id,
            symbol=target.symbol,
            sizing_breakdown=finalized_outcome.sizing_breakdown,
            final_action=finalized_outcome.final_action,
            final_direction=finalized_outcome.final_direction,
            final_target_qty=finalized_outcome.final_target_qty,
            policy_blocked=finalized_outcome.policy_blocked,
            risk_capped=finalized_outcome.risk_capped,
        )
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

    def _risk_budget_state_with_execution_convergence(risk_decision: RiskDecision) -> dict[str, Any]:
        state = dict(risk_decision.risk_budget_state or {})
        convergence = state.get("execution_convergence")
        convergence_state = dict(convergence) if isinstance(convergence, dict) else {}

        def _add_exposure(prefix: str, exposure: Any | None) -> None:
            if exposure is None:
                return
            for field_name in (
                "long_position_qty",
                "short_position_qty",
                "net_position_qty",
                "gross_position_qty",
                "long_notional",
                "short_notional",
                "net_notional",
                "gross_notional",
                "net_exposure_side",
            ):
                value = getattr(exposure, field_name, None)
                if value is not None:
                    convergence_state[f"{prefix}_{field_name}"] = str(value)

        _add_exposure("current", risk_decision.current_derivatives_exposure)
        _add_exposure("projected", risk_decision.projected_derivatives_exposure)
        if convergence_state:
            convergence_state.setdefault("source", "risk_engine_derivatives_exposure")
            state["execution_convergence"] = convergence_state
        return state

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
            risk_budget_state=_risk_budget_state_with_execution_convergence(risk_decision),
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
            market_snapshot_ref=target.market_snapshot_ref,
            feature_snapshot_ref=target.feature_snapshot_ref,
            portfolio_snapshot_ref=target.portfolio_snapshot_ref,
            health_snapshot_ref=target.health_snapshot_ref,
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
            market_snapshot_ref=base_target.market_snapshot_ref,
            feature_snapshot_ref=base_target.feature_snapshot_ref,
            portfolio_snapshot_ref=base_target.portfolio_snapshot_ref,
            health_snapshot_ref=base_target.health_snapshot_ref,
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
                market_snapshot_ref=base_target.market_snapshot_ref,
                feature_snapshot_ref=base_target.feature_snapshot_ref,
                portfolio_snapshot_ref=base_target.portfolio_snapshot_ref,
                health_snapshot_ref=base_target.health_snapshot_ref,
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
                    "risk_budget_state": _risk_budget_state_with_execution_convergence(risk_decision),
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
        # LF-20260421-004 fix · 2026-04-22
        # Kill Switch 同步预检：在任何 account.refresh / policy / risk / submit 之前
        # 读本地 kill_switch.halted 状态。避免 Reconciliation → RecoveryPosture →
        # kill_switch.halt 链路的 10-50ms 窗口内溜单。
        #
        # kill_switch.halted 是同步读、本地 cache、I1 保证 halt() 立即生效。
        # 所以这个检查开销几乎为零（< 1 μs），但能堵住"halt 已经决定但 NATS 还
        # 没广播到别的 process" 的窗口 —— 因为 execution 进程调 halt() 本身就
        # 更新了自己 local cache，后续 handle_position_target 立刻能读到。
        ks_status = kill_switch.status()
        if ks_status.get("halted"):
            log_event(
                _log,
                "position_target_rejected_kill_switch_halted",
                level="warning",
                decision_id=target.decision_id,
                symbol=target.symbol,
                reason=str(ks_status.get("reason") or "kill_switch_halted"),
            )
            return
        if runtime_layering.environment_capabilities.exchange_coupled:
            await account_service.refresh(force_account_state=True)

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
                # Sort close/reduce legs before open legs so that de-risk
                # actions publish (and therefore execute) first.  Critical
                # for short→long reversals where the close-leg must free
                # margin before the open-leg consumes it.
                if len(execution_leg_results) > 1:
                    execution_leg_results = [
                        item
                        for _, item in sorted(
                            enumerate(execution_leg_results),
                            key=lambda pair: _independent_leg_priority(
                                target=target,
                                item=pair[1],
                                original_index=pair[0],
                            ),
                        )
                    ]
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
                    # P0-b Task 2.3 follow-up (2026-04-20):
                    # 给 order submission 加 mode label, 让
                    # aats_orders_submitted_total{mode=...} alert 能 fire.
                    # 见 deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml
                    # sev3-runtime-ai-decision-no-orders.
                    # (2026-04-23: sev2-runtime-baseline-has-orders 已废弃删除,
                    # 见 docs/governance/runtime_trading_mode_semantics.md §8).
                    # metrics.increment_labeled 异常永不阻断订单流.
                    try:
                        metrics.increment_labeled(
                            "orders_submitted",
                            labels={"mode": str(settings.canonical_ai_operating_mode)},
                        )
                    except Exception:
                        pass
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
            # build_plan() already logs at the appropriate level (debug for legitimate
            # no-op like "hold current position", warning for suspicious cases like
            # lot-size quantization issues). Keep this caller-side message at debug
            # so a normal no-op decision does not emit two warnings.
            _log.debug(
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
        # P0-b Task 2.3 follow-up (2026-04-20): mode label for alert rules.
        # 同上面 execution leg intent 路径, 此路径为 single-leg position target.
        try:
            metrics.increment_labeled(
                "orders_submitted",
                labels={"mode": str(settings.canonical_ai_operating_mode)},
            )
        except Exception:
            pass
        await execution_planner.publish_intent(bus=bus, intent=intent)

    return handle_position_target


# =============================================================================
# _CollectingBus: 订阅去重适配器（Stage 7 NATS duplicate-binding 修复）
#
# 背景：
#   - HybridEventBus.subscribe(topic, handler) 按 topic 路由：critical-topic
#     直接进 NATS critical bus，observer-topic 进 InMemoryBus。
#   - NATS 的 durable_name 来自 (consumer_role, topic)，每 (role, topic)
#     只允许一个 binding，第二次 subscribe 同一 topic 会抛
#     "consumer is already bound to a subscription"。
#   - 但 _subscribe_critical_handlers + _subscribe_observer_handlers 历史
#     实现里有些 critical-routed topic 被重复订阅（不同 handler 各订一次）：
#       * POSITION_TARGETS (critical)：position_target_handler + audit.handle_position_target
#       * PORTFOLIO_SNAPSHOTS (critical)：audit.handle_portfolio_snapshot + reconciliation.handle_portfolio_snapshot
#       * RECONCILIATION_REPORTS (critical)：ai.handle_reconciliation_report + audit.handle_reconciliation_report
#   - InMemoryEventBus 容忍每 topic 多 handler（内部 list），所以 monolith
#     模式从未撞到这个问题；4 进程 hybrid 切到 NATS 后立即 restart-loop。
#
# 设计：
#   _CollectingBus 是 EventBus 的薄壳：所有 subscribe 调用先 buffer 进
#   topic→[handler] dict，调用 flush() 时再按 topic 聚合：
#     - 单 handler：直接 await real_bus.subscribe(topic, handler)
#     - 多 handler：包成顺序 fan_out 函数，再 await real_bus.subscribe 一次
#
# 顺序：critical 先 collect，observer 后 collect。fan_out 按 collect 顺序
# 调用各 handler。critical handler 通常裸调（失败抛→NATS NAK 重投），
# observer handler 已被 resilient_subscription_handler(raise_on_error=False)
# 包过，永不抛、幂等。所以 critical 失败重投时 observer 跑多次也安全。
#
# 为什么不把去重逻辑放进 NatsEventBus 里？
#   - bus 层本身没有"哪些是同一进程发起的多次订阅"概念；它只看到一次次
#     subscribe call。把多 handler 聚合是 caller 侧的语义决定。
#   - 改 caller（_wire_event_subscriptions）一处比改 bus 层透明、安全。
#   - InMemoryEventBus 也不需要这个逻辑，但放壳里它免费支持。
# =============================================================================
class _CollectingBus(EventBus):
    """Buffer subscribe() calls; on flush() de-duplicate by topic via fan-out."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._pending: dict[str, list[MessageHandler]] = {}

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        # _CollectingBus 只在订阅装配阶段使用，理论上不会被 publish。
        # 留个直通实现以满足 EventBus 抽象，避免误用时静默丢消息。
        await self._bus.publish(topic, key, payload)

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        self._pending.setdefault(topic, []).append(handler)

    async def flush(self) -> None:
        for topic, handlers in self._pending.items():
            if len(handlers) == 1:
                await self._bus.subscribe(topic, handlers[0])
                continue
            # 多 handler：包 fan_out。注意 default-arg capture 防止 closure
            # 共享同一个 list 引用（topic-by-topic 循环里 handlers 会被复用）。
            ordered = tuple(handlers)

            # R3-P1-U-C：per-handler 隔离（初版）+ 2026-04-23 P1-b 并行化：
            #
            # 【隔离】每个 handler 独立 try；exception 聚合到 first_exc，所有
            # handler 都跑完后再 raise first_exc。NATS 会看到一次异常就 NAK
            # 重投，所有 handler 都被标记执行过——但单 handler 的失败不会把
            # 后续 handler 的首次执行窗口也吞掉。observer handler 已经在外层
            # resilient_subscription_handler(raise_on_error=False) 吞自身异常，
            # 不会进入这里；只有 critical handler 的真异常会被 re-raise。
            #
            # 【并行】原串行 `for h in _hs: await h(...)` 把 observer / cache
            # 的 freshness 绑在前序 handler 的耗时上，也把 NATS ack 延迟叠加
            # 起来（慢 handler 会拖慢整个 fan_out 返回）。改用 asyncio.gather
            # (return_exceptions=True) 让所有 handler 并发 await。
            #
            # 假设：handlers 是 order-independent（不同 handler 间不能依赖先
            # 后次序）。此假设本来就该成立——因为：
            #   1) 不同 topic 之间 NATS 没有 ordering 保证
            #   2) 一个 topic 多 handler 的集合本身是 ad-hoc（由
            #      _wire_event_subscriptions 累积，谁先 subscribe 谁先跑是
            #      未承诺的实现细节）
            # 若发现某 handler 对 order 有隐式依赖，那是该 handler 的 latent
            # bug，不应让 fan_out 为此付串行代价。
            #
            # asyncio.gather(return_exceptions=True) 会把 Exception 包装成
            # results item；BaseException（KeyboardInterrupt / SystemExit /
            # asyncio.CancelledError）仍会 propagate，保留中断语义。
            async def _fan_out(message: dict, _hs: tuple[MessageHandler, ...] = ordered) -> None:
                results = await asyncio.gather(
                    *(h(message) for h in _hs),
                    return_exceptions=True,
                )
                first_exc: Exception | None = None
                for r in results:
                    if isinstance(r, Exception):
                        if first_exc is None:
                            first_exc = r
                if first_exc is not None:
                    raise first_exc

            await self._bus.subscribe(topic, _fan_out)
        self._pending.clear()


async def _subscribe_critical_handlers(
    *,
    bus: EventBus,
    feature_engine: FeatureEngine | None,
    decision_trigger: DecisionCycleTrigger | None,
    order_manager: OrderManager | None,
    portfolio_service: PortfolioService | None,
    reconciliation_service: ReconciliationService | None,
    audit_service: DecisionAuditService | None,
    position_target_handler,
    market_gateway: MarketDataGateway | None = None,
) -> None:
    """订阅 critical handler。

    Stage 3 process_role 门控之后，被跳过的 slice 对应的 handler 入参为 None，
    本函数按 None 跳过相应订阅。Stage 4 引入 NATS 后，每个 process 自己只
    订阅自己关心的 topic，本函数会被进一步按 role 拆分。
    """
    if feature_engine is not None:
        await bus.subscribe(topics.MARKET_SNAPSHOTS, feature_engine.handle_market_snapshot)
    # 4 进程架构 consumer 模式：非 producer 角色（gateway / decision / execution）
    # 通过 NATS 接收 market 进程广播的 MARKET_SNAPSHOTS，更新本地
    # MarketDataGateway._latest_received_at / _latest_snapshots，使
    # is_fresh() / latest_price() / status() 反映远端实际行情状态。
    # producer 角色由 _publish_snapshot() 直接写入，不需要此订阅。
    if market_gateway is not None and not market_gateway.is_producer:
        await bus.subscribe(topics.MARKET_SNAPSHOTS, market_gateway.handle_remote_market_snapshot)
    if decision_trigger is not None:
        # 2026-04-20 decision_features_handler_queue_decoupling_sow.md §3.S1
        # 必须在 subscribe 之前调 start()：handler 有机会走 queue 路径时，queue
        # 和 dispatcher task 都得先就位。flag 默认 False → start() 对生产行为
        # 无影响，只建 infra；S2 切 flag=True 时 queue 会立即投入使用。
        await decision_trigger.start()
        await bus.subscribe(topics.FEATURE_SNAPSHOTS, decision_trigger.handle_feature_snapshot)
    if order_manager is not None:
        await bus.subscribe(topics.ORDER_INTENTS, order_manager.handle_order_intent)
    if portfolio_service is not None:
        await bus.subscribe(topics.FILL_EVENTS, portfolio_service.handle_fill_event)
    if audit_service is not None:
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
    if reconciliation_service is not None:
        await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, reconciliation_service.handle_portfolio_snapshot)
    if position_target_handler is not None:
        await bus.subscribe(topics.POSITION_TARGETS, position_target_handler)


def _observer_subscription_specs(
    *,
    audit_service: DecisionAuditService | None,
    ai_service: AIInferenceService | None,
    reconciliation_service: ReconciliationService | None,
) -> tuple[ObserverSubscriptionSpec, ...]:
    """生成 observer 订阅清单。

    Stage 3 process_role 门控之后，被跳过的 slice 对应的 service 入参为 None，
    本函数会跳过对应的 spec。monolith 模式下三个 service 都不为 None，输出
    与原版本完全相同的 22 条 spec。
    """
    specs: list[ObserverSubscriptionSpec] = []
    if audit_service is not None:
        specs.extend(
            [
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
            ]
        )
    if ai_service is not None:
        specs.extend(
            [
                ObserverSubscriptionSpec(topics.PORTFOLIO_SNAPSHOTS, "ai.handle_portfolio_snapshot", ai_service.handle_portfolio_snapshot),
                ObserverSubscriptionSpec(topics.RECONCILIATION_REPORTS, "ai.handle_reconciliation_report", ai_service.handle_reconciliation_report),
            ]
        )
    if audit_service is not None:
        specs.append(
            ObserverSubscriptionSpec(topics.RECONCILIATION_REPORTS, "audit.handle_reconciliation_report", audit_service.handle_reconciliation_report),
        )
    if reconciliation_service is not None:
        specs.append(
            ObserverSubscriptionSpec(topics.PROCESSING_FAILURES, "reconciliation.handle_processing_failure", reconciliation_service.handle_processing_failure),
        )
    return tuple(specs)


async def _subscribe_observer_handlers(
    *,
    bus: EventBus,
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


# =============================================================================
# Slice 化分解（多进程切片化重构 — Stage 2）
#
# build_runtime() 历史上是一个 778 行的单体函数。为了支持 Stage 3 的
# AATS_PROCESS_ROLE 多进程门控，我们把它拆分为 8 个 slice builder：
#
#   1. _build_shared_runtime_slice  — 全部进程都需要的基础设施
#                                     (metrics/bus/kill_switch/market_gateway/account/health…)
#   2. _build_market_slice          — feature_engine（market_proc）
#   3. _build_decision_slice        — ai/decision/policy/risk/strategy_coordinator/position_target_handler
#   4. _build_execution_slice       — order_manager/obligation/outbox/command_processor
#   5. _build_portfolio_slice       — portfolio_service/sleeve_pnl_projection/funding_fee_sync
#   6. _build_reconciliation_slice  — reconciliation_service/recovery_service
#   7. _wire_event_subscriptions    — critical + observer 订阅装配（async）
#   8. _apply_post_init_guards      — derivatives/trial guard + strategy profile control
#
# Stage 2 仅做结构化重排，不改变行为；所有 slice 在 monolith 模式下都会被调用。
# Stage 3 将在每个 slice 顶部加 `if process_role and process_role not in {...}: return`
# 实现按进程角色挑选 slice。
#
# ── 跨 slice 依赖图（Stage 3 process_role 门控时必须遵守的顺序）─────────────
#
#   shared ┬─→ market
#          ├─→ decision ──┐
#          ├─→ execution ←┘  (execution 读 decision.risk_engine 作 leg evaluator)
#          ├─→ portfolio ←──── execution.portfolio_outbox_publisher
#          └─→ reconciliation
#
# 装配顺序必须为 shared → market → decision → execution → portfolio → reconciliation；
# 任何 slice 在跳过上游依赖的情况下被启用，都会读取到 None 字段并触发 Stage 3
# 计划好的 assertion 失败。每个 _build_*_slice 函数 docstring 顶部都列出了
# 自己的"跨 slice 依赖"清单，作为单一权威来源。
#
# Stage 3 引入 process_role 门控后的 slice → role 矩阵：
#
#   slice           | gateway | market | decision | execution | monolith |
#   ----------------|---------|--------|----------|-----------|----------|
#   shared          |  装     |  装    |  装      |  装       |  装      |
#   market          |  跳     |  装    |  跳      |  跳       |  装      |
#   decision        |  跳     |  跳    |  装      |  跳       |  装      |
#   execution       |  跳     |  跳    |  跳      |  装       |  装      |
#   portfolio       |  跳     |  跳    |  跳      |  装       |  装      |
#   reconciliation  |  跳     |  跳    |  跳      |  装       |  装      |
#   startup recovery|  跳     |  跳    |  跳      |  装       |  装      |
#
# `gateway` 在 Stage 3 仅持有 shared slice（FastAPI/UI 静态资源等 gateway 专属
# 装配尚未实装，会在后续 stage 引入；目前 gateway role 等价于"只装基础设施"）。
# =============================================================================


# Stage 3：每个 slice 在哪些 role 下需要装。
# None / "monolith" 视为单进程模式 → 全部 slice 都装。
_SLICE_REQUIRED_ROLES: dict[str, frozenset[str | None]] = {
    "shared": frozenset(
        {None, PROCESS_ROLE_MONOLITH, PROCESS_ROLE_GATEWAY, PROCESS_ROLE_MARKET, PROCESS_ROLE_DECISION, PROCESS_ROLE_EXECUTION}
    ),
    "market": frozenset({None, PROCESS_ROLE_MONOLITH, PROCESS_ROLE_MARKET}),
    "decision": frozenset({None, PROCESS_ROLE_MONOLITH, PROCESS_ROLE_DECISION}),
    "execution": frozenset({None, PROCESS_ROLE_MONOLITH, PROCESS_ROLE_EXECUTION}),
    "portfolio": frozenset({None, PROCESS_ROLE_MONOLITH, PROCESS_ROLE_EXECUTION}),
    "reconciliation": frozenset({None, PROCESS_ROLE_MONOLITH, PROCESS_ROLE_EXECUTION}),
    "startup_recovery": frozenset({None, PROCESS_ROLE_MONOLITH, PROCESS_ROLE_EXECUTION}),
}


# =============================================================================
# Profile 语义 × Topology 能力矩阵
#
# profile 定义业务语义（position_mode、strategy family 等），部署拓扑定义进程
# 角色组合。并非所有语义都能在所有拓扑下运行。
#
# 拓扑分类：
#   "monolith"  — 单进程，所有 slice 同进程（None / "monolith" role）
#   "split_de"  — decision + execution 共享进程（未来 Stage 5 / 6）
#   "4proc"     — gateway / market / decision / execution 各自独立
#
# 已解除的阻断关系（Stage 4 完成）：
#   derivatives + hedge + 4proc-execution —— execution slice 现在本地构造
#   RiskEngine 实例（slices.execution_leg_risk_engine），所有依赖来自
#   shared slice / storage 层，无需 decision 共处同进程。
# =============================================================================

# 拓扑类型标识（用于能力矩阵查询）
_TOPOLOGY_MONOLITH = "monolith"
_TOPOLOGY_4PROC = "4proc"


def _infer_topology_kind(effective_process_role: str | None) -> str:
    """从 effective_process_role 推导当前拓扑类型。"""
    if effective_process_role in {None, PROCESS_ROLE_MONOLITH}:
        return _TOPOLOGY_MONOLITH
    return _TOPOLOGY_4PROC


# 矩阵条目：(条件描述, 是否阻断的判断函数, 错误码, 人可读消息)
# 新增 profile 语义 × 拓扑约束只需在此表追加条目，不必散落在各 slice builder。
_TOPOLOGY_CAPABILITY_RULES: list[
    tuple[
        str,                                           # rule_id
        Callable[[AATSSettings, str], bool],           # predicate(settings, topology_kind) → blocked?
        str,                                           # error_code
        str,                                           # human_message
    ]
] = [
    # ── 四进程拓扑必须使用跨进程事件总线 ─────────────────────────────
    # 限定条件：仅在实盘/交易所耦合场景下校验（live_submit_enabled 或
    # account_backend=okx），避免阻断本地 smoke 测试和纯模拟 profile。
    (
        "4proc_requires_cross_process_event_bus",
        lambda s, topo: (
            topo == _TOPOLOGY_4PROC
            and s.event_bus_backend == EVENT_BUS_BACKEND_IN_MEMORY
            and (
                s.live_submit_enabled
                or s.mode == "guarded_live"
                or (s.account_backend == "okx" and s.execution_backend == "okx")
            )
        ),
        "4proc_requires_cross_process_event_bus",
        "四进程模式下不能使用 in_memory 事件总线——跨进程事件无法送达。"
        " 请设置 AATS_EVENT_BUS_BACKEND=hybrid（推荐）或 nats。",
    ),
    # ── 四进程拓扑必须使用 Redis 热状态 ──────────────────────────────
    (
        "4proc_requires_redis_hot_state",
        lambda s, topo: (
            topo == _TOPOLOGY_4PROC
            and s.hot_state_backend == "memory"
            and (
                s.live_submit_enabled
                or s.mode == "guarded_live"
                or (s.account_backend == "okx" and s.execution_backend == "okx")
            )
        ),
        "4proc_requires_redis_hot_state",
        "四进程模式下不能使用 memory 热状态——跨进程状态无法共享。"
        " 请设置 AATS_HOT_STATE_BACKEND=redis 并配置 AATS_HOT_STATE_REDIS_URL。",
    ),
]


def _validate_topology_capability(
    settings: AATSSettings,
    *,
    effective_process_role: str | None,
) -> None:
    """校验 profile 语义与当前部署拓扑是否兼容。

    在 build_runtime() 的 settings 解析完成、slice 构建之前调用。
    不兼容时抛出 RuntimeError 并给出明确的错误码和修复建议。
    """
    topology_kind = _infer_topology_kind(effective_process_role)
    for rule_id, predicate, error_code, message in _TOPOLOGY_CAPABILITY_RULES:
        try:
            blocked = predicate(settings, topology_kind)
        except Exception:
            # 防御性：predicate 内部异常不阻断启动，仅 warning
            _log.warning("topology_capability_rule_predicate_error rule=%s", rule_id)
            continue
        if blocked:
            raise RuntimeError(
                f"{error_code}: {message} "
                f"[rule={rule_id} topology={topology_kind} "
                f"process_role={effective_process_role}]"
            )


def _slice_active(slice_name: str, *, effective_process_role: str | None) -> bool:
    """判断给定 slice 是否在当前 process_role 下需要装。

    None / "monolith" 都视为单进程模式：所有 slice 都装。
    其他 role：按 _SLICE_REQUIRED_ROLES 表过滤。

    slice_name 必须是 _SLICE_REQUIRED_ROLES 已定义的键，否则 KeyError —
    这是故意的，避免新增 slice 时漏配门控。
    """
    return effective_process_role in _SLICE_REQUIRED_ROLES[slice_name]


@dataclass(slots=True)
class _RuntimeSlices:
    """临时容器：build_runtime 内部各 slice 之间共享的中间对象。

    所有字段默认为 None；slice builder 按顺序填入。
    Stage 3 引入 process_role 之后，未启用的 slice 将留为 None，
    后续的 slice 必须 None-check 自己依赖的上游字段。
    """

    # ---- shared / 基础 ----
    metrics: Any = None
    stream_snapshot_cache: Any = None
    bus: Any = None
    # Stage 6 Slice 6.4：合并的 KillSwitch 类同时承担本地 sync read/write 与
    # 跨进程同步边车。在 _start_event_bus 完成后由 build_runtime 调用
    # await slices.kill_switch.bootstrap(...) 注入 hot_state_store + bus。
    kill_switch: Any = None
    # Stage 6 Slice 6.3：跨进程 portfolio_snapshot 缓存边车。在 _start_event_bus
    # 完成后由 build_runtime 构造 + bootstrap，订阅 portfolio.snapshots 让 4 进
    # 程的 latest snapshot 视图保持收敛。
    portfolio_snapshot_cache: Any = None
    # Stage 6 Slice 6.5：跨进程 obligation 缓存边车。在 _start_event_bus 完成后
    # 由 build_runtime 构造 + bootstrap，订阅 execution.obligation_updates 让 4
    # 进程的 obligation 视图保持收敛。slice builder 从本字段取 cache 引用注入
    # ObligationService / RiskEngine / query_service 等读写路径。
    obligation_hot_state_cache: Any = None
    # P1-1 热路径优化：OrderState 跨进程缓存边车。
    order_state_hot_cache: Any = None
    # P1-2 热路径优化：FillEvent 跨进程缓存边车。
    fill_event_hot_cache: Any = None
    # 跨进程 account snapshot 缓存边车。execution role 在每次 refresh 后 publish，
    # 非 execution role 订阅 NATS + Redis hydrate，让 account_service._latest_snapshot
    # 保持跨进程同步。设计文档见 account_snapshot_cache.py 模块 docstring。
    account_snapshot_cache: Any = None
    mode_controller: Any = None
    normalizer: Any = None
    market_publisher: Any = None
    okx_client: Any = None
    okx_ws_client: Any = None
    market_gateway: Any = None
    private_account_ws_client: Any = None
    account_service: Any = None
    fee_resolver: Any = None
    baseline_import_service: Any = None
    bootstrap_from_exchange: bool = False
    execution_adapter: Any = None
    health_service: Any = None
    snapshot_builder: Any = None
    reconciliation_classifier: Any = None
    phase1_shadow_monitor: Any = None
    phase1_shadow: Any = None

    # ---- market ----
    feature_engine: Any = None
    long_short_poller: Any = None  # P2.7 optional 后台 poller，flag off 时 None

    # ---- decision ----
    ai_service: Any = None
    strategy_coordinator: Any = None
    decision_trigger_policy: Any = None
    decision_engine: Any = None
    decision_trigger: Any = None
    audit_service: Any = None
    policy_engine: Any = None
    risk_engine: Any = None
    execution_planner: Any = None
    position_target_handler: Any = None

    # ---- housekeeping ----
    housekeeping: Any = None

    # ---- execution ----
    obligation_service: Any = None
    exit_execution_writer: Any = None
    execution_outbox_publisher: Any = None
    portfolio_outbox_publisher: Any = None
    execution_order_service: Any = None
    order_manager: Any = None
    execution_command_processor: Any = None
    # Stage 4：4 进程 execution role 下本地构造的 RiskEngine，仅用于
    # OrderManager.leg_risk_evaluator。monolith / decision role 下为 None
    # （monolith 复用 decision slice 的 risk_engine）。
    # _bootstrap_derivatives_live_runtime_guards 需要访问此实例以注入
    # live_runtime_guard_provider / trial_guard_provider / recovery_status_provider。
    execution_leg_risk_engine: Any = None

    # ---- portfolio ----
    portfolio_state: Any = None
    sleeve_pnl_projection_service: Any = None
    portfolio_service: Any = None
    funding_fee_sync_service: Any = None

    # ---- reconciliation ----
    reconciliation_service: Any = None
    base_recovery_service: Any = None
    recovery_service: Any = None


def _construct_event_bus(
    *,
    runtime_settings: AATSSettings,
    event_store: Any,
    process_role: str | None,
    stream_snapshot_cache: StreamSnapshotCache | None = None,
) -> EventBus:
    """Stage 4 工厂：按 settings.event_bus_backend 选择 EventBus 实现。

    返回值：
        - "in_memory" → InMemoryEventBus（向后兼容默认，monolith 唯一选择）
        - "hybrid"    → HybridEventBus(critical=NatsEventBus, observer=InMemoryBus)
        - "nats"      → NatsEventBus（全部 topic 都走 NATS，Stage 5+）

    本函数 **不做任何 I/O**：返回的 bus 实例尚未 connect/ensure_stream。
    生命周期启动统一在 build_runtime 调用 ``await bus.start()``，避免让
    `_build_shared_runtime_slice` 变成 async 而破坏 6 个 slice builder 的对称性。

    Why fail-fast: 4 进程拓扑必须显式选 hybrid/nats；如果环境配错误地把
    backend 设为 in_memory，跨进程的 fill / decision 事件会因为 InMemoryBus
    没有跨进程能力而静默丢失。设计上不会自动从 hybrid 退化到 in_memory。
    """
    backend = runtime_settings.event_bus_backend
    persistence_mode = runtime_settings.event_persistence_mode
    consumer_role = process_role or "monolith"

    if backend == "in_memory":
        return InMemoryEventBus(
            event_store=event_store,
            persistence_mode=persistence_mode,
            stream_snapshot_cache=stream_snapshot_cache,
        )

    # slice nats-capacity（§7.5a R1）：runtime 路径使用分层 stream 拓扑。
    # streams 字段通过 build_nats_streams_from_env(DEFAULT_STREAM_SPECS) 构造，
    # 支持通过 AATS_NATS_MARKET_MAX_* / AATS_NATS_EVENTS_MAX_* 环境变量覆盖
    # 单条 stream 的容量参数（max_bytes / max_msgs / max_msg_size /
    # max_age_seconds）。默认三条 stream，均以 1 天为时间上限/兜底：
    #   - AATS_EVENTS_MARKET  : 2 GiB，承载 MARKET_SNAPSHOTS / FEATURE_SNAPSHOTS
    #   - AATS_EVENTS         : 4 GiB，承载其他 critical 事件
    #   - AATS_EVENTS_COMMANDS: 512 MiB，承载命令类事件
    # legacy 字段 stream_name / stream_max_age_seconds 不再被 runtime 路径
    # 读取，只有 ensure_stream(topics=...) legacy shim 会读（给单元测试用）。
    # 2026-04-20 code review Issue 2+3 fix:
    #   诊断报告观察到 aats-decision-features_snapshots consumer
    #   pending=142,441, ack_pending=256/256 打满, redelivered=8,580.
    #   根因: decision run_cycle 单轮 17s (15 次同步 OKX REST), features
    #   进入速率 17/min >> 决策处理 3-4/min, backlog 持续增长触发 NATS
    #   storage 80% 阈值.
    # 缓解:
    #   (a) FEATURE_SNAPSHOTS 已在 SNAPSHOT_DELIVERY_TOPICS → DeliverPolicy.LAST
    #       (只消费最新, 不回放历史积压). 但**现有 durable consumer 不会自动
    #       更新**, 需运维侧删除 consumer 让新 deploy 按新 policy 重建.
    #   (b) per-topic max_ack_pending 降到 32: 让 NATS 不再一次推 256 条到
    #       decision buffer, 避免 ack_pending 常年打满的死循环.
    #   (c) per-topic ack_wait 90s: 给 decision run_cycle 17s 留 5x buffer,
    #       避免 30s 超时导致的 redelivered=8580 次死循环重投.
    #   同样 policy 也应用到 market_snapshots (同 pattern, 同 run_cycle 消费者).
    _slow_consumer_backpressure_topics = {
        topics.FEATURE_SNAPSHOTS: 32,
        topics.MARKET_SNAPSHOTS: 32,
    }
    _slow_consumer_ack_wait_topics = {
        topics.FEATURE_SNAPSHOTS: 90.0,
        topics.MARKET_SNAPSHOTS: 90.0,
    }
    nats_config = NatsBusConfig(
        servers=(runtime_settings.nats_url,),
        streams=build_nats_streams_from_env(DEFAULT_STREAM_SPECS),
        stream_name=runtime_settings.nats_stream_name,
        stream_max_age_seconds=float(runtime_settings.nats_stream_max_age_seconds),
        per_topic_max_ack_pending=_slow_consumer_backpressure_topics,
        per_topic_ack_wait_seconds=_slow_consumer_ack_wait_topics,
    )

    if backend == "nats":
        # Stage 5+：全部 topic 都走 NATS。critical + observer 都进 stream。
        return NatsEventBus(
            config=nats_config,
            event_store=event_store,
            persistence_mode=persistence_mode,
            consumer_role=consumer_role,
            stream_snapshot_cache=stream_snapshot_cache,
        )

    if backend == "hybrid":
        # Stage 4 主路径：critical → NATS file storage 跨进程；observer → memory
        critical_bus = NatsEventBus(
            config=nats_config,
            event_store=event_store,
            persistence_mode=persistence_mode,
            consumer_role=consumer_role,
            stream_snapshot_cache=stream_snapshot_cache,
        )
        observer_bus = InMemoryEventBus(
            event_store=None,  # 双写已由 critical_bus 接管，observer 不重复落盘
            persistence_mode="permissive",
        )
        return HybridEventBus(
            critical_bus=critical_bus,
            observer_bus=observer_bus,
            routing=HybridBusRouting(),
        )

    raise ValueError(
        f"unsupported event_bus_backend: {backend!r} "
        f"(this should have been rejected by AATSSettings validator)"
    )


async def _start_event_bus(bus: EventBus) -> None:
    """生命周期启动钩子：对支持 ``start()`` 的 bus 实现调用一次。

    InMemoryEventBus 没有 start，跳过；HybridEventBus / NatsEventBus 有 start，
    会触发 NATS connect + JetStream ensure_stream。
    """
    start_method = getattr(bus, "start", None)
    if start_method is None:
        return
    await start_method()


def _build_shared_runtime_slice(
    *,
    runtime_settings: AATSSettings,
    runtime_layering: RuntimeLayering,
    storage: StorageBackends,
    slices: _RuntimeSlices,
    effective_process_role: str | None,
) -> None:
    """构造全部进程都需要的基础设施。

    包含：metrics、bus、kill_switch、mode_controller、市场网关链路、
    账户服务、费率求解、execution_adapter、health_service、phase1_shadow。
    这些都是"读多写少"或纯共享，未来 4 进程都需要其中一部分。

    Stage 3 process_role 门控：本 slice 在所有 role 下都装。effective_process_role
    参数仅作为接口对称（与其他 slice builder 一致），不会在内部短路。
    """
    if not _slice_active("shared", effective_process_role=effective_process_role):
        return
    slices.metrics = MetricsRegistry()
    # 高频流式快照缓存：替代 Postgres 为 market/features snapshots 提供查询。
    # recent 深度按 topic 独立配置，策略 lookback 需求驱动 MARKET_SNAPSHOTS
    # 深度，FEATURE_SNAPSHOTS 保持默认值减少内存开销。
    _market_recent_depth = max(
        getattr(runtime_settings, "spot_grid_anchor_lookback_snapshots", 50),
        50,
    )
    slices.stream_snapshot_cache = StreamSnapshotCache(
        default_max_recent=50,
        max_recent_by_topic={
            topics.MARKET_SNAPSHOTS: _market_recent_depth,
        },
    )
    # Stage 4: bus 实现按 settings.event_bus_backend 选择。
    # 注意 _construct_event_bus 不做 I/O；NATS 连接和 stream 创建在
    # build_runtime 调用 await _start_event_bus(slices.bus) 时才发生。
    slices.bus = _construct_event_bus(
        runtime_settings=runtime_settings,
        event_store=storage.event_store,
        process_role=effective_process_role,
        stream_snapshot_cache=slices.stream_snapshot_cache,
    )
    _backfill_fill_outcomes_from_event_store(
        event_store=storage.event_store,
        fill_outcome_repo=storage.fill_outcome_repo,
        execution_repo=storage.execution_repo,
    )

    slices.kill_switch = KillSwitch()
    slices.mode_controller = RuntimeModeController(
        settings=runtime_settings,
        kill_switch=slices.kill_switch,
        runtime_layering=runtime_layering,
    )

    slices.normalizer = MarketSnapshotNormalizer(exchange_name=runtime_settings.exchange_name)
    slices.market_publisher = MarketSnapshotPublisher(bus=slices.bus)
    slices.okx_client = OKXRESTClient(settings=runtime_settings)
    _market_is_producer = _slice_active("market", effective_process_role=effective_process_role)
    # OKX 公共 WebSocket 客户端仅 producer 角色（market / monolith）需要。
    # consumer 角色（gateway / decision / execution）通过 NATS 订阅接收快照，
    # 不直连 OKX WS——无谓创建实例既浪费资源又容易在 status() 中产生误导。
    slices.okx_ws_client = (
        OKXPublicWebSocketClient(settings=runtime_settings)
        if runtime_settings.market_data_backend == "okx" and _market_is_producer
        else None
    )
    slices.market_gateway = MarketDataGateway(
        settings=runtime_settings,
        normalizer=slices.normalizer,
        publisher=slices.market_publisher,
        okx_ws_client=slices.okx_ws_client,
        okx_rest_client=slices.okx_client if runtime_settings.market_data_backend == "okx" else None,
        is_producer=_market_is_producer,
    )
    slices.private_account_ws_client = (
        OKXPrivateWebSocketClient(settings=runtime_settings)
        if runtime_settings.account_backend == "okx" and runtime_settings.account_read_enabled
        else None
    )
    slices.account_service = OKXAccountService(
        settings=runtime_settings,
        client=slices.okx_client,
        private_ws_client=slices.private_account_ws_client,
    )
    slices.fee_resolver = EffectiveFeeResolver(
        settings=runtime_settings,
        account_service=slices.account_service,
    )
    slices.baseline_import_service = AccountBaselineImportService(
        event_store=storage.event_store,
        reconciliation_repo=storage.reconciliation_repo,
    )
    slices.bootstrap_from_exchange = runtime_layering.recovery_policy.startup_baseline_import_supported
    slices.execution_adapter = _build_execution_adapter(
        settings=runtime_settings,
        market_gateway=slices.market_gateway,
        account_service=slices.account_service,
        obligation_repo=storage.obligation_repo,
        mode_controller=slices.mode_controller,
        environment_capabilities=runtime_layering.environment_capabilities,
        policy_profile=runtime_layering.policy_profile,
    )
    slices.health_service = SystemHealthService(
        settings=runtime_settings,
        mode_controller=slices.mode_controller,
        kill_switch=slices.kill_switch,
        market_provider=slices.market_gateway,
        account_provider=slices.account_service,
        execution_provider=slices.execution_adapter,
        reconciliation_repo=storage.reconciliation_repo,
        recovery_policy=runtime_layering.recovery_policy,
    )
    if isinstance(slices.execution_adapter, OKXExecutionAdapter):
        slices.execution_adapter.health_service = slices.health_service

    slices.snapshot_builder = PortfolioSnapshotBuilder(pnl_calculator=PortfolioPnLCalculator())
    slices.phase1_shadow_monitor = Phase1ShadowMonitor(
        execution_repo=storage.execution_repo,
        obligation_repo=storage.obligation_repo,
        state_scope=runtime_state_scope(runtime_settings),
        execution_shadow_service=storage.phase1_execution_shadow_service,
        ledger_mirror_service=storage.phase1_ledger_mirror_service,
        execution_order_repo=storage.execution_order_repo,
        execution_fill_repo=storage.execution_fill_repo_v2,
        reservation_repo=storage.reservation_repo_v2,
    )
    slices.phase1_shadow = storage.phase1_shadow or Phase1ShadowSubsystem(
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
    slices.phase1_shadow.monitor = slices.phase1_shadow_monitor
    slices.health_service.phase1_shadow_provider = slices.phase1_shadow_monitor
    slices.reconciliation_classifier = (
        RecoveryReconciliationClassifier()
        if runtime_settings.recovery_reconciliation_execution_ledger_enabled
        else None
    )


def _build_market_slice(
    *,
    slices: _RuntimeSlices,
    effective_process_role: str | None,
    runtime_settings: AATSSettings,
) -> None:
    """构造 market 进程独占资源：feature_engine。

    feature_engine 当前是同步 Python 实现，未来在 Stage 7 会改为
    multiprocessing.Pool 旁挂以绕过 GIL。

    Stage 3 process_role 门控：仅 None / monolith / market 时构造，
    其他 role 跳过，slices.feature_engine 保留为 None。

    Bug-1 时序平滑：FeatureCalculator 从 settings 读 rolling 窗口参数和
    feature flag；warmup（OKX REST 拉历史 K 线灌入 RollingCandleState）
    由启动序列在 bus.start 前按需触发（见 build_runtime）。
    """
    if not _slice_active("market", effective_process_role=effective_process_role):
        return
    # P2.9 RegimeClassifier 按 settings 配置 ADX 阈值. 在此构造传给 FeatureCalculator,
    # calculator 内部再按 enable_regime_adx 决定走 classify_with_adx 还是 classify.
    regime_classifier = RegimeClassifier(
        adx_trend_threshold=runtime_settings.strategy_baseline_regime_adx_trend_threshold,
        adx_range_threshold=runtime_settings.strategy_baseline_regime_adx_range_threshold,
    )
    # P2.7 Long-Short ratio poller: 仅当 flag 开时才构造并后台轮询.
    # poller 没启动时 FeatureCalculator 读缓存返回 None → ls_alpha=0 退化.
    long_short_poller: LongShortRatioPoller | None = None
    if runtime_settings.strategy_baseline_ls_ratio_signal_enabled:
        long_short_poller = LongShortRatioPoller(
            okx_rest_url=runtime_settings.okx_rest_url,
            poll_interval_seconds=runtime_settings.strategy_baseline_ls_ratio_poll_interval_seconds,
            timeout_seconds=float(runtime_settings.okx_timeout_seconds),
            period=runtime_settings.strategy_baseline_ls_ratio_period,
        )
    calculator = FeatureCalculator(
        regime=regime_classifier,
        enable_timeseries_smoothing=runtime_settings.strategy_baseline_timeseries_smoothing_enabled,
        rolling_max_bars=runtime_settings.strategy_baseline_rolling_max_bars,
        rolling_roc_window=runtime_settings.strategy_baseline_rolling_roc_window,
        rolling_atr_window=runtime_settings.strategy_baseline_rolling_atr_window,
        enable_basis_signal=runtime_settings.strategy_baseline_basis_signal_enabled,
        basis_scale_bps=runtime_settings.strategy_baseline_basis_scale_bps,
        enable_funding_signal=runtime_settings.strategy_baseline_funding_signal_enabled,
        funding_scale=runtime_settings.strategy_baseline_funding_scale,
        enable_oi_signal=runtime_settings.strategy_baseline_oi_signal_enabled,
        oi_max_snapshots=runtime_settings.strategy_baseline_oi_max_snapshots,
        oi_ema_period=runtime_settings.strategy_baseline_oi_ema_period,
        oi_dead_zone=runtime_settings.strategy_baseline_oi_dead_zone,
        enable_regime_adx=runtime_settings.strategy_baseline_regime_adx_enabled,
        long_short_poller=long_short_poller,
        enable_ls_ratio_signal=runtime_settings.strategy_baseline_ls_ratio_signal_enabled,
        ls_ratio_scale=runtime_settings.strategy_baseline_ls_ratio_scale,
        ls_ratio_max_staleness_seconds=runtime_settings.strategy_baseline_ls_ratio_max_staleness_seconds,
    )
    slices.feature_engine = FeatureEngine(bus=slices.bus, calculator=calculator)
    # 把 poller 引用挂到 slices 以便 start_background_tasks 拿到并启动后台 loop
    slices.long_short_poller = long_short_poller


def _build_decision_slice(
    *,
    runtime_settings: AATSSettings,
    storage: StorageBackends,
    runtime_layering: RuntimeLayering,
    slices: _RuntimeSlices,
    effective_process_role: str | None,
) -> None:
    """构造 decision 进程独占资源。

    包含：ai_service、strategy_coordinator、decision_engine、policy/risk、
    execution_planner（属于 decision 计划阶段）和 position_target_handler。
    全部对 storage 的写入均集中在 decision_proc 内，避免跨进程冲突。

    跨 slice 依赖（Stage 3 process_role 门控时必须保证已构造）：
      - shared slice: bus、market_gateway、health_service、account_service、
        kill_switch、mode_controller、fee_resolver、metrics
    本 slice 不依赖 market/execution/portfolio/reconciliation slice，但
    risk_engine 与 execution_planner 会被 _build_execution_slice 反向引用。

    Stage 3 process_role 门控：仅 None / monolith / decision 时构造。
    其他 role 跳过，相关 slices.* 字段保留为 None。
    """
    if not _slice_active("decision", effective_process_role=effective_process_role):
        return
    _feature_resolver = FeatureSnapshotResolver(
        event_store=storage.event_store,
        stream_snapshot_cache=slices.stream_snapshot_cache,
    )
    execution_truth_repo = _storage_execution_truth_repo(storage)
    slices.ai_service = AIInferenceService(
        settings=runtime_settings,
        event_store=storage.event_store,
        bus=slices.bus,
        execution_repo=execution_truth_repo,
        prompt_builder=PromptBuilder(),
        validator=AssessmentValidator(),
        fee_resolver=slices.fee_resolver,
        feature_resolver=_feature_resolver,
    )
    slices.strategy_coordinator = StrategyCoordinatorService(
        settings=runtime_settings,
        event_store=storage.event_store,
        market_gateway=slices.market_gateway,
        portfolio_repo=storage.portfolio_repo,
        execution_repo=execution_truth_repo,
        position_lot_repo=storage.position_lot_repo,
        account_service=slices.account_service,
        strategy_sleeve_repo=storage.strategy_sleeve_repo,
        strategy_runtime_repo=storage.strategy_runtime_repo,
        reconciliation_repo=storage.reconciliation_repo,
        sleeve_pnl_repo=storage.sleeve_pnl_repo,
        stream_snapshot_cache=slices.stream_snapshot_cache,
    )
    slices.decision_trigger_policy = DecisionTriggerPolicy(settings=runtime_settings)
    # Round 3 · 2026-04-22 · Non-AI paper trading shadow 服务 (optional).
    # 只在 settings.paper_trading_shadow_enabled + candidates 非空时实例化；
    # 否则传 None，orchestrator 里走零开销 skip 路径。
    _paper_trading_shadow_service = None
    if runtime_settings.paper_trading_shadow_enabled and runtime_settings.paper_trading_shadow_candidates:
        from aats.services.strategy_engines.paper_trading_shadow import (
            PaperTradingShadowService,
        )

        _paper_trading_shadow_service = PaperTradingShadowService(
            base_settings=runtime_settings,
            fee_resolver=slices.fee_resolver,
            metrics=slices.metrics,
        )
    slices.decision_engine = DecisionOrchestrator(
        bus=slices.bus,
        context_builder=DecisionContextBuilder(
            settings=runtime_settings,
            event_store=storage.event_store,
            portfolio_repo=storage.portfolio_repo,
            execution_repo=execution_truth_repo,
            mode_controller=slices.mode_controller,
            health_service=slices.health_service,
            account_service=slices.account_service,
            stream_snapshot_cache=slices.stream_snapshot_cache,
            portfolio_snapshot_cache=slices.portfolio_snapshot_cache,
            order_state_cache=slices.order_state_hot_cache,
            fill_event_cache=slices.fill_event_hot_cache,
        ),
        baseline_strategy=BaselineStrategy(
            event_store=storage.event_store,
            feature_resolver=_feature_resolver,
            settings=runtime_settings,
        ),
        ai_service=slices.ai_service,
        target_engine=TargetPositionEngine(
            settings=runtime_settings,
            fee_resolver=slices.fee_resolver,
            metrics=slices.metrics,
        ),
        strategy_coordinator=slices.strategy_coordinator,
        paper_trading_shadow_service=_paper_trading_shadow_service,
        metrics=slices.metrics,
    )
    _kill_switch = slices.kill_switch
    slices.decision_trigger = DecisionCycleTrigger(
        orchestrator=slices.decision_engine,
        market_gateway=slices.market_gateway,
        policy=slices.decision_trigger_policy,
        can_trigger=lambda *, symbol: (
            False,
            "kill_switch_active",
        )
        if _kill_switch.halted
        else (
            True,
            "ready",
        )
        if runtime_settings.symbol_allowed_for_decision_cycle(symbol)
        else (
            False,
            "symbol_not_enabled_for_decision_cycle",
        ),
        metrics=slices.metrics,  # LF-019：decision_cycle_dropped_triggers_total 计数
    )
    slices.audit_service = DecisionAuditService(bus=slices.bus, audit_repo=storage.audit_repo)
    slices.policy_engine = PolicyEngine(
        settings=runtime_settings,
        kill_switch=slices.kill_switch,
        mode_controller=slices.mode_controller,
        health_service=slices.health_service,
        environment_capabilities=runtime_layering.environment_capabilities,
        policy_profile=runtime_layering.policy_profile,
    )
    slices.risk_engine = RiskEngine(
        settings=runtime_settings,
        account_service=slices.account_service,
        health_service=slices.health_service,
        trigger_policy=slices.decision_trigger_policy,
        price_provider=slices.market_gateway.latest_price,
        mode_controller=slices.mode_controller,
        obligation_repo=storage.obligation_repo,
        environment_capabilities=runtime_layering.environment_capabilities,
        policy_profile=runtime_layering.policy_profile,
        fee_resolver=slices.fee_resolver,
        reconciliation_repo=storage.reconciliation_repo,
        # Stage 6 Slice 6.5：注入跨进程 obligation 缓存。risk.py
        # _active_local_obligations 的读路径优先走 cache.active_sync() 替代
        # obligation_repo Postgres SELECT。cache 未接线 / 未 bootstrap 时完全
        # 退化到 repo 原路径（I5 miss 不破坏读）。
        obligation_cache=slices.obligation_hot_state_cache,
    )
    slices.execution_planner = ExecutionPlanner(settings=runtime_settings)
    slices.position_target_handler = _build_position_target_handler(
        settings=runtime_settings,
        mode_controller=slices.mode_controller,
        runtime_layering=runtime_layering,
        account_service=slices.account_service,
        policy_engine=slices.policy_engine,
        risk_engine=slices.risk_engine,
        execution_planner=slices.execution_planner,
        market_gateway=slices.market_gateway,
        kill_switch=slices.kill_switch,
        metrics=slices.metrics,
        bus=slices.bus,
        event_store=storage.event_store,
        execution_repo=execution_truth_repo,
        strategy_runtime_repo=storage.strategy_runtime_repo,
    )


def _build_execution_slice(
    *,
    runtime_settings: AATSSettings,
    storage: StorageBackends,
    runtime_layering: RuntimeLayering,
    slices: _RuntimeSlices,
    effective_process_role: str | None,
) -> None:
    """构造 execution 进程独占资源：order_manager 及上下游 outbox/obligation。

    跨 slice 依赖（Stage 3 process_role 门控时必须保证已构造）：
      - shared slice: bus、market_gateway、account_service、fee_resolver、
        execution_adapter、kill_switch、health_service、mode_controller、metrics

    derivatives + hedge 模式下的 leg_risk_evaluator：
      - monolith: 直接复用 decision slice 构造的 slices.risk_engine
      - 4 进程 execution role: 本地构造一个 RiskEngine 实例，所有依赖
        （account_service、health_service、price_provider、obligation_repo 等）
        均来自 shared slice / storage 层，无跨进程调用。本地实例使用
        execution 进程自身的 account_service，数据比 decision 进程更新鲜
        （已反映最近的成交 / 保证金变化），风控评估更准确。

    Stage 3 process_role 门控：仅 None / monolith / execution 时构造。
    """
    if not _slice_active("execution", effective_process_role=effective_process_role):
        return
    slices.obligation_service = ExecutionObligationService(
        settings=runtime_settings,
        obligation_repo=storage.obligation_repo,
        account_snapshot_loader=lambda: slices.account_service.refresh(
            force_account_state=runtime_layering.environment_capabilities.exchange_coupled
        ),
        price_provider=slices.market_gateway.latest_price,
        fee_resolver=slices.fee_resolver,
        # Stage 6 Slice 6.5：注入跨进程 obligation 缓存。construction 顺序保证：
        # ObligationHotStateCache 在 _start_event_bus 后立即构造 + bootstrap，
        # _build_execution_slice 才跑，所以这里拿到的一定是已 hydrate 的实例。
        obligation_cache=slices.obligation_hot_state_cache,
    )
    slices.execution_outbox_publisher = None
    slices.portfolio_outbox_publisher = None
    if (
        storage.database_runtime is not None
        and isinstance(storage.obligation_repo, PostgresExecutionObligationRepository)
        and isinstance(storage.event_store, PostgresEventStore)
        and storage.outbox_repo is not None
        and hasattr(storage.execution_repo, "save_order_state_in_session")
        and hasattr(storage.execution_repo, "save_fill_in_session")
    ):
        slices.execution_outbox_publisher = PostgresExecutionOutboxPublisher(
            session_factory=storage.database_runtime.session_factory,
            event_store=storage.event_store,
            execution_repo=storage.execution_repo,
            obligation_repo=storage.obligation_repo,
            outbox_repo=storage.outbox_repo,
            bus=slices.bus,
            execution_command_repo=(
                storage.execution_command_repo
                if isinstance(storage.execution_command_repo, PostgresExecutionCommandRepository)
                else None
            ),
            execution_order_repo=storage.execution_order_repo,
            execution_order_history_repo=storage.execution_order_history_repo,
            execution_fill_repo=storage.execution_fill_repo_v2,
            # Stage 6 Slice 6.5：注入跨进程 obligation 缓存。commit hook（见 outbox
            # publisher 的 _publish_obligation_to_cache）会在事务成功后 best-effort
            # 调 cache.fire_and_forget_publish(obligation) 同步本地 dict + Redis
            # + NATS 广播；cache 未接线时为 None，行为退化为 6.5 之前。
            obligation_cache=slices.obligation_hot_state_cache,
            # P1-1 + P1-2：注入 order_state / fill 跨进程缓存。
            order_state_cache=slices.order_state_hot_cache,
            fill_event_cache=slices.fill_event_hot_cache,
        )
        slices.obligation_service.attach_obligation_writer(slices.execution_outbox_publisher)
    if storage.exit_execution_repo is not None:
        slices.exit_execution_writer = ExitExecutionWriter(storage.exit_execution_repo)
    if (
        storage.database_runtime is not None
        and isinstance(storage.event_store, PostgresEventStore)
        and storage.outbox_repo is not None
        and isinstance(storage.portfolio_repo, PostgresPortfolioRepository)
        and isinstance(storage.fill_outcome_repo, PostgresFillOutcomeRepository)
    ):
        slices.portfolio_outbox_publisher = PostgresPortfolioOutboxPublisher(
            session_factory=storage.database_runtime.session_factory,
            event_store=storage.event_store,
            outbox_repo=storage.outbox_repo,
            bus=slices.bus,
            portfolio_repo=storage.portfolio_repo,
            fill_outcome_repo=storage.fill_outcome_repo,
            # Stage 6 Slice 6.3：commit hook 注入 cache（construct 顺序保证：
            # cache 在 _start_event_bus 后立即构造，slice builders 之后才跑）
            snapshot_cache=slices.portfolio_snapshot_cache,
        )
    # P3-1 / P3-2：数据库定期清理工具——仅在有 PG session_factory 时构造。
    if storage.database_runtime is not None:
        from aats.storage.housekeeping import DatabaseHousekeeping
        slices.housekeeping = DatabaseHousekeeping(
            session_factory=storage.database_runtime.session_factory,
        )
    slices.execution_order_service = None
    slices.execution_command_processor = None
    if runtime_settings.execution_command_flow_enabled and storage.execution_command_repo is not None:
        slices.execution_order_service = ExecutionOrderService(
            execution_command_repo=storage.execution_command_repo,
            execution_order_repo=storage.execution_order_repo,
            execution_order_history_repo=storage.execution_order_history_repo,
        )
    # ── leg_risk_evaluator 解析 ────────────────────────────────────────
    # derivatives + hedge 模式下 OrderManager 需要 risk_engine.evaluate_leg_order
    # 作为下单前最后风控关卡。
    #   - monolith / None: decision slice 已构造 slices.risk_engine，直接复用。
    #   - 4 进程 execution role: decision slice 未构造（门控跳过），在此本地构造
    #     一个 RiskEngine 实例。所有依赖均来自 shared slice / storage 层，
    #     无跨进程调用。evaluate_leg_order 不累积内部 mutable state，
    #     给定相同依赖注入，两个实例行为一致。
    _leg_risk_evaluator = None
    if (
        runtime_settings.trading_product_type == "derivatives"
        and runtime_settings.derivatives_position_mode == "hedge"
    ):
        if slices.risk_engine is not None:
            # monolith 路径：直接复用 decision slice 的 risk_engine
            _leg_risk_evaluator = slices.risk_engine.evaluate_leg_order
        else:
            # 4 进程 execution 路径：本地构造 RiskEngine，存入 slices 以便
            # _bootstrap_derivatives_live_runtime_guards 后置注入
            # live_runtime_guard_provider / trial_guard_provider /
            # recovery_status_provider 三个安全信号 provider。
            slices.execution_leg_risk_engine = RiskEngine(
                settings=runtime_settings,
                account_service=slices.account_service,
                health_service=slices.health_service,
                trigger_policy=DecisionTriggerPolicy(settings=runtime_settings),
                price_provider=slices.market_gateway.latest_price,
                mode_controller=slices.mode_controller,
                obligation_repo=storage.obligation_repo,
                environment_capabilities=runtime_layering.environment_capabilities,
                policy_profile=runtime_layering.policy_profile,
                fee_resolver=slices.fee_resolver,
                reconciliation_repo=storage.reconciliation_repo,
                obligation_cache=slices.obligation_hot_state_cache,
            )
            _leg_risk_evaluator = slices.execution_leg_risk_engine.evaluate_leg_order

    slices.order_manager = OrderManager(
        settings=runtime_settings,
        bus=slices.bus,
        adapter=slices.execution_adapter,
        execution_repo=storage.execution_repo,
        exit_execution_repo=storage.exit_execution_repo,
        exit_execution_writer=slices.exit_execution_writer,
        obligation_service=slices.obligation_service,
        execution_outbox_publisher=slices.execution_outbox_publisher,
        persistent_order_service=slices.execution_order_service,
        shadow_execution_service=storage.phase1_execution_shadow_service,
        shadow_execution_order_repo=storage.execution_order_repo,
        shadow_execution_order_history_repo=storage.execution_order_history_repo,
        shadow_execution_fill_repo=storage.execution_fill_repo_v2,
        shadow_ledger_mirror_service=storage.phase1_ledger_mirror_service,
        leg_risk_evaluator=_leg_risk_evaluator,
        strategy_runtime_repo=storage.strategy_runtime_repo,
        kill_switch=slices.kill_switch,
    )
    if slices.execution_order_service is not None and storage.execution_command_repo is not None:
        _kill_switch = slices.kill_switch
        _order_manager = slices.order_manager
        slices.execution_command_processor = ExecutionCommandProcessor(
            execution_command_repo=storage.execution_command_repo,
            submit_executor=lambda intent, client_order_id=None: _order_manager.process_submit_command(
                intent=intent,
                client_order_id=client_order_id,
            ),
            cancel_executor=lambda client_order_id: _order_manager.process_cancel_command(
                client_order_id=client_order_id,
            ),
            can_execute_command=lambda command: (
                str(command.get("command_type") or "").lower() != "submit"
                or not _kill_switch.halted
            ),
            sent_retry_after_seconds=runtime_settings.execution_command_sent_retry_after_seconds,
        )


def _build_portfolio_slice(
    *,
    runtime_settings: AATSSettings,
    runtime_layering: RuntimeLayering,
    state_scope: Any,
    storage: StorageBackends,
    slices: _RuntimeSlices,
    effective_process_role: str | None,
) -> None:
    """构造 portfolio_service / sleeve_pnl_projection / funding_fee_sync。

    portfolio 在多进程拓扑里属于 execution_proc（与 order_manager 绑定，
    避免 fill → portfolio 更新跨进程往返）。

    跨 slice 依赖（Stage 3 process_role 门控时必须保证已构造）：
      - shared slice: bus、market_gateway、metrics、snapshot_builder
      - execution slice: portfolio_outbox_publisher（可为 None，仅在 Postgres
        存储 + 完整 outbox 链路时才会被 _build_execution_slice 实例化）

    因此 build_runtime 内 slice 装配顺序必须为
    shared → execution → portfolio，禁止颠倒。

    Stage 3 process_role 门控：与 execution 一起，仅 None / monolith / execution
    时构造。
    """
    if not _slice_active("portfolio", effective_process_role=effective_process_role):
        return
    portfolio_initial_usdt_balance = effective_portfolio_initial_usdt_balance(
        runtime_settings,
        exchange_coupled=runtime_layering.environment_capabilities.exchange_coupled,
    )
    slices.portfolio_state = PortfolioState(
        initial_usdt_balance=portfolio_initial_usdt_balance,
        default_product_type=runtime_settings.trading_product_type,
        default_margin_mode=runtime_settings.margin_mode,
    )
    execution_truth_repo = _storage_execution_truth_repo(storage)
    slices.sleeve_pnl_projection_service = SleevePnLProjectionService(
        fill_outcome_repo=storage.fill_outcome_repo,
        funding_fee_repo=storage.funding_fee_repo,
        sleeve_pnl_repo=storage.sleeve_pnl_repo,
        execution_repo=execution_truth_repo,
        strategy_sleeve_repo=storage.strategy_sleeve_repo,
    )
    if (
        runtime_settings.portfolio_ledger_truth_enabled
        and storage.ledger_account_repo is not None
        and storage.ledger_journal_repo is not None
        and storage.ledger_entry_repo is not None
    ):
        slices.portfolio_service = LedgerBackedPortfolioService(
            bus=slices.bus,
            state=slices.portfolio_state,
            snapshot_builder=slices.snapshot_builder,
            portfolio_repo=storage.portfolio_repo,
            fill_outcome_repo=storage.fill_outcome_repo,
            price_provider=slices.market_gateway.latest_price,
            execution_repo=execution_truth_repo,
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
            sleeve_pnl_projection_service=slices.sleeve_pnl_projection_service,
            portfolio_outbox_publisher=slices.portfolio_outbox_publisher,
            state_scope=state_scope,
            initial_usdt_balance=portfolio_initial_usdt_balance,
            metrics=slices.metrics,
        )
    else:
        slices.portfolio_service = PortfolioService(
            bus=slices.bus,
            state=slices.portfolio_state,
            snapshot_builder=slices.snapshot_builder,
            portfolio_repo=storage.portfolio_repo,
            fill_outcome_repo=storage.fill_outcome_repo,
            price_provider=slices.market_gateway.latest_price,
            execution_repo=execution_truth_repo,
            persistent_lot_book_service=(
                PersistentLotBookService(
                    position_lot_repo=storage.position_lot_repo,
                    lot_event_repo=storage.lot_event_repo,
                    projection_builder=LotBasedProjectionBuilder(),
                )
                if storage.position_lot_repo is not None and storage.lot_event_repo is not None
                else None
            ),
            sleeve_pnl_projection_service=slices.sleeve_pnl_projection_service,
            portfolio_outbox_publisher=slices.portfolio_outbox_publisher,
            state_scope=state_scope,
            metrics=slices.metrics,
        )
    slices.funding_fee_sync_service = (
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


def _build_reconciliation_slice(
    *,
    runtime_settings: AATSSettings,
    runtime_layering: RuntimeLayering,
    storage: StorageBackends,
    slices: _RuntimeSlices,
    effective_process_role: str | None,
) -> None:
    """构造 reconciliation_service 与 recovery_service。

    与 portfolio 一起属于 execution_proc — 都需要写 reconciliation_repo
    并直接联动 portfolio_state。

    跨 slice 依赖（Stage 3 process_role 门控时必须保证已构造）：
      - shared slice: bus、market_gateway、account_service、snapshot_builder、
        execution_adapter（OKXExecutionAdapter 实例提供 exchange_order_client）、
        kill_switch、metrics、bootstrap_from_exchange、reconciliation_classifier
    本 slice 依赖 execution slice 提前构造 portfolio_outbox_publisher，用于
    recovery/repair snapshot 单 writer 收敛。

    Stage 3 process_role 门控：与 execution/portfolio 一起，仅 None / monolith /
    execution 时构造。
    """
    if not _slice_active("reconciliation", effective_process_role=effective_process_role):
        return
    reconstruction_initial_usdt_balance = effective_portfolio_initial_usdt_balance(
        runtime_settings,
        exchange_coupled=runtime_layering.environment_capabilities.exchange_coupled,
    )
    reconciliation_execution_repo = _storage_execution_truth_repo(storage)
    slices.reconciliation_service = ReconciliationService(
        settings=runtime_settings,
        bus=slices.bus,
        fetcher=ExchangeStateFetcher(account_service=slices.account_service),
        comparator=StateComparator(),
        repair_service=ReconciliationRepairService(),
        reconciliation_repo=storage.reconciliation_repo,
        execution_repo=reconciliation_execution_repo,
        portfolio_repo=storage.portfolio_repo,
        event_store=storage.event_store,
        reconstruction_service=PortfolioReconstructionService(
            initial_usdt_balance=reconstruction_initial_usdt_balance,
            snapshot_builder=slices.snapshot_builder,
        ),
        price_provider=slices.market_gateway.latest_price,
        exit_execution_repo=storage.exit_execution_repo,
        bootstrap_portfolio_from_exchange=slices.bootstrap_from_exchange,
        recovery_policy=runtime_layering.recovery_policy,
        metrics=slices.metrics,
        reconciliation_classifier=slices.reconciliation_classifier,
        portfolio_outbox_publisher=slices.portfolio_outbox_publisher,
        exit_execution_writer=slices.exit_execution_writer,
    )
    slices.base_recovery_service = ExecutionRecoveryService(
        settings=runtime_settings,
        execution_repo=reconciliation_execution_repo,
        obligation_repo=storage.obligation_repo,
        portfolio_repo=storage.portfolio_repo,
        reconciliation_repo=storage.reconciliation_repo,
        strategy_runtime_repo=storage.strategy_runtime_repo,
        reconstruction_service=PortfolioReconstructionService(
            initial_usdt_balance=reconstruction_initial_usdt_balance,
            snapshot_builder=slices.snapshot_builder,
        ),
        price_provider=slices.market_gateway.latest_price,
        kill_switch=slices.kill_switch,
        bootstrap_portfolio_from_exchange=slices.bootstrap_from_exchange,
        exchange_coupled=runtime_layering.environment_capabilities.exchange_coupled,
        reconciliation_stale_after_seconds=runtime_settings.reconciliation_stale_after_seconds,
        recovery_policy=runtime_layering.recovery_policy,
        fill_outcome_repo=storage.fill_outcome_repo,
        event_store=storage.event_store,
        persistent_lot_book_service=(
            PersistentLotBookService(
                position_lot_repo=storage.position_lot_repo,
                lot_event_repo=storage.lot_event_repo,
                projection_builder=LotBasedProjectionBuilder(),
            )
            if storage.position_lot_repo is not None and storage.lot_event_repo is not None
            else None
        ),
        # Stage 6 Slice 6.5：注入 obligation cache，让 _cleanup_orphan_obligations
        # 的释放结果广播到跨进程 cache。
        obligation_cache=slices.obligation_hot_state_cache,
        obligation_writer=slices.execution_outbox_publisher,
        portfolio_outbox_publisher=slices.portfolio_outbox_publisher,
        sleeve_pnl_projection_service=slices.sleeve_pnl_projection_service,
    )
    slices.reconciliation_service.stale_reconciliation_halt_clearer = (
        slices.base_recovery_service.clear_stale_reconciliation_halt_if_resolved
    )
    # OKXExecutionAdapter.client satisfies ExchangeOrderQuerier protocol.
    _exchange_order_client = (
        getattr(slices.execution_adapter, "client", None)
        if isinstance(slices.execution_adapter, OKXExecutionAdapter)
        else None
    )
    slices.recovery_service = (
        ExecutionLedgerRecoveryService(
            settings=runtime_settings,
            base_recovery_service=slices.base_recovery_service,
            reconciliation_repo=storage.reconciliation_repo,
            portfolio_repo=storage.portfolio_repo,
            kill_switch=slices.kill_switch,
            reconciliation_classifier=slices.reconciliation_classifier or RecoveryReconciliationClassifier(),
            execution_order_repo=storage.execution_order_repo,
            execution_command_repo=storage.execution_command_repo,
            execution_outbox_publisher=slices.execution_outbox_publisher,
            exchange_order_client=_exchange_order_client,
        )
        if runtime_settings.recovery_reconciliation_execution_ledger_enabled
        else slices.base_recovery_service
    )


async def _wire_event_subscriptions(
    *,
    slices: _RuntimeSlices,
    effective_process_role: str | None = None,
) -> None:
    """把 critical 与 observer handler 注册到 bus 上。

    Stage 4 引入 NATS 之后，本函数会改成根据 process_role 选择性订阅
    各 process 自己关心的 topic（gateway 不会订阅 fill 事件等）。

    Stage 7 修复（NATS duplicate-binding）：把 critical + observer 的 subscribe
    调用都灌进 _CollectingBus，flush 时按 topic 聚合 fan-out。原因见
    _CollectingBus docstring：NATS 同 (role, topic) 只允许一个 durable binding，
    历史代码里有 critical 与 observer 同时订阅同一 critical-routed topic 的情况
    （POSITION_TARGETS / PORTFOLIO_SNAPSHOTS / RECONCILIATION_REPORTS），decision
    进程在 hybrid 模式下会因此 restart-loop。
    """
    collector = _CollectingBus(slices.bus)
    await _subscribe_critical_handlers(
        bus=collector,
        feature_engine=slices.feature_engine,
        decision_trigger=slices.decision_trigger,
        order_manager=slices.order_manager,
        portfolio_service=slices.portfolio_service,
        reconciliation_service=slices.reconciliation_service,
        audit_service=slices.audit_service,
        position_target_handler=slices.position_target_handler,
        market_gateway=slices.market_gateway,
    )
    await _subscribe_observer_handlers(
        bus=collector,
        specs=_observer_subscription_specs(
            audit_service=slices.audit_service,
            ai_service=slices.ai_service,
            reconciliation_service=slices.reconciliation_service,
        ),
    )
    # StreamSnapshotCache 远端订阅：让非 producer role（gateway / decision /
    # execution）也能通过 NATS 持续收到 MARKET_SNAPSHOTS / FEATURE_SNAPSHOTS，
    # bus receive 路径会自动调 cache.update() 保持缓存新鲜。producer role
    # （market / monolith）已经通过 feature_engine / decision_trigger 建立了
    # 对这些 topic 的订阅，这里的 noop handler 会被 _CollectingBus fan-out
    # 到同一个 durable consumer。
    if slices.stream_snapshot_cache is not None:
        await slices.stream_snapshot_cache.register_remote_subscription(collector)
    # Stage 6 Slice 6.3：把 portfolio_snapshot 缓存的 _handle_remote_event 接进
    # 同一个 collector，让 audit / reconciliation / cache 三者共享 fan-out。
    # 不走 cache.bootstrap 内部的 subscribe，避免重复 durable binding。
    if slices.portfolio_snapshot_cache is not None:
        await slices.portfolio_snapshot_cache.register_remote_subscription(collector)
    # Stage 6 Slice 6.5：同 6.3 处理，把 obligation 缓存的 _handle_remote_event
    # 也接进 collector。execution.obligation_updates 是 slice 6.5 新增 topic，
    # 初始只被 cache 自己订阅，但为了与 6.3 模板一致 + 避免未来其它 service 也
    # 订阅时踩 durable binding 冲突，这里仍然统一走 collector 聚合。设计文档：
    # docs/task/stage_6_slice_6_5_obligation_hot_state_design.md §10
    if slices.obligation_hot_state_cache is not None:
        await slices.obligation_hot_state_cache.register_remote_subscription(collector)
    # 跨进程 account snapshot 缓存：把 cache 的 _handle_remote_event 接进
    # collector，让 account.snapshots 订阅通过 _CollectingBus fan-out 聚合。
    # 非 execution 角色通过此订阅接收 execution role 广播的 account snapshot，
    # handler 内部会 idempotent 更新 cache._latest 并回调 account_service._latest_snapshot。
    if slices.account_snapshot_cache is not None:
        await slices.account_snapshot_cache.register_remote_subscription(collector)
    # P1-1：OrderState 跨进程缓存。execution.order_updates 已由 outbox publisher
    # 广播（flush_pending），order_state_cache 通过此订阅保持各进程缓存新鲜。
    if slices.order_state_hot_cache is not None:
        await slices.order_state_hot_cache.register_remote_subscription(collector)
    # P1-2：FillEvent 跨进程缓存。execution.fill_events 已由 outbox publisher
    # 广播，fill_event_cache 通过此订阅保持各进程缓存新鲜。
    if slices.fill_event_hot_cache is not None:
        await slices.fill_event_hot_cache.register_remote_subscription(collector)
    # ── Gateway event store relay ────────────────────────────────────────
    #
    # 4 进程架构下，gateway 角色只装 shared slice，不运行 decision / execution
    # 引擎。但 gateway 的 OperatorQueryService 需要通过 event_store 向 dashboard
    # 展示跨进程产生的事件（如决策上下文、风险决策、AI 报告等）。
    #
    # 问题：NatsEventBus 的 event_store.append 同时发生在 publish（生产端）和
    # receive（消费端）路径。但 receive 路径只在有 NATS subscription 时触发。
    # Gateway 对这些 topic 没有业务 handler → 没有 NATS subscription → receive
    # 路径不触发 → event_store 为空 → dashboard 查询全部返回 []。
    #
    # 修复：为 gateway 添加一个 no-op relay handler，sole purpose 是让 NATS
    # durable consumer 被创建。实际 event_store 持久化由 NatsEventBus._on_msg
    # 内置的 event_store.append 完成（本次同步添加）。
    #
    # 此列表覆盖 OperatorQueryService / RuntimeQueries 读取 event_store 的
    # 全部跨进程 topic。新增 dashboard 查询需要的 topic 时，同步更新此列表。
    if effective_process_role == PROCESS_ROLE_GATEWAY:
        from aats.events import topics as _relay_topics

        _GATEWAY_DASHBOARD_RELAY_TOPICS: tuple[str, ...] = (
            # 决策路径事件：dashboard "最近决策" / "决策详情" 面板
            _relay_topics.DECISION_CONTEXTS,
            _relay_topics.BASELINE_ASSESSMENTS,
            _relay_topics.AI_ASSESSMENTS,
            _relay_topics.AI_DECISION_BRIEFS,
            _relay_topics.AI_SHADOW_DECISIONS,
            _relay_topics.AI_SHADOW_EVALUATIONS,
            _relay_topics.AI_DEGRADATION_EVENTS,
            _relay_topics.DECISION_OUTCOMES,
            _relay_topics.EXECUTION_PLANS,
            _relay_topics.ORDER_INTENTS,
            # 风险/策略治理事件：dashboard "风控日志" 面板
            _relay_topics.RISK_DECISIONS,
            _relay_topics.POLICY_DECISIONS,
            # 策略快照：dashboard "策略状态" 面板
            _relay_topics.STRATEGY_COORDINATOR_SNAPSHOTS,
            _relay_topics.POSITION_TARGETS,
            # AI 报告：dashboard "AI 表现" / "优化报告" 面板
            _relay_topics.AI_PERFORMANCE_REPORTS,
            _relay_topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,
            # 策略 profile 管理事件：dashboard "策略 profile" 面板
            _relay_topics.STRATEGY_PROFILE_RECOMMENDATIONS,
            _relay_topics.STRATEGY_PROFILE_ACTIVATIONS,
            _relay_topics.STRATEGY_PROFILE_REJECTIONS,
            _relay_topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
        )

        async def _gateway_event_store_noop(message: dict) -> None:
            """No-op relay handler: event_store 持久化由 NatsEventBus._on_msg 处理。"""
            pass

        for _relay_t in _GATEWAY_DASHBOARD_RELAY_TOPICS:
            await collector.subscribe(_relay_t, _gateway_event_store_noop)
        log_event(
            get_logger("aats.bootstrap"),
            "gateway_event_store_relay_registered",
            topic_count=len(_GATEWAY_DASHBOARD_RELAY_TOPICS),
        )
    await collector.flush()


def _collect_drift_inputs_for_abort_hook(runtime: "ApplicationRuntime") -> DriftInputs:
    """给 Stage 9 AbortHookService 用的 inputs 收集器（best-effort）。

    checklist-4 scope：能从已有 service 摘到的指标尽量摘；拿不到的留 None，
    drift_score 的归一化会自动把 missing 视为 0 + 标记 missing。

    目前摘取的字段
    ------------
    - ``fee_to_pnl_ratio`` ← trial_guard_service.snapshot()["fee_to_notional_ratio"]
    - ``adverse_slippage_ratio`` ← trial_guard_service.snapshot()["high_slippage_ratio"]
    - ``balance_drift_ratio`` / ``max_drawdown_ratio`` ← 暂缺（留给 checklist-5
      从 ledger_portfolio.snapshot() 收集）
    - ``fill_success_ratio`` ← 暂缺
    - ``decision_*`` / ``data link *`` ← 暂缺

    为什么不直接 raise 而是 fail-soft
    ----------------------------------
    I1 (fail-soft) 要求 provider 抛异常也不能让 abort_hook loop 挂掉。这里
    额外兜一层 try/except 是为了让单次取数失败（比如 trial_guard snapshot 格式变了）
    不污染上层 evaluate_once。
    """
    import decimal
    from datetime import datetime, timezone

    notes: list[str] = [
        "abort_hook_inputs_collector: checklist-4 stub；fee + slippage 已接，"
        "其余指标 checklist-5 会从 ledger/health/quality_monitor 继续补",
    ]

    fee_to_pnl_ratio: decimal.Decimal | None = None
    adverse_slippage_ratio: decimal.Decimal | None = None

    trial_guard = runtime.trial_guard_service
    if trial_guard is not None:
        try:
            snap = trial_guard.snapshot() or {}
            raw_fee = snap.get("fee_to_notional_ratio")
            if raw_fee is not None:
                try:
                    fee_to_pnl_ratio = decimal.Decimal(str(raw_fee))
                except (decimal.InvalidOperation, ValueError):
                    notes.append(
                        f"abort_hook: fee_to_notional_ratio parse failed (raw={raw_fee!r})"
                    )
            raw_slip = snap.get("high_slippage_ratio")
            if raw_slip is not None:
                try:
                    adverse_slippage_ratio = decimal.Decimal(str(raw_slip))
                except (decimal.InvalidOperation, ValueError):
                    notes.append(
                        f"abort_hook: high_slippage_ratio parse failed (raw={raw_slip!r})"
                    )
        except Exception as exc:  # pragma: no cover - defensive
            notes.append(
                f"abort_hook: trial_guard snapshot read failed error={type(exc).__name__}"
            )

    stage_value = getattr(runtime.settings, "stage9_current_stage", "T0") or "T0"
    return DriftInputs(
        stage=stage_value,  # type: ignore[arg-type]
        window_hours=24,
        evaluated_at=datetime.now(timezone.utc),
        balance_drift_ratio=None,
        max_drawdown_ratio=None,
        fee_to_pnl_ratio=fee_to_pnl_ratio,
        fill_success_ratio=None,
        adverse_slippage_ratio=adverse_slippage_ratio,
        decision_cycle_cadence_ratio=None,
        decision_error_ratio=None,
        reconciliation_mismatch_count=None,
        nats_handler_error_ratio=None,
        okx_rate_limit_count=None,
        notes=notes,
    )


def _apply_post_init_guards(
    *,
    runtime: ApplicationRuntime,
    effective_process_role: str | None,
) -> None:
    """ApplicationRuntime 构造完成后的最终装配步骤。

    - derivatives_live_guard / trial_guard 创建并注入 risk_engine
    - abort_hook_service 创建（Stage 9 checklist-4）
    - StrategyProfileControlService 注入 decision_engine

    Stage 3：trial_guard.evaluate_now() 内部会通过 OperatorQueryService 访问
    runtime.decision_engine 等字段，gateway/market 等 role 下这些字段为 None
    会触发 AttributeError。derivatives_live_guard 需要 health_service 与
    risk_engine 都同时存在；这些 guard 的语义本身就是"执行链上的安全网"，
    对非 execution/monolith role 没有意义，因此整段直接跳过。
    """
    if not _slice_active("startup_recovery", effective_process_role=effective_process_role):
        return
    # 局部 import 是为了打破循环依赖：OperatorQueryService 与
    # RecoveryPostureEvaluator 内部都依赖 ApplicationRuntime 的字段，把它们
    # 提到模块顶部会让 aats.bootstrap.config ↔ aats.services.* 形成 import 环。
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
    # 为 decision slice 的 risk_engine 和/或 execution slice 的本地 risk_engine
    # 注入实盘安全信号 provider。monolith 下两者是同一个实例（risk_engine）；
    # 4 进程下 risk_engine 可能为 None（decision role 不在本进程），
    # 但 execution_leg_risk_engine 可能非 None（execution role 本地构造）。
    for _re in (runtime.risk_engine, getattr(runtime, "execution_leg_risk_engine", None)):
        if _re is not None:
            _re.live_runtime_guard_provider = runtime.derivatives_live_guard_service
    runtime.trial_guard_service = ForwardTrialGuardService(
        settings=runtime.settings,
        kill_switch=runtime.kill_switch,
        event_store=runtime.event_store,
        metrics=runtime.metrics,
        profitability_provider=lambda limit: OperatorQueryService(runtime).profitability_overview(limit=limit),
        anomaly_provider=lambda limit: OperatorQueryService(runtime).execution_anomaly_report(limit=limit),
    )
    runtime.trial_guard_service.evaluate_now()
    for _re in (runtime.risk_engine, getattr(runtime, "execution_leg_risk_engine", None)):
        if _re is not None:
            _re.trial_guard_provider = runtime.trial_guard_service
            _re.recovery_status_provider = lambda: RecoveryPostureEvaluator(runtime).finalize_status(
                base_status=runtime.recovery_status
            )
    # strategy_profile_service 注入已移出本函数,
    # 见 _attach_strategy_profile_service(),由 decision slice 拥有者装配。

    # Stage 9 checklist-4：AbortHookService sidecar。
    # 与 trial_guard 一样只在 decision+execution+monolith role 下实例化（都走
    # _slice_active("startup_recovery") 门禁）。设计文档：
    # docs/task/stage_9_abort_hooks_design.md §5。
    abort_hook_cfg = AbortHookConfig.from_settings(runtime.settings)
    runtime.abort_hook_service = AbortHookService(
        config=abort_hook_cfg,
        kill_switch=runtime.kill_switch,
        inputs_provider=lambda: _collect_drift_inputs_for_abort_hook(runtime),
        logger=runtime.logger,
    )


async def _build_and_connect_hot_state_store(
    *,
    runtime_settings: AATSSettings,
) -> HotStateStore:
    """Stage 6 Slice 6.1：根据 settings 构造 HotStateStore 并 fail-fast 连接。

    返回的 store 在 backend=redis 模式下已经 ping 通；调用方拿到的实例
    可以直接 await store.get/set，不需要再额外 connect。

    设计要点：
    * memory backend：纯进程内 dict，不需要 connect，立刻可用。
    * redis backend：在 build_runtime 内同步 await store.connect()。失败
      抛 RuntimeError，让 4 进程 entry 在启动期就崩，而不是延迟到第一次
      读写时才发现 Redis 不可用——避免 gateway/decision/execution 在
      "看上去 healthy 但状态不一致" 的状态下跑业务流。
    * connect() 失败时不会让 store 半初始化：异常会从 build_runtime 抛出，
      调用方的 try/except 会清理 storage.database_runtime。
    """
    if runtime_settings.hot_state_backend == "redis":
        redis_config = RedisHotStateConfig(
            url=runtime_settings.hot_state_redis_url,
            global_prefix=runtime_settings.hot_state_global_prefix,
        )
        store = build_hot_state_store(backend="redis", redis_config=redis_config)
        await store.connect()  # type: ignore[attr-defined]
        return store
    return build_hot_state_store(backend="memory")


def _resolve_effective_process_role(
    *,
    kwarg_role: str | None,
    settings: AATSSettings,
) -> str | None:
    """决定本次 build_runtime 实际启用的 process_role。

    解析优先级：
      1) 显式传入的 kwarg_role（脚本/测试可强制指定，绕过环境变量）
      2) settings.process_role（来自 AATS_PROCESS_ROLE 环境变量或 yaml）
      3) None = monolith 模式（向后兼容）

    settings.process_role 已被 AATSSettings.normalize_process_role validator
    归一化为 None 或 ALLOWED_PROCESS_ROLES 中的小写 token，本函数不需要再处理
    空白/大小写。kwarg_role 由调用方负责，通常来自代码常量或测试 fixture。
    """
    if kwarg_role is not None:
        return kwarg_role
    return settings.process_role


async def build_runtime(
    settings: AATSSettings | None = None,
    *,
    bootstrap_portfolio_snapshot: bool = True,
    process_role: str | None = None,
) -> ApplicationRuntime:
    base_settings = settings or load_settings()
    base_runtime_layering = resolve_runtime_layering(base_settings)
    _validate_runtime_settings(base_settings, base_runtime_layering)
    effective_process_role = _resolve_effective_process_role(
        kwarg_role=process_role,
        settings=base_settings,
    )
    # Stage 8：OpenTelemetry 初始化。必须在 settings_provenance / storage / bus
    # 之前完成，这样后续所有 log_event / span 都能关联到正确的
    # service.name = aats-<process_role>。
    # fail-soft：OTel 相关异常永远不阻断主系统启动——未装 opentelemetry 包时
    # configure_telemetry 自身会 fallback 到 _NoopTracer 并返回 False；这里的
    # try/except 再兜一层保险，避免 OTLP endpoint 不通之类的异常传播出去。
    # 设计文档：docs/task/stage_8_otel_integration_design.md §D2
    try:
        _telemetry_cfg = TelemetryConfig.from_env(process_role=effective_process_role)
        configure_telemetry(_telemetry_cfg)
    except Exception as _telemetry_exc:  # pragma: no cover - 防御性兜底
        log_event(
            get_logger("aats.bootstrap"),
            "telemetry_bootstrap_failed",
            level="warning",
            error_type=type(_telemetry_exc).__name__,
            error=str(_telemetry_exc),
        )
    storage = build_storage_backends(base_settings, process_role=effective_process_role)
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
        _validate_topology_capability(
            runtime_settings,
            effective_process_role=effective_process_role,
        )
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
        # Stage 6 Slice 6.1：构造 HotStateStore。memory backend 立刻可用；
        # redis backend 在此处 ping 通 Redis，失败则抛 RuntimeError 走下面
        # 的 except 清理路径。
        hot_state_store = await _build_and_connect_hot_state_store(
            runtime_settings=runtime_settings,
        )
        log_event(
            get_logger("aats.bootstrap"),
            "hot_state_store_initialized",
            backend=runtime_settings.hot_state_backend,
            global_prefix=runtime_settings.hot_state_global_prefix,
        )
    except Exception:
        if storage.database_runtime is not None:
            storage.database_runtime.dispose()
        raise
    # ── Slice 化构造（Stage 2 / Stage 3）────────────────────────────
    # build_runtime 内部按 6 个 slice builder + wire + post_init_guards 顺序装配
    # runtime；Stage 3 已在每个 builder 顶部按 process_role 门控以支持 4 进程拓扑。
    # effective_process_role 在最早期已经从 settings/kwarg 解析出来。
    slices = _RuntimeSlices()
    _build_shared_runtime_slice(
        runtime_settings=runtime_settings,
        runtime_layering=runtime_layering,
        storage=storage,
        slices=slices,
        effective_process_role=effective_process_role,
    )
    # Stage 4: bus 生命周期启动必须在任何 subscriber 注册（_build_*_slice
    # 内部 subscribe / _wire_event_subscriptions）之前完成；否则 NatsEventBus
    # 的 .subscribe() 会因为 _js 未初始化而抛 RuntimeError。
    if slices.bus is not None:
        await _start_event_bus(slices.bus)

    # Stage 6 Slice 6.4：把合并后的 KillSwitch 升级为跨进程 sidecar 模式。
    # 必须在 _start_event_bus 完成后立即调用 bootstrap，因为：
    # 1) bootstrap 内部要 await bus.subscribe(...)，bus 必须已经 connect
    # 2) 其他 slice builder 与 _apply_post_init_guards 都直接持有 slices.kill_switch
    #    引用，bootstrap 是 in-place 升级，引用不变，无需再额外注入
    # 设计文档：docs/task/stage_6_slice_6_4_kill_switch_unification_design.md §3-§5
    await slices.kill_switch.bootstrap(
        hot_state_store=hot_state_store,
        bus=slices.bus,
        process_role=effective_process_role or "monolith",
        logger=get_logger("aats.governance.kill_switch"),
        fail_closed_on_authority_loss=(
            slices.mode_controller.environment_capabilities.exchange_submission_enabled
        ),
    )
    log_event(
        get_logger("aats.bootstrap"),
        "kill_switch_initialized",
        process_role=effective_process_role or "monolith",
        bootstrap_state=slices.kill_switch.snapshot(),
    )

    # StreamSnapshotCache bootstrap：高频 MARKET_SNAPSHOTS / FEATURE_SNAPSHOTS
    # 不落 Postgres，NATS durable consumer 重启后只从上次 ack 续收。从 Redis
    # 恢复 latest + recent 条目，消除重启空窗。bootstrap 内部 best-effort 不抛。
    # 必须用 expanded_allowed_symbols 覆盖 swap / hedge / arbitrage 派生的
    # symbol，否则这些扩展标的重启后不会恢复快照。
    if slices.stream_snapshot_cache is not None:
        await slices.stream_snapshot_cache.bootstrap(
            hot_state_store=hot_state_store,
            symbols=list(runtime_settings.expanded_allowed_symbols()),
            logger=get_logger("aats.stream_snapshot_cache"),
        )

    # Stage 6 Slice 6.3：跨进程 portfolio_snapshot 缓存边车。和 kill_switch
    # bootstrap 同模板：bus.start 完成后立即构造 + Redis hydrate，让所有 slice builder（特
    # 别是 _build_portfolio_slice 的 outbox publisher 与 query_service 路径）能
    # 拿到一个已 hydrate 的 cache 实例。
    #
    # ⚠️ NATS subscribe 步骤推迟到 _wire_event_subscriptions 经 _CollectingBus
    # 聚合：portfolio.snapshots 在 production 路径上已经被 audit_service /
    # reconciliation_service 订阅，NATS 同 (role, topic) 只允许一个 durable
    # binding，cache 的订阅必须和它们共用 _CollectingBus 的 fan-out 否则会触发
    # "consumer is already bound to a subscription"。Stage 7 修复 _CollectingBus
    # 时已经踩过同类问题（POSITION_TARGETS / PORTFOLIO_SNAPSHOTS / RECONCILIATION_REPORTS）。
    # bootstrap 内部即使 Redis 故障也走 best-effort 路径不抛。
    # 设计文档：docs/task/stage_6_slice_6_3_portfolio_snapshot_design.md §4
    slices.portfolio_snapshot_cache = PortfolioSnapshotCache(
        hot_state_store=hot_state_store,
        bus=slices.bus,
        process_role=effective_process_role or "monolith",
        logger=get_logger("aats.portfolio.snapshot_cache"),
    )
    await slices.portfolio_snapshot_cache.bootstrap(
        scope_fingerprint=f"{state_scope.product_type}:{state_scope.margin_mode}",
        subscribe=False,  # 推迟到 _wire_event_subscriptions 走 _CollectingBus
    )
    log_event(
        get_logger("aats.bootstrap"),
        "portfolio_snapshot_cache_initialized",
        process_role=effective_process_role or "monolith",
        bootstrap_state=slices.portfolio_snapshot_cache.snapshot(),
    )

    # 2026-04-27 single-writer convergence：production portfolio snapshot writes
    # must go through PostgresPortfolioOutboxPublisher. The repository listener
    # remains available for low-level unit tests, but build_runtime no longer
    # wires it as a production cache publication path.

    # Stage 6 Slice 6.5：跨进程 obligation 缓存边车。和 6.3 PortfolioSnapshotCache
    # 同模板：bus.start 完成后立即构造 + Redis hydrate，让所有 slice builder
    # （decision 的 risk_engine、execution 的 obligation_service、gateway 的
    # query_service）能在构造时拿到一个已 hydrate 的 cache 实例。
    #
    # ⚠️ NATS subscribe 步骤推迟到 _wire_event_subscriptions 经 _CollectingBus
    # 聚合：execution.obligation_updates 是 slice 6.5 新增 topic，初始只被 cache
    # 自己订阅，理论上不会撞 durable binding，但为了与 6.3 模板一致 + 避免未来
    # 其它 service 也订阅本 topic 时踩坑，这里仍然 defer subscribe。
    # bootstrap 内部即使 Redis 故障也走 best-effort 路径不抛（I1 fail-soft）。
    # 设计文档：docs/task/stage_6_slice_6_5_obligation_hot_state_design.md §10
    slices.obligation_hot_state_cache = ObligationHotStateCache(
        logger=get_logger("aats.execution.obligation_cache"),
    )
    await slices.obligation_hot_state_cache.bootstrap(
        hot_state_store=hot_state_store,
        bus=slices.bus,
        process_role=effective_process_role or "monolith",
        subscribe=False,  # 推迟到 _wire_event_subscriptions 走 _CollectingBus
    )
    log_event(
        get_logger("aats.bootstrap"),
        "obligation_hot_state_cache_initialized",
        process_role=effective_process_role or "monolith",
        bootstrap_state=slices.obligation_hot_state_cache.snapshot(),
    )
    # Stage 6 Slice 6.5：Phase1ShadowMonitor 在 _build_shared_runtime_slice 早
    # 期构造，那时 obligation_hot_state_cache 还不存在；这里用 setter 注入把
    # cache 交给 monitor。dashboard snapshot() 读路径就会优先用 cache。
    if slices.phase1_shadow_monitor is not None:
        slices.phase1_shadow_monitor.attach_obligation_cache(
            slices.obligation_hot_state_cache
        )

    # P1-1 热路径优化：OrderState 跨进程缓存边车。
    from aats.services.execution_engine.order_state_cache import OrderStateHotCache
    slices.order_state_hot_cache = OrderStateHotCache(
        logger=get_logger("aats.execution.order_state_cache"),
    )
    await slices.order_state_hot_cache.bootstrap(
        hot_state_store=hot_state_store,
        bus=slices.bus,
        process_role=effective_process_role or "monolith",
        truth_loader=_storage_execution_truth_repo(storage).get_order_state,
        subscribe=False,
    )
    log_event(
        get_logger("aats.bootstrap"),
        "order_state_hot_cache_initialized",
        process_role=effective_process_role or "monolith",
        bootstrap_state=slices.order_state_hot_cache.snapshot(),
    )

    # P1-2 热路径优化：FillEvent 跨进程缓存边车。
    from aats.services.execution_engine.fill_event_cache import FillEventHotCache
    slices.fill_event_hot_cache = FillEventHotCache(
        logger=get_logger("aats.execution.fill_event_cache"),
    )
    fill_event_truth_repo = _storage_execution_truth_repo(storage)
    await slices.fill_event_hot_cache.bootstrap(
        hot_state_store=hot_state_store,
        bus=slices.bus,
        process_role=effective_process_role or "monolith",
        truth_loader=lambda limit: fill_event_truth_repo.fills_for_scope(
            scope=state_scope,
            limit=limit,
        ),
        subscribe=False,
    )
    log_event(
        get_logger("aats.bootstrap"),
        "fill_event_hot_cache_initialized",
        process_role=effective_process_role or "monolith",
        bootstrap_state=slices.fill_event_hot_cache.snapshot(),
    )

    # 跨进程 account snapshot 缓存边车。和 6.3 PortfolioSnapshotCache / 6.5
    # ObligationHotStateCache 同 sidecar 模板：bus.start 完成后立即构造 + Redis
    # hydrate，让 health_service / query_service / dashboard 在非 execution 角色
    # 下也能读到由 execution role 广播的最新 account snapshot。
    #
    # ⚠️ NATS subscribe 步骤推迟到 _wire_event_subscriptions 经 _CollectingBus
    # 聚合，避免 NATS durable binding 冲突。
    # bootstrap 内部即使 Redis 故障也走 best-effort 路径不抛（I1 fail-soft）。
    slices.account_snapshot_cache = AccountSnapshotCache(
        logger=get_logger("aats.execution.account_snapshot_cache"),
        redis_ttl_seconds=max(1800, int(base_settings.account_state_stale_after_seconds * 3)),
    )
    await slices.account_snapshot_cache.bootstrap(
        hot_state_store=hot_state_store,
        bus=slices.bus,
        process_role=effective_process_role or "monolith",
        subscribe=False,  # 推迟到 _wire_event_subscriptions 走 _CollectingBus
    )
    log_event(
        get_logger("aats.bootstrap"),
        "account_snapshot_cache_initialized",
        process_role=effective_process_role or "monolith",
        bootstrap_state=slices.account_snapshot_cache.snapshot(),
    )
    # 非 execution 角色：把 cache 的状态更新回调注册到 account_service，
    # 让 cache 收到远端 NATS 事件后自动写回 account_service._latest_snapshot
    # 和 account_service._latest_recent_bills，保证 dashboard 的
    # recent_funding_fee_summary / recent_bills_summary 也能跨进程同步。
    # execution 角色本身由 _refresh_account_loop 驱动写入，但注册 listener
    # 也无害（idempotent 规则会 noop 掉 self-loop）。
    if slices.account_service is not None:
        _acct_svc = slices.account_service

        def _sync_to_account_service(
            snap: ExchangeAccountSnapshot,
            recent_bills: list[dict],
        ) -> None:
            # 只在 account_service._latest_snapshot 为 None 或比远端旧时更新
            existing = _acct_svc._latest_snapshot
            if existing is not None and snap.fetched_at <= existing.fetched_at:
                return
            _acct_svc._latest_snapshot = snap
            _acct_svc._latest_recent_bills = [
                dict(row) for row in recent_bills if isinstance(row, dict)
            ]

        slices.account_snapshot_cache.set_on_state_updated(_sync_to_account_service)
        # bootstrap hydrate 之后立即同步一次
        bootstrapped_snap = slices.account_snapshot_cache.get_sync()
        if bootstrapped_snap is not None:
            _sync_to_account_service(
                bootstrapped_snap,
                slices.account_snapshot_cache.recent_bills,
            )
            log_event(
                get_logger("aats.bootstrap"),
                "account_service_hydrated_from_cache",
                process_role=effective_process_role or "monolith",
                fetched_at=bootstrapped_snap.fetched_at.isoformat(),
                recent_bills_count=len(slices.account_snapshot_cache.recent_bills),
            )

    _build_market_slice(
        slices=slices,
        effective_process_role=effective_process_role,
        runtime_settings=runtime_settings,
    )
    _build_decision_slice(
        runtime_settings=runtime_settings,
        storage=storage,
        runtime_layering=runtime_layering,
        slices=slices,
        effective_process_role=effective_process_role,
    )
    _build_execution_slice(
        runtime_settings=runtime_settings,
        storage=storage,
        runtime_layering=runtime_layering,
        slices=slices,
        effective_process_role=effective_process_role,
    )
    _build_portfolio_slice(
        runtime_settings=runtime_settings,
        runtime_layering=runtime_layering,
        state_scope=state_scope,
        storage=storage,
        slices=slices,
        effective_process_role=effective_process_role,
    )
    _build_reconciliation_slice(
        runtime_settings=runtime_settings,
        runtime_layering=runtime_layering,
        storage=storage,
        slices=slices,
        effective_process_role=effective_process_role,
    )
    await _wire_event_subscriptions(slices=slices, effective_process_role=effective_process_role)

    # P2-1：启动审计批量写任务。订阅已全部装配完毕，开始积攒写缓冲。
    if slices.audit_service is not None:
        await slices.audit_service.start_batch_writer()

    # ── Bootstrap recovery / startup snapshot 装配 ──────────────────
    # 这部分是 slice 装配完成后的“启动序列编排”，依赖多个 slice 的产物，
    # 因此保留在 build_runtime 内作为 orchestration glue。
    bus = slices.bus
    market_gateway = slices.market_gateway
    account_service = slices.account_service
    fee_resolver = slices.fee_resolver
    feature_engine = slices.feature_engine
    ai_service = slices.ai_service
    strategy_coordinator = slices.strategy_coordinator
    decision_engine = slices.decision_engine
    decision_trigger = slices.decision_trigger
    decision_trigger_policy = slices.decision_trigger_policy
    execution_planner = slices.execution_planner
    execution_adapter = slices.execution_adapter
    order_manager = slices.order_manager
    portfolio_service = slices.portfolio_service
    reconciliation_service = slices.reconciliation_service
    policy_engine = slices.policy_engine
    risk_engine = slices.risk_engine
    kill_switch = slices.kill_switch
    mode_controller = slices.mode_controller
    health_service = slices.health_service
    metrics = slices.metrics
    bootstrap_from_exchange = slices.bootstrap_from_exchange
    baseline_import_service = slices.baseline_import_service
    funding_fee_sync_service = slices.funding_fee_sync_service
    sleeve_pnl_projection_service = slices.sleeve_pnl_projection_service
    recovery_service = slices.recovery_service
    phase1_shadow_monitor = slices.phase1_shadow_monitor
    phase1_shadow = slices.phase1_shadow
    execution_outbox_publisher = slices.execution_outbox_publisher
    execution_order_service = slices.execution_order_service
    execution_command_processor = slices.execution_command_processor
    # Stage 6 Slice 6.4：合并的 KillSwitch 已经在 bus.start 后被 bootstrap 升级为
    # sidecar 模式，slices.kill_switch 引用即是。
    # Stage 6 Slice 6.3：portfolio_snapshot 跨进程缓存（同样已在 bus.start 后立
    # 即构造 + bootstrap），稍后注入到 outbox publisher 与 query_service。
    portfolio_snapshot_cache = slices.portfolio_snapshot_cache
    # Stage 6 Slice 6.5：obligation 跨进程缓存（同样已在 bus.start 后立即构造 +
    # bootstrap），稍后注入到 obligation_service 写路径与 risk_engine /
    # query_service 读路径。
    obligation_hot_state_cache = slices.obligation_hot_state_cache
    account_snapshot_cache = slices.account_snapshot_cache

    # Stage 3：startup recovery 段只在 execution / monolith role 下运行。
    # gateway / market / decision role 没有 portfolio_service 与 recovery_service，
    # 整个恢复流程跳过；recovery_status 用极简默认值占位（仅 status 字段必填）。
    if _slice_active("startup_recovery", effective_process_role=effective_process_role):
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
                execution_repo=_storage_execution_truth_repo(storage),
                exit_execution_repo=storage.exit_execution_repo,
                scope=state_scope,
                exit_execution_writer=slices.exit_execution_writer,
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
    else:
        # gateway / market / decision role：跳过启动恢复，给个最小占位 RecoveryStatus。
        # 这些 role 不持有 portfolio/recovery slice，恢复语义不适用。
        recovery_status = RecoveryStatus(
            status="multi_process_role_skip",
            recovery_state="multi_process_role_skip",
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
        stream_snapshot_cache=slices.stream_snapshot_cache,
        hot_state_store=hot_state_store,
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
        portfolio_snapshot_cache=portfolio_snapshot_cache,
        obligation_hot_state_cache=obligation_hot_state_cache,
        account_snapshot_cache=account_snapshot_cache,
        mode_controller=mode_controller,
        health_service=health_service,
        account_service=account_service,
        metrics=metrics,
        audit_repo=storage.audit_repo,
        portfolio_repo=storage.portfolio_repo,
        fill_outcome_repo=storage.fill_outcome_repo,
        sleeve_pnl_repo=storage.sleeve_pnl_repo,
        execution_repo=storage.execution_repo,
        execution_truth_repo=_storage_execution_truth_repo(storage),
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
        audit_service=slices.audit_service,
        housekeeping=slices.housekeeping,
        execution_leg_risk_engine=slices.execution_leg_risk_engine,
        process_role=effective_process_role,
        long_short_poller=slices.long_short_poller,
    )
    if runtime.sleeve_pnl_projection_service is not None:
        runtime.sleeve_pnl_projection_service.rebuild_scope(scope=state_scope)

    # Slice 4-proc operator command proxy：gateway 进程的 /system/rebaseline
    # 与 /system/resume HTTP endpoint 需要访问 portfolio_service /
    # reconciliation_service，但这两个 service 只在 execution role 装配
    # （_SLICE_REQUIRED_ROLES 门控）。本段在 gateway role 下装 client、
    # execution role 下装 worker，monolith / market / decision role 下留
    # 空字段——monolith 走本地直接调用、market/decision 根本没有 operator
    # endpoint。bootstrap() 必须在 runtime 对外暴露前完成，确保后续
    # reconciliation_system_queries.rebaseline() 的 client.invoke 前订阅已就位。
    # 设计文档：docs/task/slice_4proc_operator_command_proxy_fix_design.md §4.4/§4.5
    if effective_process_role == PROCESS_ROLE_GATEWAY:
        runtime.operator_command_client = OperatorCommandClient(
            bus=bus,
            process_role=PROCESS_ROLE_GATEWAY,
            logger=runtime.logger,
        )
        await runtime.operator_command_client.bootstrap()
        # AI command client：同进程再装一个，topic 换成 AI_COMMAND_*。
        # component_name="ai_command" 让日志 event 打成 ai_command_*
        # 前缀，可与 execution 代理独立 grep / 告警。
        runtime.ai_command_client = OperatorCommandClient(
            bus=bus,
            process_role=PROCESS_ROLE_GATEWAY,
            logger=runtime.logger,
            request_topic=topics.AI_COMMAND_REQUESTS,
            response_topic=topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await runtime.ai_command_client.bootstrap()
    elif effective_process_role == PROCESS_ROLE_EXECUTION:
        # 局部 import 是为了打破循环依赖：OperatorQueryService 间接依赖
        # ApplicationRuntime 的大量字段，模块顶层 import 会让 bootstrap.config
        # ↔ services.operator.query_service 形成 import 环。execution role 下
        # runtime.portfolio_service / reconciliation_service 等字段都非 None，
        # dispatch 时直接复用 monolith 路径的业务逻辑。
        from aats.services.operator.query_service import OperatorQueryService

        async def _handle_rebaseline(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.rebaseline(
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_halt(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.halt(
                reason=payload.get("reason", "manual_halt"),
                generation=payload.get("generation"),
                set_at_ts=payload.get("set_at_ts"),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_resume(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.resume(
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_validate_reconciliation(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.validate_reconciliation(
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_cancel_order(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.cancel_order(
                client_order_id=payload["client_order_id"],
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_resolve_stuck_submission(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.resolve_stuck_submission(
                client_order_id=payload["client_order_id"],
                reason=payload.get("reason", ""),
                operator_confirmation=payload.get("operator_confirmation"),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_refresh_exchange_state(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.refresh_exchange_state(
                blocker=payload.get("blocker"),
                parent_intent_id=payload.get("parent_intent_id"),
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_retry_limit_lookup(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.retry_limit_lookup(
                parent_intent_id=payload.get("parent_intent_id"),
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_safe_cancel_exit_execution(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return await service.safe_cancel_exit_execution(
                parent_intent_id=payload.get("parent_intent_id"),
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_reset_trial_guard(payload: dict[str, Any]) -> dict[str, Any]:
            service = OperatorQueryService(runtime)
            return service.record_trial_guard_manual_reset(
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        runtime.operator_command_worker = OperatorCommandWorker(
            bus=bus,
            process_role=PROCESS_ROLE_EXECUTION,
            logger=runtime.logger,
            command_handlers={
                "halt": _handle_halt,
                "rebaseline": _handle_rebaseline,
                "resume": _handle_resume,
                "validate_reconciliation": _handle_validate_reconciliation,
                "cancel_order": _handle_cancel_order,
                "resolve_stuck_submission": _handle_resolve_stuck_submission,
                "refresh_exchange_state": _handle_refresh_exchange_state,
                "retry_limit_lookup": _handle_retry_limit_lookup,
                "safe_cancel_exit_execution": _handle_safe_cancel_exit_execution,
                "reset_trial_guard": _handle_reset_trial_guard,
            },
        )
        await runtime.operator_command_worker.bootstrap()
    elif effective_process_role == PROCESS_ROLE_DECISION:
        # AI command worker：gateway 通过 NATS 转发的 AI mutate 请求在 decision
        # 进程落地执行。ai_service 驻 decision role，因此 dispatch 的 AI 命令
        # 回到 OperatorQueryService 本地方法即可直接调用 ai_service。
        from aats.services.operator.query_service import OperatorQueryService as _AIQueryService

        async def _handle_ai_runtime_status(_payload: dict[str, Any]) -> dict[str, Any]:
            service = _AIQueryService(runtime)
            status = service.ai_runtime()
            status["ai_runtime_source"] = "local_decision"
            return status

        async def _handle_ai_operating_mode_select(payload: dict[str, Any]) -> dict[str, Any]:
            service = _AIQueryService(runtime)
            return await service.set_ai_operating_mode(
                mode=payload["mode"],
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_ai_review_restore(payload: dict[str, Any]) -> dict[str, Any]:
            service = _AIQueryService(runtime)
            return await service.ai_review_restore(
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        async def _handle_ai_review_degrade_to_baseline(payload: dict[str, Any]) -> dict[str, Any]:
            service = _AIQueryService(runtime)
            return await service.ai_review_degrade_to_baseline(
                reason=payload.get("reason", ""),
                actor_role=payload.get("actor_role", "anonymous"),
                actor_identity=payload.get("actor_identity"),
                auth_source=payload.get("auth_source", "anonymous"),
            )

        runtime.ai_command_worker = OperatorCommandWorker(
            bus=bus,
            process_role=PROCESS_ROLE_DECISION,
            logger=runtime.logger,
            command_handlers={
                "ai_runtime_status": _handle_ai_runtime_status,
                "ai_operating_mode_select": _handle_ai_operating_mode_select,
                "ai_review_restore": _handle_ai_review_restore,
                "ai_review_degrade_to_baseline": _handle_ai_review_degrade_to_baseline,
            },
            request_topic=topics.AI_COMMAND_REQUESTS,
            response_topic=topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await runtime.ai_command_worker.bootstrap()

    # StrategyProfileControlService 只依赖 shared + decision slice 资源
    # (repo / settings / event_store),不走 startup_recovery(execution 侧)。
    # 必须在 decision role 也装配,否则 4 进程拓扑下 decision 进程的
    # orchestrator.strategy_profile_service=None,active_profile_id 永远为 null。
    if _slice_active("decision", effective_process_role=effective_process_role):
        if runtime.decision_engine is not None:
            runtime.decision_engine.strategy_profile_service = StrategyProfileControlService(runtime)

    _apply_post_init_guards(runtime=runtime, effective_process_role=effective_process_role)

    # ── Finding 3: Guard signal 跨进程缓存 ──────────────────────────
    # execution 侧：创建 3 个 GuardSignalHotStateCache，发布初始快照，
    #   启动后台任务每 10 秒重新发布（guard 评估周期约 5-15 秒）
    # decision 侧：创建 3 个 reader 缓存，从 Redis 恢复 + 订阅 NATS，
    #   注入 risk_engine 作为 provider（取代本地缺失的 guard service）
    # monolith：guard service 直接注入 risk_engine，不需要缓存层
    from aats.services.governance_engine.guard_signal_cache import (
        GuardSignalHotStateCache,
    )

    if effective_process_role == PROCESS_ROLE_EXECUTION:
        _guard_caches: dict[str, GuardSignalHotStateCache] = {}
        for _sig_name in ("derivatives_live", "trial", "recovery"):
            _cache = GuardSignalHotStateCache(
                signal_name=_sig_name,
                logger=runtime.logger,
            )
            await _cache.bootstrap(
                hot_state_store=hot_state_store,
                bus=bus,
                process_role=PROCESS_ROLE_EXECUTION,
            )
            _guard_caches[_sig_name] = _cache

        # 发布初始快照（guard service 已在 _apply_post_init_guards 中 evaluate_now）
        if runtime.derivatives_live_guard_service is not None:
            await _guard_caches["derivatives_live"].publish(
                runtime.derivatives_live_guard_service.snapshot()
            )
        if runtime.trial_guard_service is not None:
            await _guard_caches["trial"].publish(
                runtime.trial_guard_service.snapshot()
            )
        _recovery_provider = getattr(runtime, "_recovery_posture_for_guard_cache", None)
        if _recovery_provider is None:
            from aats.services.governance_engine.recovery_posture import (
                RecoveryPostureEvaluator as _RPE,
            )

            def _recovery_provider() -> dict[str, Any]:
                return _RPE(runtime).finalize_status(
                    base_status=runtime.recovery_status
                )
        # Finding 2 修复：RecoveryPostureEvaluator.finalize_status() 返回的是
        # RecoveryStatus (Pydantic BaseModel)，不是 dict。之前 isinstance(..., dict)
        # 永远 False 导致 recovery 快照从未被 publish。统一 model_dump 后再检查。
        _initial_recovery = _recovery_provider()
        if hasattr(_initial_recovery, "model_dump"):
            _initial_recovery = _initial_recovery.model_dump(mode="json")
        if isinstance(_initial_recovery, dict) and _initial_recovery:
            await _guard_caches["recovery"].publish(_initial_recovery)

        runtime.guard_signal_caches = _guard_caches

        # 后台发布任务：每 10 秒把 guard 快照刷到 Redis + NATS
        # Finding 3 修复：纳入 runtime.background_tasks 生命周期管理，
        # stop_background_tasks() 会 cancel + await 它，不再在关停后继续写
        # 已关闭的 bus/store。异常用 log_event 结构化记录，不再 silent pass。
        async def _guard_signal_publish_loop() -> None:
            # 2026-04-21 修复：原实现 publish 调用无 timeout 保护。观察到
            # NATS client 在持续 outbound buffer 溢出后进入某种未恢复态，
            # `await cache.publish()` 永久 hang 而不抛 exception → 整个 loop
            # 停在单次 publish 上、从 log 看像静默消失（每 10s 本应有活动，
            # 实际 2.8h 无任何 guard_signal 事件）。Decision 侧看到 cache
            # age=10000+s 触发 guard_signal_cache_fail_closed WARN 风暴。
            #
            # 修法：把每个 publish 包 asyncio.wait_for(..., timeout=8.0)，
            # 让单次 publish 最多等 8 秒就 TimeoutError。except 捕获后
            # log_event，下一轮 10s 后继续尝试，自动恢复；NATS client 内部
            # 重连机制接管即可。
            import asyncio as _aio

            _PUBLISH_TIMEOUT_SECONDS = 8.0

            async def _safe_publish(signal_name: str, cache, payload) -> None:
                try:
                    await _aio.wait_for(
                        cache.publish(payload),
                        timeout=_PUBLISH_TIMEOUT_SECONDS,
                    )
                except _aio.TimeoutError:
                    log_event(
                        runtime.logger,
                        "guard_signal_publish_loop_publish_timeout",
                        level="warning",
                        signal_name=signal_name,
                        timeout_seconds=_PUBLISH_TIMEOUT_SECONDS,
                    )

            while True:
                # 2026-04-21 A2: 10s base + 10% jitter 防跨进程锁步
                await _aio.sleep(runtime._jittered_sleep_seconds(10.0))
                try:
                    if runtime.derivatives_live_guard_service is not None:
                        # 先 evaluate_now 确保 snapshot 是最新评估结果，
                        # 而不是 bootstrap 时的旧快照。evaluate_now 是同步
                        # CPU-bound 调用，走 to_thread 避免阻塞事件循环。
                        await _aio.to_thread(
                            runtime.derivatives_live_guard_service.evaluate_now
                        )
                        await _safe_publish(
                            "derivatives_live",
                            _guard_caches["derivatives_live"],
                            runtime.derivatives_live_guard_service.snapshot(),
                        )
                    if runtime.trial_guard_service is not None:
                        await _aio.to_thread(
                            runtime.trial_guard_service.evaluate_now
                        )
                        await _safe_publish(
                            "trial",
                            _guard_caches["trial"],
                            runtime.trial_guard_service.snapshot(),
                        )
                    _rec = _recovery_provider()
                    if hasattr(_rec, "model_dump"):
                        _rec = _rec.model_dump(mode="json")
                    if isinstance(_rec, dict) and _rec:
                        await _safe_publish(
                            "recovery", _guard_caches["recovery"], _rec,
                        )
                except Exception as exc:
                    log_event(
                        runtime.logger,
                        "guard_signal_publish_loop_error",
                        level="warning",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )

        _publish_task = asyncio.create_task(
            _guard_signal_publish_loop(),
            name="aats_guard_signal_publish",
        )
        runtime.register_background_task(
            _publish_task,
            name="aats_guard_signal_publish",
            critical=True,
            owned_by_runtime=True,
        )
        runtime._guard_signal_publish_task = _publish_task

    elif effective_process_role == PROCESS_ROLE_DECISION and runtime.risk_engine is not None:
        # Decision 侧：从 Redis 恢复 + 订阅 NATS，注入 risk_engine 作为 provider
        #
        # 注意：三个 guard cache 共享同一个 NATS topic (GUARD_SIGNAL_UPDATES)，
        # NatsBus 为同 topic 只创建一个 durable consumer，因此不能让每个 cache
        # 各自 subscribe（第二/三个会失败 "consumer is already bound"）。
        # 统一用 subscribe=False + 手动单次订阅 + 分发器模式。
        _live_guard_cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=runtime.logger,
        )
        await _live_guard_cache.bootstrap(
            hot_state_store=hot_state_store,
            bus=bus,
            process_role=PROCESS_ROLE_DECISION,
            subscribe=False,
        )
        runtime.risk_engine.live_runtime_guard_provider = _live_guard_cache

        _trial_guard_cache = GuardSignalHotStateCache(
            signal_name="trial",
            logger=runtime.logger,
        )
        await _trial_guard_cache.bootstrap(
            hot_state_store=hot_state_store,
            bus=bus,
            process_role=PROCESS_ROLE_DECISION,
            subscribe=False,
        )
        runtime.risk_engine.trial_guard_provider = _trial_guard_cache

        _recovery_cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=runtime.logger,
        )
        await _recovery_cache.bootstrap(
            hot_state_store=hot_state_store,
            bus=bus,
            process_role=PROCESS_ROLE_DECISION,
            subscribe=False,
        )
        runtime.risk_engine.recovery_status_provider = _recovery_cache

        # 单次订阅 GUARD_SIGNAL_UPDATES，分发到所有 cache。
        # 每个 cache 的 _handle_remote_update 内部按 signal_name 过滤，
        # 只更新匹配自己的快照。
        _decision_guard_caches_by_name = {
            "derivatives_live": _live_guard_cache,
            "trial": _trial_guard_cache,
            "recovery": _recovery_cache,
        }

        async def _guard_signal_dispatch(message: dict) -> None:
            for _gc in _decision_guard_caches_by_name.values():
                await _gc._handle_remote_update(message)

        from aats.events import topics as _guard_topics

        await bus.subscribe(_guard_topics.GUARD_SIGNAL_UPDATES, _guard_signal_dispatch)
        _live_guard_cache._subscribed = True
        _trial_guard_cache._subscribed = True
        _recovery_cache._subscribed = True

        runtime.guard_signal_caches = {
            "derivatives_live": _live_guard_cache,
            "trial": _trial_guard_cache,
            "recovery": _recovery_cache,
        }

    return runtime
