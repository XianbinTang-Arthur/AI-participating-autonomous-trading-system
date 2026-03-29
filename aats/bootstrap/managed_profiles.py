from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml


ManagedEnvProfile = Literal["spot", "derivatives", "spot_live", "derivatives_live"]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class ManagedProfileDefinition:
    profile: ManagedEnvProfile
    runtime_defaults: dict[str, Any]
    strategy_tuning_relative_path: str

    def strategy_tuning_path(self, project_root: Path | str = ".") -> Path:
        return Path(project_root) / self.strategy_tuning_relative_path


MANAGED_PROFILE_DEFINITIONS: dict[ManagedEnvProfile, ManagedProfileDefinition] = {
    "spot": ManagedProfileDefinition(
        profile="spot",
        runtime_defaults={
            "environment": "dev",
            "config_profile": "guarded_spot_enabled",
            "startup_profile": "spot",
            "mode": "guarded_live",
            "storage_mode": "postgres",
            "market_data_backend": "okx",
            "execution_backend": "okx",
            "account_backend": "okx",
            "account_read_enabled": True,
            "live_submit_enabled": True,
            "guarded_execution_dry_run": False,
            "bootstrap_portfolio_from_exchange": True,
            "trading_product_type": "spot",
            "margin_mode": "cash",
            "okx_simulated_trading": True,
            "operator_auth_enabled": True,
            "operator_session_cookie_secure": False,
        },
        strategy_tuning_relative_path="configs/strategy_profiles/spot.yaml",
    ),
    "spot_live": ManagedProfileDefinition(
        profile="spot_live",
        runtime_defaults={
            "environment": "prod",
            "config_profile": "guarded_spot_enabled",
            "startup_profile": "spot",
            "mode": "guarded_live",
            "storage_mode": "postgres",
            "market_data_backend": "okx",
            "execution_backend": "okx",
            "account_backend": "okx",
            "account_read_enabled": True,
            "live_submit_enabled": True,
            "guarded_execution_dry_run": False,
            "bootstrap_portfolio_from_exchange": True,
            "trading_product_type": "spot",
            "margin_mode": "cash",
            "okx_simulated_trading": False,
            "operator_auth_enabled": True,
            "operator_session_cookie_secure": True,
        },
        strategy_tuning_relative_path="configs/strategy_profiles/spot_live.yaml",
    ),
    "derivatives": ManagedProfileDefinition(
        profile="derivatives",
        runtime_defaults={
            "environment": "dev",
            "config_profile": "guarded_derivatives_enabled",
            "startup_profile": "derivatives",
            "mode": "guarded_live",
            "storage_mode": "postgres",
            "market_data_backend": "okx",
            "execution_backend": "okx",
            "account_backend": "okx",
            "account_read_enabled": True,
            "live_submit_enabled": True,
            "guarded_execution_dry_run": False,
            "bootstrap_portfolio_from_exchange": True,
            "trading_product_type": "derivatives",
            "margin_mode": "cross",
            "derivatives_position_mode": "net",
            "derivatives_hedge_transition_mode": "close_then_open",
            "derivatives_require_exchange_pos_mode_match": True,
            "okx_simulated_trading": True,
            "operator_auth_enabled": True,
            "operator_session_cookie_secure": False,
        },
        strategy_tuning_relative_path="configs/strategy_profiles/derivatives.yaml",
    ),
    "derivatives_live": ManagedProfileDefinition(
        profile="derivatives_live",
        runtime_defaults={
            "environment": "prod",
            "config_profile": "guarded_derivatives_enabled",
            "startup_profile": "derivatives",
            "mode": "guarded_live",
            "storage_mode": "postgres",
            "market_data_backend": "okx",
            "execution_backend": "okx",
            "account_backend": "okx",
            "account_read_enabled": True,
            "live_submit_enabled": True,
            "guarded_execution_dry_run": False,
            "bootstrap_portfolio_from_exchange": True,
            "trading_product_type": "derivatives",
            "margin_mode": "cross",
            "derivatives_position_mode": "hedge",
            "derivatives_hedge_transition_mode": "close_then_open",
            "derivatives_require_exchange_pos_mode_match": True,
            "okx_simulated_trading": False,
            "operator_auth_enabled": True,
            "operator_session_cookie_secure": True,
        },
        strategy_tuning_relative_path="configs/strategy_profiles/derivatives_live.yaml",
    ),
}


MANAGED_PROFILE_DERIVED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "AATS_CONFIG_PROFILE",
        "AATS_ENVIRONMENT",
        "AATS_STARTUP_PROFILE",
        "AATS_MODE",
        "AATS_STORAGE_MODE",
        "AATS_MARKET_DATA_BACKEND",
        "AATS_EXECUTION_BACKEND",
        "AATS_ACCOUNT_BACKEND",
        "AATS_ACCOUNT_READ_ENABLED",
        "AATS_LIVE_SUBMIT_ENABLED",
        "AATS_GUARDED_EXECUTION_DRY_RUN",
        "AATS_BOOTSTRAP_PORTFOLIO_FROM_EXCHANGE",
        "AATS_TRADING_PRODUCT_TYPE",
        "AATS_MARGIN_MODE",
        "AATS_DERIVATIVES_POSITION_MODE",
        "AATS_DERIVATIVES_HEDGE_TRANSITION_MODE",
        "AATS_DERIVATIVES_REQUIRE_EXCHANGE_POS_MODE_MATCH",
        "AATS_STRATEGY_HEDGE_OPPORTUNISTIC_ROLLOUT_STAGE",
        "AATS_STRATEGY_HEDGE_INDEPENDENT_ROLLOUT_STAGE",
        "AATS_OKX_SIMULATED_TRADING",
        "AATS_OPERATOR_AUTH_ENABLED",
        "AATS_OPERATOR_SESSION_COOKIE_SECURE",
        "AATS_PRIMARY_TIMEFRAME",
        "AATS_SECONDARY_TIMEFRAME",
    }
)


def load_managed_profile_values(
    profile: ManagedEnvProfile,
    *,
    project_root: Path | str = PROJECT_ROOT,
) -> dict[str, Any]:
    definition = MANAGED_PROFILE_DEFINITIONS[profile]
    merged = dict(definition.runtime_defaults)
    strategy_path = definition.strategy_tuning_path(project_root)
    if strategy_path.exists():
        merged.update(yaml.safe_load(strategy_path.read_text(encoding="utf-8")) or {})
    return merged
