from __future__ import annotations

from typing import TYPE_CHECKING

from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.strategy_profile_reports import (
    StrategyProfileActivationPolicyConfig,
    StrategyProfileAutoRollbackPolicyConfig,
)

if TYPE_CHECKING:
    from aats.services.operator.strategy_profiles import StrategyProfileControlService


def auto_rollback_policy_view(service: "StrategyProfileControlService") -> dict:
    return resolved_auto_rollback_policy(service).model_dump(mode="json")


def staged_auto_rollback_policy_view(service: "StrategyProfileControlService") -> dict | None:
    staged = next(
        (item for item in auto_rollback_policy_history(service, limit=50) if item.policy_status == "staged"),
        None,
    )
    return staged.model_dump(mode="json") if staged is not None else None


def stored_auto_rollback_policy(
    service: "StrategyProfileControlService",
) -> StrategyProfileAutoRollbackPolicyConfig | None:
    history = auto_rollback_policy_history(service, limit=50)
    return next((item for item in history if item.policy_status == "approved"), None)


def auto_rollback_policy_history(
    service: "StrategyProfileControlService",
    *,
    limit: int | None = None,
) -> list[StrategyProfileAutoRollbackPolicyConfig]:
    rows = [
        StrategyProfileAutoRollbackPolicyConfig.model_validate(item.payload)
        for item in reversed(
            service.event_store.by_topic_scoped(
                topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES,
                scope=service.runtime_state_scope,
            )
        )
        if isinstance(item.payload, dict)
    ]
    if limit is not None:
        rows = rows[:limit]
    return rows


def auto_rollback_policy_history_payload(
    service: "StrategyProfileControlService",
    *,
    limit: int | None = None,
) -> list[dict]:
    return [item.model_dump(mode="json") for item in auto_rollback_policy_history(service, limit=limit)]


def resolved_auto_rollback_policy(
    service: "StrategyProfileControlService",
) -> StrategyProfileAutoRollbackPolicyConfig:
    stored = stored_auto_rollback_policy(service)
    if stored is not None:
        return stored
    settings = service.settings
    return StrategyProfileAutoRollbackPolicyConfig(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
        allowed_symbols=tuple(settings.allowed_symbols),
        policy_status="settings_fallback",
        effective=True,
        enabled=bool(settings.strategy_profile_auto_rollback_enabled),
        review_required_only=bool(settings.strategy_profile_auto_rollback_review_required_only),
        min_trade_count=int(settings.strategy_profile_auto_rollback_min_trade_count),
        cooldown_seconds=float(settings.strategy_profile_auto_rollback_cooldown_seconds),
        matrix_allowed_symbols=tuple(settings.strategy_profile_auto_rollback_allowed_symbols),
        matrix_allowed_regimes=tuple(settings.strategy_profile_auto_rollback_allowed_regimes),
        matrix_allowed_profiles=tuple(settings.strategy_profile_auto_rollback_allowed_profiles),
        updated_by="runtime_settings_default",
        update_reason="settings_fallback",
    )


def activation_policy_view(service: "StrategyProfileControlService") -> dict:
    return resolved_activation_policy(service).model_dump(mode="json")


def staged_activation_policy_view(service: "StrategyProfileControlService") -> dict | None:
    staged = next(
        (item for item in activation_policy_history(service, limit=50) if item.policy_status == "staged"),
        None,
    )
    return staged.model_dump(mode="json") if staged is not None else None


def stored_activation_policy(
    service: "StrategyProfileControlService",
) -> StrategyProfileActivationPolicyConfig | None:
    history = activation_policy_history(service, limit=50)
    return next((item for item in history if item.policy_status == "approved"), None)


def activation_policy_history(
    service: "StrategyProfileControlService",
    *,
    limit: int | None = None,
) -> list[StrategyProfileActivationPolicyConfig]:
    rows = [
        StrategyProfileActivationPolicyConfig.model_validate(item.payload)
        for item in reversed(
            service.event_store.by_topic_scoped(
                topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,
                scope=service.runtime_state_scope,
            )
        )
        if isinstance(item.payload, dict)
    ]
    if limit is not None:
        rows = rows[:limit]
    return rows


def activation_policy_history_payload(
    service: "StrategyProfileControlService",
    *,
    limit: int | None = None,
) -> list[dict]:
    return [item.model_dump(mode="json") for item in activation_policy_history(service, limit=limit)]


def resolved_activation_policy(
    service: "StrategyProfileControlService",
) -> StrategyProfileActivationPolicyConfig:
    stored = stored_activation_policy(service)
    if stored is not None:
        return stored
    settings = service.settings
    return StrategyProfileActivationPolicyConfig(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
        allowed_symbols=tuple(settings.allowed_symbols),
        policy_status="settings_fallback",
        effective=True,
        enabled=bool(settings.strategy_profile_activation_policy_enabled),
        min_composite_score=float(settings.strategy_profile_auto_activation_min_composite_score),
        min_offline_replay_score=float(settings.strategy_profile_auto_activation_min_offline_replay_score),
        min_recommendation_strength=float(settings.strategy_profile_auto_activation_min_recommendation_strength),
        require_positive_replay_consensus=bool(
            settings.strategy_profile_auto_activation_require_positive_replay_consensus
        ),
        disallow_when_shadow_review_required=bool(
            settings.strategy_profile_auto_activation_disallow_when_shadow_review_required
        ),
        matrix_allowed_symbols=tuple(settings.strategy_profile_activation_policy_allowed_symbols),
        matrix_allowed_regimes=tuple(settings.strategy_profile_activation_policy_allowed_regimes),
        matrix_allowed_profiles=tuple(settings.strategy_profile_activation_policy_allowed_profiles),
        updated_by="runtime_settings_default",
        update_reason="settings_fallback",
    )


def update_activation_policy(
    service: "StrategyProfileControlService",
    *,
    enabled: bool,
    min_composite_score: float,
    min_offline_replay_score: float,
    min_recommendation_strength: float,
    require_positive_replay_consensus: bool,
    disallow_when_shadow_review_required: bool,
    matrix_allowed_symbols: tuple[str, ...],
    matrix_allowed_regimes: tuple[str, ...],
    matrix_allowed_profiles: tuple[str, ...],
    reason: str,
    actor_identity: str | None,
) -> dict:
    previous = activation_policy_history(service, limit=1)
    previous_policy = previous[0] if previous else None
    policy = StrategyProfileActivationPolicyConfig(
        product_type=service.settings.trading_product_type,
        margin_mode=service.settings.margin_mode,
        allowed_symbols=tuple(service.settings.allowed_symbols),
        previous_policy_id=previous_policy.policy_id if previous_policy is not None else None,
        policy_status="staged",
        effective=False,
        enabled=enabled,
        min_composite_score=float(min_composite_score),
        min_offline_replay_score=float(min_offline_replay_score),
        min_recommendation_strength=float(min_recommendation_strength),
        require_positive_replay_consensus=bool(require_positive_replay_consensus),
        disallow_when_shadow_review_required=bool(disallow_when_shadow_review_required),
        matrix_allowed_symbols=tuple(matrix_allowed_symbols),
        matrix_allowed_regimes=tuple(matrix_allowed_regimes),
        matrix_allowed_profiles=tuple(matrix_allowed_profiles),
        updated_by=actor_identity,
        update_reason=reason,
    )
    service.event_store.append(
        build_envelope(
            topic=topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,
            key=service.settings.default_symbol,
            payload_model=policy,
            source_component="strategy_profile_service",
        )
    )
    return policy.model_dump(mode="json")


def approve_activation_policy(
    service: "StrategyProfileControlService",
    *,
    policy_id: str | None,
    actor_identity: str | None,
    reason: str,
) -> dict:
    history = activation_policy_history(service, limit=50)
    target = next((item for item in history if policy_id is None or item.policy_id == policy_id), None)
    if target is None:
        raise KeyError("strategy_profile_activation_policy_not_found")
    approved = target.model_copy(
        update={
            "policy_status": "approved",
            "effective": not bool(target.frozen),
            "approved_by": actor_identity,
            "approved_at": utc_now(),
            "update_reason": reason,
        }
    )
    service.event_store.append(
        build_envelope(
            topic=topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,
            key=service.settings.default_symbol,
            payload_model=approved,
            source_component="strategy_profile_service",
        )
    )
    return approved.model_dump(mode="json")


def freeze_activation_policy(
    service: "StrategyProfileControlService",
    *,
    frozen: bool,
    actor_identity: str | None,
    reason: str,
) -> dict:
    current = resolved_activation_policy(service)
    updated = current.model_copy(
        update={
            "policy_status": "approved",
            "effective": False if frozen else True,
            "frozen": frozen,
            "frozen_by": actor_identity if frozen else None,
            "frozen_at": utc_now() if frozen else None,
            "freeze_reason": reason if frozen else None,
            "update_reason": reason,
        }
    )
    service.event_store.append(
        build_envelope(
            topic=topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,
            key=service.settings.default_symbol,
            payload_model=updated,
            source_component="strategy_profile_service",
        )
    )
    return updated.model_dump(mode="json")


def update_auto_rollback_policy(
    service: "StrategyProfileControlService",
    *,
    enabled: bool,
    review_required_only: bool,
    min_trade_count: int,
    cooldown_seconds: float,
    matrix_allowed_symbols: tuple[str, ...],
    matrix_allowed_regimes: tuple[str, ...],
    matrix_allowed_profiles: tuple[str, ...],
    reason: str,
    actor_identity: str | None,
) -> dict:
    previous = auto_rollback_policy_history(service, limit=1)
    previous_policy = previous[0] if previous else None
    policy = StrategyProfileAutoRollbackPolicyConfig(
        product_type=service.settings.trading_product_type,
        margin_mode=service.settings.margin_mode,
        allowed_symbols=tuple(service.settings.allowed_symbols),
        previous_policy_id=previous_policy.policy_id if previous_policy is not None else None,
        policy_status="staged",
        effective=False,
        enabled=enabled,
        review_required_only=review_required_only,
        min_trade_count=max(int(min_trade_count), 0),
        cooldown_seconds=max(float(cooldown_seconds), 0.0),
        matrix_allowed_symbols=tuple(matrix_allowed_symbols),
        matrix_allowed_regimes=tuple(matrix_allowed_regimes),
        matrix_allowed_profiles=tuple(matrix_allowed_profiles),
        updated_by=actor_identity,
        update_reason=reason,
    )
    service.event_store.append(
        build_envelope(
            topic=topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES,
            key=service.settings.default_symbol,
            payload_model=policy,
            source_component="strategy_profile_service",
        )
    )
    return policy.model_dump(mode="json")


def approve_auto_rollback_policy(
    service: "StrategyProfileControlService",
    *,
    policy_id: str | None,
    actor_identity: str | None,
    reason: str,
) -> dict:
    history = auto_rollback_policy_history(service, limit=50)
    target = next((item for item in history if policy_id is None or item.policy_id == policy_id), None)
    if target is None:
        raise KeyError("strategy_profile_auto_rollback_policy_not_found")
    approved = target.model_copy(
        update={
            "policy_status": "approved",
            "effective": not bool(target.frozen),
            "approved_by": actor_identity,
            "approved_at": utc_now(),
            "update_reason": reason,
        }
    )
    service.event_store.append(
        build_envelope(
            topic=topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES,
            key=service.settings.default_symbol,
            payload_model=approved,
            source_component="strategy_profile_service",
        )
    )
    return approved.model_dump(mode="json")


def freeze_auto_rollback_policy(
    service: "StrategyProfileControlService",
    *,
    frozen: bool,
    actor_identity: str | None,
    reason: str,
) -> dict:
    current = resolved_auto_rollback_policy(service)
    updated = current.model_copy(
        update={
            "policy_status": "approved",
            "effective": False if frozen else True,
            "frozen": frozen,
            "frozen_by": actor_identity if frozen else None,
            "frozen_at": utc_now() if frozen else None,
            "freeze_reason": reason if frozen else None,
            "update_reason": reason,
        }
    )
    service.event_store.append(
        build_envelope(
            topic=topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES,
            key=service.settings.default_symbol,
            payload_model=updated,
            source_component="strategy_profile_service",
        )
    )
    return updated.model_dump(mode="json")
