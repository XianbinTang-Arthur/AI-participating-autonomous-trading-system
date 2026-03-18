from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.settings import AATSSettings
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope, parse_payload, publish_model
from aats.schemas.decision import PositionTarget
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
from aats.services.execution_engine.baseline_import import AccountBaselineImportService
from aats.services.execution_engine.obligations import ExecutionObligationService
from aats.services.execution_engine.recovery import ExecutionRecoveryService
from aats.services.execution_engine.okx_rest import OKXRESTClient
from aats.services.execution_engine.order_manager import OrderManager
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher
from aats.services.execution_engine.paper_adapter import PaperExecutionAdapter
from aats.services.execution_engine.planner import ExecutionPlanner
from aats.services.feature_engine.calculator import FeatureCalculator, FeatureEngine
from aats.services.governance_engine.health import SystemHealthService
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
from aats.services.market_gateway.gateway import MarketDataGateway
from aats.services.market_gateway.normalizer import MarketSnapshotNormalizer
from aats.services.market_gateway.okx_websocket import OKXPublicWebSocketClient
from aats.services.market_gateway.publisher import MarketSnapshotPublisher
from aats.services.operator.accounts import enabled_admin_count
from aats.services.operator.runtime_profiles import runtime_profile_resolution
from aats.services.operator.strategy_profiles import seed_strategy_profiles
from aats.services.runtime_scope import latest_matching_snapshot, runtime_state_scope, scoped_portfolio_event
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
from aats.storage.execution_repo_postgres import PostgresExecutionRepository
from aats.storage.obligation_repo import InMemoryExecutionObligationRepository
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository
from aats.storage.operator_repo import InMemoryOperatorUserRepository
from aats.storage.operator_repo_postgres import PostgresOperatorUserRepository
from aats.storage.portfolio_repo import InMemoryPortfolioRepository
from aats.storage.portfolio_repo_postgres import PostgresPortfolioRepository
from aats.storage.reconciliation_repo import InMemoryReconciliationRepository
from aats.storage.reconciliation_repo_postgres import PostgresReconciliationRepository
from aats.storage.runtime_profile_repo import InMemoryRuntimeProfileRepository
from aats.storage.runtime_profile_repo_postgres import PostgresRuntimeProfileRepository
from aats.storage.strategy_profile_repo import InMemoryStrategyProfileRepository
from aats.storage.strategy_profile_repo_postgres import PostgresStrategyProfileRepository
from aats.storage.session import DatabaseRuntime, create_database_runtime, create_schema
from aats.schemas.system import RecoveryStatus
from aats.schemas.common import utc_now
from aats.schemas.operator import ExecutionErrorSummary
from aats.schemas.runtime_profiles import RuntimeProfileResolution
from aats.storage.base import StrategyProfileRepository


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
    execution_repo: ExecutionRepository
    obligation_repo: ExecutionObligationRepository
    reconciliation_repo: ReconciliationRepository
    operator_repo: OperatorUserRepository
    runtime_profile_repo: RuntimeProfileRepository
    strategy_profile_repo: StrategyProfileRepository
    outbox_repo: PostgresOutboxRepository | None = None
    database_runtime: DatabaseRuntime | None = None


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
    policy_engine: PolicyEngine
    risk_engine: RiskEngine
    kill_switch: KillSwitch
    mode_controller: RuntimeModeController
    health_service: SystemHealthService
    account_service: OKXAccountService
    metrics: MetricsRegistry
    audit_repo: AuditRepository
    portfolio_repo: PortfolioRepository
    execution_repo: ExecutionRepository
    obligation_repo: ExecutionObligationRepository
    reconciliation_repo: ReconciliationRepository
    operator_repo: OperatorUserRepository
    runtime_profile_repo: RuntimeProfileRepository
    strategy_profile_repo: StrategyProfileRepository
    recovery_status: RecoveryStatus
    replay_validation_history: list[dict[str, Any]] = field(default_factory=list)
    database_runtime: DatabaseRuntime | None = None
    background_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    execution_outbox_publisher: PostgresExecutionOutboxPublisher | None = None
    logger: Any = field(default_factory=lambda: get_logger("aats.runtime"))

    async def start_background_tasks(self) -> None:
        if self.settings.market_data_backend == "okx":
            await self.market_gateway.start()
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

    async def stop_background_tasks(self) -> None:
        for task in self.background_tasks:
            task.cancel()
        for task in self.background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.background_tasks.clear()
        await self.market_gateway.stop()
        if self.database_runtime is not None:
            self.database_runtime.dispose()

    async def _refresh_account_loop(self) -> None:
        while True:
            try:
                await self.account_service.refresh()
            except Exception as exc:
                self._record_background_failure(subsystem="account_refresh", exc=exc)
            await asyncio.sleep(self.settings.okx_account_refresh_interval_seconds)

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


def _validate_runtime_settings(settings: AATSSettings, runtime_layering: RuntimeLayering) -> None:
    if (
        runtime_layering.environment_capabilities.persistent_storage_required
        and runtime_layering.environment_capabilities.exchange_submission_enabled
        and settings.storage_mode == "memory"
    ):
        raise ValueError("guarded_simulated_submit_requires_persistent_storage")


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


def build_storage_backends(settings: AATSSettings) -> StorageBackends:
    if settings.storage_mode == "memory":
        return StorageBackends(
            event_store=InMemoryEventStore(),
            audit_repo=InMemoryAuditRepository(),
            portfolio_repo=InMemoryPortfolioRepository(),
            execution_repo=InMemoryExecutionRepository(),
            obligation_repo=InMemoryExecutionObligationRepository(),
            outbox_repo=None,
            reconciliation_repo=InMemoryReconciliationRepository(),
            operator_repo=InMemoryOperatorUserRepository(),
            runtime_profile_repo=InMemoryRuntimeProfileRepository(),
            strategy_profile_repo=InMemoryStrategyProfileRepository(),
        )

    if not settings.database_url:
        raise ValueError("AATS_DATABASE_URL must be configured when storage_mode=postgres")

    database_runtime = create_database_runtime(settings.database_url)
    if settings.database_auto_create_schema:
        create_schema(database_runtime)

    return StorageBackends(
        event_store=PostgresEventStore(database_runtime.session_factory),
        audit_repo=PostgresAuditRepository(database_runtime.session_factory),
        portfolio_repo=PostgresPortfolioRepository(database_runtime.session_factory),
        execution_repo=PostgresExecutionRepository(database_runtime.session_factory),
        obligation_repo=PostgresExecutionObligationRepository(database_runtime.session_factory),
        outbox_repo=PostgresOutboxRepository(database_runtime.session_factory),
        reconciliation_repo=PostgresReconciliationRepository(database_runtime.session_factory),
        operator_repo=PostgresOperatorUserRepository(database_runtime.session_factory),
        runtime_profile_repo=PostgresRuntimeProfileRepository(database_runtime.session_factory),
        strategy_profile_repo=PostgresStrategyProfileRepository(database_runtime.session_factory),
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


async def build_runtime(
    settings: AATSSettings | None = None,
    *,
    bootstrap_portfolio_snapshot: bool = True,
) -> ApplicationRuntime:
    base_settings = settings or load_settings()
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

    kill_switch = KillSwitch()
    mode_controller = RuntimeModeController(
        settings=runtime_settings,
        kill_switch=kill_switch,
        runtime_layering=runtime_layering,
    )

    normalizer = MarketSnapshotNormalizer(exchange_name=runtime_settings.exchange_name)
    market_publisher = MarketSnapshotPublisher(bus=bus)
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
    )

    okx_client = OKXRESTClient(settings=runtime_settings)
    account_service = OKXAccountService(settings=runtime_settings, client=okx_client)
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
    ai_service = AIInferenceService(
        settings=runtime_settings,
        event_store=storage.event_store,
        prompt_builder=PromptBuilder(),
        validator=AssessmentValidator(),
    )
    decision_trigger_policy = DecisionTriggerPolicy(settings=runtime_settings)
    decision_engine = DecisionOrchestrator(
        bus=bus,
        context_builder=DecisionContextBuilder(
            settings=runtime_settings,
            event_store=storage.event_store,
            portfolio_repo=storage.portfolio_repo,
            mode_controller=mode_controller,
            health_service=health_service,
        ),
        baseline_strategy=BaselineStrategy(event_store=storage.event_store),
        ai_service=ai_service,
        target_engine=TargetPositionEngine(settings=runtime_settings),
        metrics=metrics,
    )
    decision_trigger = DecisionCycleTrigger(
        orchestrator=decision_engine,
        market_gateway=market_gateway,
        policy=decision_trigger_policy,
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
    )
    execution_planner = ExecutionPlanner(settings=runtime_settings)
    obligation_service = ExecutionObligationService(
        settings=runtime_settings,
        obligation_repo=storage.obligation_repo,
        account_snapshot_loader=lambda: account_service.refresh(),
        price_provider=market_gateway.latest_price,
    )
    execution_outbox_publisher = None
    if (
        storage.database_runtime is not None
        and isinstance(storage.execution_repo, PostgresExecutionRepository)
        and isinstance(storage.obligation_repo, PostgresExecutionObligationRepository)
        and isinstance(storage.event_store, PostgresEventStore)
        and storage.outbox_repo is not None
    ):
        execution_outbox_publisher = PostgresExecutionOutboxPublisher(
            session_factory=storage.database_runtime.session_factory,
            event_store=storage.event_store,
            execution_repo=storage.execution_repo,
            obligation_repo=storage.obligation_repo,
            outbox_repo=storage.outbox_repo,
            bus=bus,
        )
    order_manager = OrderManager(
        settings=runtime_settings,
        bus=bus,
        adapter=execution_adapter,
        execution_repo=storage.execution_repo,
        obligation_service=obligation_service,
        execution_outbox_publisher=execution_outbox_publisher,
        kill_switch=kill_switch,
    )

    portfolio_service = PortfolioService(
        bus=bus,
        state=PortfolioState(
            initial_usdt_balance=runtime_settings.initial_usdt_balance,
            default_product_type=runtime_settings.trading_product_type,
            default_margin_mode=runtime_settings.margin_mode,
        ),
        snapshot_builder=snapshot_builder,
        portfolio_repo=storage.portfolio_repo,
        price_provider=market_gateway.latest_price,
        metrics=metrics,
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
    )
    recovery_service = ExecutionRecoveryService(
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

    await bus.subscribe(topics.MARKET_SNAPSHOTS, feature_engine.handle_market_snapshot)
    await bus.subscribe(topics.FEATURE_SNAPSHOTS, decision_trigger.handle_feature_snapshot)

    await bus.subscribe(topics.DECISION_CONTEXTS, audit_service.handle_decision_context)
    await bus.subscribe(topics.BASELINE_ASSESSMENTS, audit_service.handle_baseline_assessment)
    await bus.subscribe(topics.AI_ASSESSMENTS, audit_service.handle_ai_assessment)
    await bus.subscribe(topics.POSITION_TARGETS, audit_service.handle_position_target)
    await bus.subscribe(topics.POLICY_DECISIONS, audit_service.handle_policy_decision)
    await bus.subscribe(topics.RISK_DECISIONS, audit_service.handle_risk_decision)
    await bus.subscribe(topics.EXECUTION_PLANS, audit_service.handle_execution_plan)
    await bus.subscribe(topics.ORDER_INTENTS, audit_service.handle_order_intent)
    await bus.subscribe(topics.ORDER_INTENTS, order_manager.handle_order_intent)
    await bus.subscribe(topics.ORDER_UPDATES, audit_service.handle_order_update)
    await bus.subscribe(topics.FILL_EVENTS, audit_service.handle_fill_event)
    await bus.subscribe(topics.FILL_EVENTS, portfolio_service.handle_fill_event)
    await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, ai_service.handle_portfolio_snapshot)
    await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, audit_service.handle_portfolio_snapshot)
    await bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, reconciliation_service.handle_portfolio_snapshot)
    await bus.subscribe(topics.RECONCILIATION_REPORTS, ai_service.handle_reconciliation_report)
    await bus.subscribe(topics.RECONCILIATION_REPORTS, audit_service.handle_reconciliation_report)

    async def handle_position_target(message: dict[str, Any]) -> None:
        target = parse_payload(message, PositionTarget)
        if runtime_layering.environment_capabilities.account_state_source_kind == "exchange":
            await account_service.refresh()

        policy_decision = policy_engine.evaluate(target=target)
        await policy_engine.publish_decision(bus=bus, target=target, decision=policy_decision)
        if not policy_decision.execution_allowed:
            return

        risk_decision = risk_engine.evaluate(target=target)
        await risk_engine.publish_decision(bus=bus, target=target, decision=risk_decision)
        if not risk_decision.approved or risk_decision.halt_required:
            return
        if kill_switch.halted:
            return

        plan = execution_planner.build_plan(
            decision_id=target.decision_id,
            symbol=target.symbol,
            current_position_qty=target.current_position_qty,
            target_position_qty=target.target_position_qty,
            approved_target_position_qty=risk_decision.capped_target_position_qty,
            delta_qty=risk_decision.capped_target_position_qty - target.current_position_qty,
            urgency=target.urgency,
            max_slippage_tolerance_bps=target.max_slippage_tolerance_bps,
            product_type=target.product_type,
            target_leverage=target.target_leverage,
            margin_mode=target.margin_mode,
        )
        if plan is None:
            return
        await execution_planner.publish_plan(bus=bus, plan=plan)

        intent = execution_planner.build_intent(plan=plan)
        if intent is None:
            return
        metrics.increment("order_intents_generated")
        await execution_planner.publish_intent(bus=bus, intent=intent)

    await bus.subscribe(topics.POSITION_TARGETS, handle_position_target)

    if runtime_layering.environment_capabilities.account_state_source_kind == "exchange":
        account_snapshot = await account_service.refresh(force=True)
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
        await portfolio_service.bootstrap_snapshot()
        recovery_status = recovery_artifacts.status.model_copy(
            update={"recovered_snapshot_available": True}
        )
    else:
        recovery_status = recovery_artifacts.status

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

    return ApplicationRuntime(
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
        policy_engine=policy_engine,
        risk_engine=risk_engine,
        kill_switch=kill_switch,
        mode_controller=mode_controller,
        health_service=health_service,
        account_service=account_service,
        metrics=metrics,
        audit_repo=storage.audit_repo,
        portfolio_repo=storage.portfolio_repo,
        execution_repo=storage.execution_repo,
        obligation_repo=storage.obligation_repo,
        reconciliation_repo=storage.reconciliation_repo,
        operator_repo=storage.operator_repo,
        runtime_profile_repo=storage.runtime_profile_repo,
        strategy_profile_repo=storage.strategy_profile_repo,
        recovery_status=recovery_status,
        database_runtime=storage.database_runtime,
        execution_outbox_publisher=execution_outbox_publisher,
    )
