from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.system import (
    LeverageSupport,
    MarginModelType,
    OperatingState,
    PositionDirectionality,
    ProductType,
)


RuntimeProfileName = Literal[
    "paper_local",
    "exchange_simulated_spot",
    "exchange_simulated_derivatives",
    "exchange_live_spot",
    "exchange_live_derivatives",
]


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    name: RuntimeProfileName
    description: str
    product_type: ProductType
    exchange_coupled: bool
    exchange_submission_capable: bool
    persistent_storage_required: bool
    account_synchronization_meaningful: bool
    rebaseline_meaningful: bool
    live_trading_blocked: bool
    shorting_supported: bool
    leverage_supported: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EnvironmentCapabilities:
    product_type: ProductType
    market_data_source_kind: str
    account_state_source_kind: str
    execution_adapter_kind: str
    execution_route: str
    exchange_submission_target: str
    exchange_submission_possible: bool
    exchange_submission_enabled: bool
    persistent_storage_required: bool
    exchange_coupled: bool
    local_only: bool
    position_directionality: PositionDirectionality
    leverage_support: LeverageSupport
    margin_model: MarginModelType

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PolicyProfile:
    name: str
    product_type: ProductType
    exchange_submission_allowed_in_principle: bool
    dry_run_only: bool
    requires_human_approval: bool
    enforce_health_blockers: bool
    blocks_on_account_freshness: bool
    blocks_on_reconciliation_freshness: bool
    blocks_on_review_required: bool
    balance_checks_required: bool
    real_money_submission_structurally_blocked: bool
    shorting_allowed: bool
    leverage_allowed: bool
    max_target_leverage: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    name: str
    product_type: ProductType
    startup_baseline_import_supported: bool
    operator_rebaseline_supported: bool
    account_snapshot_required: bool
    review_required_blocks_resume: bool
    reconciliation_required_for_execution_state: bool
    exchange_portfolio_comparison_enabled: bool
    derivatives_position_comparison_enabled: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuntimeLayering:
    runtime_profile: RuntimeProfile
    environment_capabilities: EnvironmentCapabilities
    policy_profile: PolicyProfile
    recovery_policy: RecoveryPolicy
    operating_state: OperatingState
    mode_submit_blocked_reasons: tuple[str, ...]

    def to_mode_payload(self) -> dict[str, object]:
        return {
            "runtime_profile": self.runtime_profile.to_dict(),
            "environment_capabilities": self.environment_capabilities.to_dict(),
            "policy_profile": self.policy_profile.to_dict(),
            "recovery_policy": self.recovery_policy.to_dict(),
            "operating_state": self.operating_state,
            "submit_blocked_reasons": list(self.mode_submit_blocked_reasons),
        }


def resolve_runtime_layering(settings: AATSSettings) -> RuntimeLayering:
    runtime_profile = _resolve_runtime_profile(settings)
    environment_capabilities = _resolve_environment_capabilities(settings=settings, profile=runtime_profile)
    policy_profile = _resolve_policy_profile(settings=settings, profile=runtime_profile, environment=environment_capabilities)
    recovery_policy = _resolve_recovery_policy(settings=settings, profile=runtime_profile)
    operating_state = _resolve_operating_state(settings=settings, profile=runtime_profile, environment=environment_capabilities)
    submit_blocked_reasons = _resolve_mode_submit_blocked_reasons(
        settings=settings,
        profile=runtime_profile,
        operating_state=operating_state,
    )
    return RuntimeLayering(
        runtime_profile=runtime_profile,
        environment_capabilities=environment_capabilities,
        policy_profile=policy_profile,
        recovery_policy=recovery_policy,
        operating_state=operating_state,
        mode_submit_blocked_reasons=submit_blocked_reasons,
    )


def _resolve_runtime_profile(settings: AATSSettings) -> RuntimeProfile:
    if settings.mode == "guarded_live" and settings.execution_backend == "okx":
        if settings.okx_simulated_trading:
            if settings.trading_product_type == "derivatives":
                return RuntimeProfile(
                    name="exchange_simulated_derivatives",
                    description="Exchange-backed simulated submit path for derivatives-style exposure with guarded controls.",
                    product_type="derivatives",
                    exchange_coupled=True,
                    exchange_submission_capable=True,
                    persistent_storage_required=True,
                    account_synchronization_meaningful=True,
                    rebaseline_meaningful=True,
                    live_trading_blocked=False,
                    shorting_supported=True,
                    leverage_supported=True,
                )
            return RuntimeProfile(
                name="exchange_simulated_spot",
                description="Exchange-backed simulated submit path for spot trading with guarded operator controls.",
                product_type="spot",
                exchange_coupled=True,
                exchange_submission_capable=True,
                persistent_storage_required=True,
                account_synchronization_meaningful=True,
                rebaseline_meaningful=True,
                live_trading_blocked=False,
                shorting_supported=False,
                leverage_supported=False,
            )
        if settings.trading_product_type == "derivatives":
            return RuntimeProfile(
                name="exchange_live_derivatives",
                description="Exchange-backed live submit path for derivatives trading with guarded controls.",
                product_type="derivatives",
                exchange_coupled=True,
                exchange_submission_capable=True,
                persistent_storage_required=True,
                account_synchronization_meaningful=True,
                rebaseline_meaningful=True,
                live_trading_blocked=False,
                shorting_supported=True,
                leverage_supported=True,
            )
        return RuntimeProfile(
            name="exchange_live_spot",
            description="Exchange-backed live submit path for spot trading with guarded operator controls.",
            product_type="spot",
            exchange_coupled=True,
            exchange_submission_capable=True,
            persistent_storage_required=True,
            account_synchronization_meaningful=True,
            rebaseline_meaningful=True,
            live_trading_blocked=False,
            shorting_supported=False,
            leverage_supported=False,
        )
    return RuntimeProfile(
        name="paper_local",
        description="Shared local paper-execution profile for demo and real-market observation modes.",
        product_type=settings.trading_product_type,
        exchange_coupled=False,
        exchange_submission_capable=False,
        persistent_storage_required=False,
        account_synchronization_meaningful=False,
        rebaseline_meaningful=False,
        live_trading_blocked=False,
        shorting_supported=settings.trading_product_type == "derivatives",
        leverage_supported=settings.trading_product_type == "derivatives",
    )


def _resolve_environment_capabilities(
    *,
    settings: AATSSettings,
    profile: RuntimeProfile,
) -> EnvironmentCapabilities:
    market_data_source_kind = "demo" if settings.market_data_backend == "demo" else "exchange"
    position_directionality: PositionDirectionality = "bi_directional" if profile.shorting_supported else "long_only"
    leverage_support: LeverageSupport = "supported" if profile.leverage_supported else "none"
    if profile.name == "paper_local":
        account_state_source_kind = (
            "exchange" if settings.account_backend == "okx" and settings.account_read_enabled else "disabled"
        )
        execution_adapter_kind = "paper"
        execution_route = "paper_derivatives_local" if profile.product_type == "derivatives" else "paper_local"
        exchange_submission_target = "none"
        exchange_submission_possible = False
        exchange_submission_enabled = False
        persistent_storage_required = False
        exchange_coupled = False
        local_only = True
    else:
        account_state_source_kind = "exchange" if settings.account_backend == "okx" and settings.account_read_enabled else "disabled"
        execution_adapter_kind = "okx"
        if profile.name == "exchange_simulated_derivatives":
            execution_route = "okx_demo_derivatives_guarded"
            exchange_submission_target = "okx_demo_derivatives"
        elif profile.name == "exchange_simulated_spot":
            execution_route = "okx_demo_guarded"
            exchange_submission_target = "okx_demo_spot"
        elif profile.name == "exchange_live_derivatives":
            execution_route = "okx_live_derivatives_guarded"
            exchange_submission_target = "okx_live_derivatives"
        else:
            execution_route = "okx_live_guarded"
            exchange_submission_target = "okx_live_spot"
        exchange_submission_possible = True
        exchange_submission_enabled = (
            profile.name in {
                "exchange_simulated_spot",
                "exchange_simulated_derivatives",
                "exchange_live_spot",
                "exchange_live_derivatives",
            }
            and settings.live_submit_enabled
            and not settings.guarded_execution_dry_run
        )
        persistent_storage_required = True
        exchange_coupled = True
        local_only = False
    return EnvironmentCapabilities(
        product_type=profile.product_type,
        market_data_source_kind=market_data_source_kind,
        account_state_source_kind=account_state_source_kind,
        execution_adapter_kind=execution_adapter_kind,
        execution_route=execution_route,
        exchange_submission_target=exchange_submission_target,
        exchange_submission_possible=exchange_submission_possible,
        exchange_submission_enabled=exchange_submission_enabled,
        persistent_storage_required=persistent_storage_required,
        exchange_coupled=exchange_coupled,
        local_only=local_only,
        position_directionality=position_directionality,
        leverage_support=leverage_support,
        margin_model=settings.margin_mode,
    )


def _resolve_policy_profile(
    *,
    settings: AATSSettings,
    profile: RuntimeProfile,
    environment: EnvironmentCapabilities,
) -> PolicyProfile:
    if profile.name == "paper_local":
        return PolicyProfile(
            name="paper_local_policy",
            product_type=profile.product_type,
            exchange_submission_allowed_in_principle=False,
            dry_run_only=False,
            requires_human_approval=False,
            enforce_health_blockers=False,
            blocks_on_account_freshness=False,
            blocks_on_reconciliation_freshness=False,
            blocks_on_review_required=False,
            balance_checks_required=False,
            real_money_submission_structurally_blocked=False,
            shorting_allowed=profile.shorting_supported,
            leverage_allowed=profile.leverage_supported,
            max_target_leverage=settings.max_target_leverage,
        )
    dry_run_only = not environment.exchange_submission_enabled
    return PolicyProfile(
        name=f"{profile.name}_policy",
        product_type=profile.product_type,
        exchange_submission_allowed_in_principle=profile.name
        in {
            "exchange_simulated_spot",
            "exchange_simulated_derivatives",
            "exchange_live_spot",
            "exchange_live_derivatives",
        },
        dry_run_only=dry_run_only,
        requires_human_approval=True,
        enforce_health_blockers=True,
        blocks_on_account_freshness=True,
        blocks_on_reconciliation_freshness=True,
        blocks_on_review_required=True,
        balance_checks_required=True,
        real_money_submission_structurally_blocked=False,
        shorting_allowed=profile.shorting_supported,
        leverage_allowed=profile.leverage_supported,
        max_target_leverage=settings.max_target_leverage,
    )


def _resolve_recovery_policy(
    *,
    settings: AATSSettings,
    profile: RuntimeProfile,
) -> RecoveryPolicy:
    startup_baseline_import_supported = (
        settings.bootstrap_portfolio_from_exchange
        and settings.account_backend == "okx"
        and settings.account_read_enabled
    )
    operator_rebaseline_supported = profile.rebaseline_meaningful and settings.account_backend == "okx" and settings.account_read_enabled
    return RecoveryPolicy(
        name=f"{profile.name}_recovery",
        product_type=profile.product_type,
        startup_baseline_import_supported=startup_baseline_import_supported,
        operator_rebaseline_supported=operator_rebaseline_supported,
        account_snapshot_required=profile.exchange_coupled,
        review_required_blocks_resume=profile.exchange_coupled,
        reconciliation_required_for_execution_state=True,
        exchange_portfolio_comparison_enabled=startup_baseline_import_supported and profile.exchange_coupled,
        derivatives_position_comparison_enabled=profile.product_type == "derivatives" and profile.exchange_coupled,
    )


def _resolve_operating_state(
    *,
    settings: AATSSettings,
    profile: RuntimeProfile,
    environment: EnvironmentCapabilities,
) -> OperatingState:
    if profile.name == "paper_local":
        if environment.market_data_source_kind == "demo":
            return "local_demo"
        return "real_market_paper"
    if profile.name == "exchange_simulated_derivatives":
        if environment.exchange_submission_enabled:
            return "guarded_simulated_submit_derivatives_enabled"
        return "guarded_simulated_submit_derivatives_dry_run"
    if profile.name == "exchange_simulated_spot":
        if environment.exchange_submission_enabled:
            return "guarded_simulated_submit_spot_enabled"
        return "guarded_simulated_submit_spot_dry_run"
    if profile.name in {"exchange_live_spot", "exchange_live_derivatives"}:
        if settings.live_submit_enabled and not settings.guarded_execution_dry_run:
            return "guarded_live_enabled"
        return "guarded_live_blocked"
    if settings.live_submit_enabled and not settings.guarded_execution_dry_run:
        return "guarded_live_enabled"
    return "guarded_live_blocked"


def _resolve_mode_submit_blocked_reasons(
    *,
    settings: AATSSettings,
    profile: RuntimeProfile,
    operating_state: OperatingState,
) -> tuple[str, ...]:
    if profile.name == "paper_local":
        if operating_state == "local_demo":
            return ("local_demo_no_exchange_submission",)
        return ("real_market_paper_uses_local_paper_execution",)
    if profile.name in {"exchange_simulated_spot", "exchange_simulated_derivatives"}:
        if settings.guarded_execution_dry_run:
            return ("guarded_execution_dry_run",)
        if not settings.live_submit_enabled:
            return ("live_submit_disabled",)
        return ()
    if profile.name in {"exchange_live_spot", "exchange_live_derivatives"}:
        if settings.guarded_execution_dry_run:
            return ("guarded_execution_dry_run",)
        if not settings.live_submit_enabled:
            return ("live_submit_disabled",)
        return ()
    if operating_state == "guarded_live_enabled":
        return ()
    return ("live_submit_disabled",)
