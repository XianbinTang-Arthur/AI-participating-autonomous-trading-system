from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aats.services.operator.strategy_profiles import StrategyProfileControlService


def build_strategy_profile_snapshot(service: "StrategyProfileControlService") -> dict[str, Any]:
    service.ensure_seed_profiles()
    state = service._activation_state()
    active = service._revision(state.active_revision_id)
    pending = service._revision(state.pending_revision_id)
    latest_recommendation = service.repo.latest_recommendation(
        product_type=service.settings.trading_product_type,
        margin_mode=service.settings.margin_mode,
        allowed_symbols=service.settings.allowed_symbols,
    )
    revisions = service.repo.list_revisions(
        product_type=service.settings.trading_product_type,
        margin_mode=service.settings.margin_mode,
    )
    comparison_report = service._comparison_report(revisions=revisions, state=state)
    return {
        "scope": service._scope(),
        "safety_state": service._safety_state(),
        "activation": state.model_dump(mode="json"),
        "active_revision": service._revision_view(active),
        "pending_revision": service._revision_view(pending),
        "latest_recommendation": latest_recommendation.model_dump(mode="json") if latest_recommendation else None,
        "revisions": [service._revision_view(item) for item in revisions],
        "profile_space": service._profile_space(revisions=revisions),
        "comparison_report": comparison_report.model_dump(mode="json"),
        "latest_optimization_report": service._latest_optimization_report_payload(),
        "latest_selection_decision": service._latest_selection_decision_payload(),
        "auto_rollback_policy": service._auto_rollback_policy_view(),
        "auto_rollback_policy_staged": service._staged_auto_rollback_policy_view(),
        "auto_rollback_policy_history": service._auto_rollback_policy_history_payload(limit=10),
        "activation_policy": service._activation_policy_view(),
        "activation_policy_staged": service._staged_activation_policy_view(),
        "activation_policy_history": service._activation_policy_history_payload(limit=10),
        "execution_parameter_suggestion_capability": service._execution_parameter_suggestion_capability(),
        "activation_history": [
            item.model_dump(mode="json")
            for item in service.repo.list_activation_history(
                product_type=service.settings.trading_product_type,
                margin_mode=service.settings.margin_mode,
            )[:20]
        ],
        "rejections": [
            item.model_dump(mode="json")
            for item in service.repo.list_rejections(
                product_type=service.settings.trading_product_type,
                margin_mode=service.settings.margin_mode,
            )[:20]
        ],
        "evaluations": [
            item.model_dump(mode="json")
            for item in service.repo.list_evaluations(
                product_type=service.settings.trading_product_type,
                margin_mode=service.settings.margin_mode,
            )[:20]
        ],
    }
