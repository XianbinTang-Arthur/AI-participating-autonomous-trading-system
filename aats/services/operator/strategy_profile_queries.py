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

    def auto_rollback_policy(self) -> dict[str, Any]:
        return self.strategy_profiles.snapshot().get("auto_rollback_policy", {})

    def auto_rollback_policy_history(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.strategy_profiles.snapshot().get("auto_rollback_policy_history", [])
        return self.owner._paginate_rows(rows, limit=limit, offset=offset, key="history")

    def activation_policy(self) -> dict[str, Any]:
        return self.strategy_profiles.snapshot().get("activation_policy", {})

    def activation_policy_history(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.strategy_profiles.snapshot().get("activation_policy_history", [])
        return self.owner._paginate_rows(rows, limit=limit, offset=offset, key="history")

    def recommendations(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.strategy_profile_repo.list_recommendations(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
        )
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="recommendations",
            serializer=lambda item: item.model_dump(mode="json"),
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

    def rejections(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.strategy_profile_repo.list_rejections(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
        )
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="rejections",
            serializer=lambda item: item.model_dump(mode="json"),
        )

    def evaluations(self, *, limit: int, offset: int) -> dict[str, Any]:
        rows = self.runtime.strategy_profile_repo.list_evaluations(
            product_type=self.runtime.settings.trading_product_type,
            margin_mode=self.runtime.settings.margin_mode,
        )
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="evaluations",
            serializer=lambda item: item.model_dump(mode="json"),
        )

    async def evaluate_now(
        self,
        *,
        allow_auto_activation: bool,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = await self.strategy_profiles.evaluate_now(allow_auto_activation=allow_auto_activation)
        self._append_action(
            action="strategy_profile_evaluate",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="recommendation_generated",
            details={
                "recommended_profile_id": result["recommendation"]["recommended_profile_id"],
                "allow_auto_activation": allow_auto_activation,
                "auto_activation_executed": bool(
                    result.get("auto_activation") or result.get("profile_activation_policy") or result.get("auto_rollback")
                ),
            },
        )
        self.owner._invalidate_cache()
        return result

    def accept_recommendation(
        self,
        *,
        recommendation_id: str,
        activation_mode: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.accept_recommendation(
            recommendation_id=recommendation_id,
            activation_mode=activation_mode,
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_action(
            action="strategy_profile_accept",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status=result["status"],
            details={
                "recommendation_id": recommendation_id,
                "activation_mode": activation_mode,
                "active_profile_id": result.get("active_revision", {}).get("profile_id"),
            },
        )
        self.owner._invalidate_cache()
        return result

    def reject_recommendation(
        self,
        *,
        recommendation_id: str,
        reason_code: str,
        reason_detail: str | None,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.reject_recommendation(
            recommendation_id=recommendation_id,
            reason_code=reason_code,
            reason_detail=reason_detail,
            actor_role=actor_role,
            actor_identity=actor_identity,
        )
        self._append_action(
            action="strategy_profile_reject",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="recommendation_rejected",
            details={"recommendation_id": recommendation_id, "reason_code": reason_code},
        )
        self.owner._invalidate_cache()
        return result

    def activate_pending(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.activate_pending(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_action(
            action="strategy_profile_activate_pending",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="pending_profile_activated",
            details={"active_profile_id": result["active_revision"]["profile_id"]},
        )
        self.owner._invalidate_cache()
        return result

    def rollback(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        result = self.strategy_profiles.rollback(
            reason=reason,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
        )
        self._append_action(
            action="strategy_profile_rollback",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="profile_rolled_back",
            details={"active_profile_id": result["active_revision"]["profile_id"]},
        )
        self.owner._invalidate_cache()
        return result

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

    def update_auto_rollback_policy(
        self,
        *,
        enabled: bool,
        review_required_only: bool,
        min_trade_count: int,
        cooldown_seconds: float,
        matrix_allowed_symbols: tuple[str, ...],
        matrix_allowed_regimes: tuple[str, ...],
        matrix_allowed_profiles: tuple[str, ...],
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.update_auto_rollback_policy(
            enabled=enabled,
            review_required_only=review_required_only,
            min_trade_count=min_trade_count,
            cooldown_seconds=cooldown_seconds,
            matrix_allowed_symbols=matrix_allowed_symbols,
            matrix_allowed_regimes=matrix_allowed_regimes,
            matrix_allowed_profiles=matrix_allowed_profiles,
            reason=reason,
            actor_identity=actor_identity,
        )
        self._append_action(
            action="strategy_profile_rollback",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="auto_rollback_policy_updated",
            details=policy,
        )
        self.owner._invalidate_cache()
        return {"policy": policy}

    def approve_auto_rollback_policy(
        self,
        *,
        policy_id: str | None,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.approve_auto_rollback_policy(
            policy_id=policy_id,
            actor_identity=actor_identity,
            reason=reason,
        )
        self._append_action(
            action="strategy_profile_rollback",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="auto_rollback_policy_approved",
            details=policy,
        )
        self.owner._invalidate_cache()
        return {"policy": policy}

    def freeze_auto_rollback_policy(
        self,
        *,
        frozen: bool,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.freeze_auto_rollback_policy(
            frozen=frozen,
            actor_identity=actor_identity,
            reason=reason,
        )
        self._append_action(
            action="strategy_profile_rollback",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="auto_rollback_policy_frozen" if frozen else "auto_rollback_policy_unfrozen",
            details=policy,
        )
        self.owner._invalidate_cache()
        return {"policy": policy}

    def update_activation_policy(
        self,
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
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.update_activation_policy(
            enabled=enabled,
            min_composite_score=min_composite_score,
            min_offline_replay_score=min_offline_replay_score,
            min_recommendation_strength=min_recommendation_strength,
            require_positive_replay_consensus=require_positive_replay_consensus,
            disallow_when_shadow_review_required=disallow_when_shadow_review_required,
            matrix_allowed_symbols=matrix_allowed_symbols,
            matrix_allowed_regimes=matrix_allowed_regimes,
            matrix_allowed_profiles=matrix_allowed_profiles,
            reason=reason,
            actor_identity=actor_identity,
        )
        self._append_action(
            action="strategy_profile_activation_policy",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="activation_policy_updated",
            details=policy,
        )
        self.owner._invalidate_cache()
        return {"policy": policy}

    def approve_activation_policy(
        self,
        *,
        policy_id: str | None,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.approve_activation_policy(
            policy_id=policy_id,
            actor_identity=actor_identity,
            reason=reason,
        )
        self._append_action(
            action="strategy_profile_activation_policy",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="activation_policy_approved",
            details=policy,
        )
        self.owner._invalidate_cache()
        return {"policy": policy}

    def freeze_activation_policy(
        self,
        *,
        frozen: bool,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        policy = self.strategy_profiles.freeze_activation_policy(
            frozen=frozen,
            actor_identity=actor_identity,
            reason=reason,
        )
        self._append_action(
            action="strategy_profile_activation_policy",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            status="activation_policy_frozen" if frozen else "activation_policy_unfrozen",
            details=policy,
        )
        self.owner._invalidate_cache()
        return {"policy": policy}

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
