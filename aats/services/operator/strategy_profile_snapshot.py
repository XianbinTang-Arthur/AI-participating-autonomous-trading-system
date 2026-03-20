from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aats.services.operator.strategy_profiles import StrategyProfileControlService


def build_strategy_profile_snapshot(service: "StrategyProfileControlService") -> dict[str, Any]:
    service.ensure_seed_profiles()
    state = service._activation_state()
    active = service._revision(state.active_revision_id)
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
        "revisions": [service._revision_view(item) for item in revisions],
        "profile_space": service._profile_space(revisions=revisions),
        "comparison_report": comparison_report.model_dump(mode="json"),
        "latest_optimization_report": service._latest_optimization_report_payload(),
        "latest_selection_decision": service._latest_selection_decision_payload(),
        "execution_parameter_suggestion_capability": service._execution_parameter_suggestion_capability(),
        "activation_history": [
            item.model_dump(mode="json")
            for item in service.repo.list_activation_history(
                product_type=service.settings.trading_product_type,
                margin_mode=service.settings.margin_mode,
            )[:20]
        ],
    }


def build_strategy_profile_ai_config_snapshot(service: "StrategyProfileControlService") -> dict[str, Any]:
    service.ensure_seed_profiles()
    state = service._activation_state()
    active = service._revision(state.active_revision_id)
    return {
        "activation": {
            "active_profile_id": state.active_profile_id,
            "active_revision_id": state.active_revision_id,
            "last_activation_at": state.last_activation_at,
            "last_activation_result": state.last_activation_result,
        },
        "active_revision": None
        if active is None
        else {
            "revision_id": active.revision_id,
            "profile_id": active.profile_id,
            "profile_label": active.profile_label,
            "payload": active.payload,
        },
        "latest_selection_decision": service._latest_selection_decision_payload(),
        "activation_history": [
            {
                "to_profile_id": item.to_profile_id,
                "trigger_type": item.trigger_type,
                "executed_at": item.executed_at,
            }
            for item in service.repo.list_activation_history(
                product_type=service.settings.trading_product_type,
                margin_mode=service.settings.margin_mode,
            )[:10]
        ],
    }
