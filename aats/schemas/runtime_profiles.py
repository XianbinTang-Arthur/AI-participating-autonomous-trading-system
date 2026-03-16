from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import SchemaBase, new_id, utc_now


RuntimeProfileRevisionStatus = Literal[
    "draft",
    "validated",
    "pending_activation",
    "active",
    "superseded",
    "activation_failed",
]
RuntimeProfileChangeClassification = Literal[
    "safe_parameter_adjustment",
    "risk_profile_change",
    "product_posture_change",
    "account_interpretation_change",
]
ProfileSource = Literal["env_only", "env_fallback", "db_active_revision"]
ActivationResult = Literal[
    "activation_not_requested",
    "activation_in_progress",
    "activation_succeeded",
    "activation_failed",
]
RestartCause = Literal["crash_recovery", "pending_activation", "operator_requested_restart"]

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


class RuntimeProfileRevision(SchemaBase):
    revision_id: str = Field(default_factory=lambda: new_id("rtprof"))
    profile_label: str
    status: RuntimeProfileRevisionStatus = "draft"
    change_classification: RuntimeProfileChangeClassification = "safe_parameter_adjustment"
    payload: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    supersedes_revision_id: str | None = None
    activation_note: str | None = None


class RuntimeProfileActivationState(SchemaBase):
    activation_id: str = "runtime_profile_activation"
    active_revision_id: str | None = None
    pending_revision_id: str | None = None
    previous_active_revision_id: str | None = None
    restart_required: bool = False
    restart_requested_at: datetime | None = None
    restart_requested_by: str | None = None
    activation_requested_at: datetime | None = None
    activation_requested_by: str | None = None
    last_activation_result: ActivationResult = "activation_not_requested"
    last_activation_at: datetime | None = None
    last_activation_error: str | None = None
    active_profile_label: str | None = None
    pending_profile_label: str | None = None
    supervisor_heartbeat_at: datetime | None = None
    supervisor_state: str | None = None
    last_restart_cause: RestartCause | None = None
    last_restart_at: datetime | None = None
    last_restart_status: str | None = None
    last_child_exit_code: int | None = None
    restart_attempt_count: int = 0


class RuntimeProfileResolution(SchemaBase):
    profile_source: ProfileSource
    active_revision: RuntimeProfileRevision | None = None
    activation_state: RuntimeProfileActivationState = Field(default_factory=RuntimeProfileActivationState)
    resolved_settings: dict[str, Any] = Field(default_factory=dict)


class RuntimeProfileDiff(SchemaBase):
    changed_fields: list[str] = Field(default_factory=list)
    previous_values: dict[str, Any] = Field(default_factory=dict)
    next_values: dict[str, Any] = Field(default_factory=dict)
    classification: RuntimeProfileChangeClassification


class RuntimeProfilePreflightResult(SchemaBase):
    allowed: bool
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    message: str = "-"


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


def classify_runtime_profile_change(
    previous_payload: dict[str, Any],
    next_payload: dict[str, Any],
) -> RuntimeProfileChangeClassification:
    if previous_payload.get("trading_product_type") != next_payload.get("trading_product_type"):
        return "product_posture_change"
    if previous_payload.get("margin_mode") != next_payload.get("margin_mode"):
        return "account_interpretation_change"
    risk_fields = {
        "max_target_leverage",
        "default_target_leverage",
        "strategy_short_bias_enabled",
        "strategy_dynamic_leverage_enabled",
        "max_abs_position_qty",
        "max_notional_per_symbol",
        "max_open_orders",
    }
    if any(previous_payload.get(field) != next_payload.get(field) for field in risk_fields):
        return "risk_profile_change"
    return "safe_parameter_adjustment"


def diff_runtime_profile_payload(
    previous_payload: dict[str, Any],
    next_payload: dict[str, Any],
) -> RuntimeProfileDiff:
    changed_fields = [
        field
        for field in RUNTIME_PROFILE_MANAGED_FIELDS
        if previous_payload.get(field) != next_payload.get(field)
    ]
    return RuntimeProfileDiff(
        changed_fields=changed_fields,
        previous_values={field: previous_payload.get(field) for field in changed_fields},
        next_values={field: next_payload.get(field) for field in changed_fields},
        classification=classify_runtime_profile_change(previous_payload, next_payload),
    )


def stage_activation_state(
    state: RuntimeProfileActivationState,
    *,
    revision: RuntimeProfileRevision,
    actor_identity: str | None,
) -> RuntimeProfileActivationState:
    now = utc_now()
    return state.model_copy(
        update={
            "pending_revision_id": revision.revision_id,
            "pending_profile_label": revision.profile_label,
            "restart_required": True,
            "restart_requested_at": now,
            "restart_requested_by": actor_identity,
            "activation_requested_at": now,
            "activation_requested_by": actor_identity,
        }
    )
