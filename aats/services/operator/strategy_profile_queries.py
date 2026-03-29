from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.schemas.operator import AuthSource, OperatorRole

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class StrategyProfileQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    @property
    def runtime(self):
        return self.owner.runtime

    @property
    def strategy_profiles(self):
        return self.owner.strategy_profiles

    def snapshot(self) -> dict[str, Any]:
        return self.owner._cached("strategy_profile_snapshot", self.strategy_profiles.snapshot)

    def ai_config_snapshot(self) -> dict[str, Any]:
        return self.owner._cached("strategy_profile_ai_config_snapshot", self.strategy_profiles.ai_config_snapshot)

    def optimization_reports(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.owner._recent_strategy_profile_optimization_report_events()
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="reports",
            serializer=self.owner.payload,
        )

    def selection_decisions(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.owner._recent_strategy_profile_selection_decision_events()
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="decisions",
            serializer=self.owner.payload,
        )

    def activation_history(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.strategy_profile_repo.list_activation_history(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
        )
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="history",
            serializer=lambda item: item.model_dump(mode="json"),
        )

    def activate_profile(
        self,
        *,
        profile_id: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.activate_profile(
            profile_id=profile_id,
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_action(
            action="strategy_profile_manual_activate",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="profile_manually_activated",
            details={
                "requested_profile_id": profile_id,
                "active_profile_id": result["active_revision"]["profile_id"],
            },
        )
        self.owner._invalidate_cache()
        return result

    def restore_auto(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.restore_auto(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_action(
            action="strategy_profile_restore_auto",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="profile_auto_switch_restored",
            details={
                "frozen_by_admin_override": result["activation"].get("frozen_until") is not None,
                "active_profile_id": result["activation"].get("active_profile_id"),
            },
        )
        self.owner._invalidate_cache()
        return result

    def pause_auto(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.pause_auto(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_action(
            action="strategy_profile_pause_auto",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="profile_auto_switch_paused",
            details={
                "active_profile_id": result["activation"].get("active_profile_id"),
            },
        )
        self.owner._invalidate_cache()
        return result

    def _append_action(
        self,
        *,
        action: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        status: str,
        details: dict[str, Any],
    ) -> None:
        self.owner._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="strategy_profile",
            payload_model=self.strategy_profiles.audit_payload(
                action=action,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                status=status,
                details=details,
            ),
        )
