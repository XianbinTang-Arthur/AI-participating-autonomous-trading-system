from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import SchemaBase


ProfileSource = Literal["env_only"]

RUNTIME_PROFILE_MANAGED_FIELDS: tuple[str, ...] = (
    "default_symbol",
    "allowed_symbols",
    "trading_product_type",
    "margin_mode",
    "max_abs_position_qty",
    "max_notional_per_symbol",
    "max_open_orders",
    "default_order_qty",
    "max_target_leverage",
    "default_target_leverage",
    "strategy_short_bias_enabled",
    "strategy_dynamic_leverage_enabled",
    "decision_min_interval_seconds_15m",
    "decision_min_interval_seconds_1h",
    "decision_min_price_move_bps",
    "decision_min_momentum_delta",
)
class RuntimeProfileResolution(SchemaBase):
    profile_source: ProfileSource
    resolved_settings: dict[str, Any] = Field(default_factory=dict)


def runtime_profile_payload_from_settings(settings: AATSSettings) -> dict[str, Any]:
    payload = settings.model_dump(mode="python")
    return {field: payload[field] for field in RUNTIME_PROFILE_MANAGED_FIELDS}


def apply_runtime_profile_payload(settings: AATSSettings, payload: dict[str, Any]) -> AATSSettings:
    current = settings.model_dump(mode="python")
    overlay = {key: payload[key] for key in RUNTIME_PROFILE_MANAGED_FIELDS if key in payload}
    return AATSSettings.model_validate({**current, **overlay})


def summarize_runtime_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "default_symbol": payload.get("default_symbol"),
        "allowed_symbols": payload.get("allowed_symbols"),
        "trading_product_type": payload.get("trading_product_type"),
        "margin_mode": payload.get("margin_mode"),
        "max_target_leverage": payload.get("max_target_leverage"),
        "default_target_leverage": payload.get("default_target_leverage"),
        "default_order_qty": payload.get("default_order_qty"),
        "max_notional_per_symbol": payload.get("max_notional_per_symbol"),
        "strategy_short_bias_enabled": payload.get("strategy_short_bias_enabled"),
        "strategy_dynamic_leverage_enabled": payload.get("strategy_dynamic_leverage_enabled"),
    }
