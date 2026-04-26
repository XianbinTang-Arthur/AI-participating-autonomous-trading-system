from __future__ import annotations

from datetime import datetime

from aats.schemas.strategy_profiles import (
    StrategyProfileActivationRecord,
    StrategyProfileActivationState,
    StrategyProfileEvaluationRecord,
    StrategyProfileRecommendation,
    StrategyProfileRejectionRecord,
    StrategyProfileRevision,
)


class InMemoryStrategyProfileRepository:
    def __init__(self) -> None:
        self._revisions: dict[str, StrategyProfileRevision] = {}
        self._activations: dict[tuple[str, str, tuple[str, ...]], StrategyProfileActivationState] = {}
        self._recommendations: dict[str, StrategyProfileRecommendation] = {}
        self._activation_history: list[StrategyProfileActivationRecord] = []
        self._rejections: list[StrategyProfileRejectionRecord] = []
        self._evaluations: list[StrategyProfileEvaluationRecord] = []

    def save_revision(self, revision: StrategyProfileRevision) -> StrategyProfileRevision:
        self._revisions[revision.revision_id] = revision
        return revision

    def get_revision(self, revision_id: str) -> StrategyProfileRevision | None:
        return self._revisions.get(revision_id)

    def list_revisions(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        profile_id: str | None = None,
        status: str | None = None,
    ) -> list[StrategyProfileRevision]:
        rows = list(self._revisions.values())
        if product_type is not None:
            rows = [item for item in rows if item.product_type == product_type]
        if margin_mode is not None:
            rows = [item for item in rows if item.margin_mode == margin_mode]
        if profile_id is not None:
            rows = [item for item in rows if item.profile_id == profile_id]
        if status is not None:
            rows = [item for item in rows if item.status == status]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def activation_state(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> StrategyProfileActivationState:
        key = (product_type, margin_mode, allowed_symbols)
        state = self._activations.get(key)
        if state is None:
            state = StrategyProfileActivationState(
                product_type=product_type,
                margin_mode=margin_mode,
                allowed_symbols=allowed_symbols,
            )
            self._activations[key] = state
        return state

    def save_activation_state(self, state: StrategyProfileActivationState) -> StrategyProfileActivationState:
        key = (state.product_type, state.margin_mode, state.allowed_symbols)
        self._activations[key] = state
        return state

    def save_recommendation(self, recommendation: StrategyProfileRecommendation) -> StrategyProfileRecommendation:
        self._recommendations[recommendation.recommendation_id] = recommendation
        return recommendation

    def get_recommendation(self, recommendation_id: str) -> StrategyProfileRecommendation | None:
        return self._recommendations.get(recommendation_id)

    def latest_recommendation(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> StrategyProfileRecommendation | None:
        rows = [
            item
            for item in self._recommendations.values()
            if item.product_type == product_type
            and item.margin_mode == margin_mode
            and item.allowed_symbols == allowed_symbols
        ]
        return max(rows, key=lambda item: item.generated_at, default=None)

    def list_recommendations(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        decision_status: str | None = None,
    ) -> list[StrategyProfileRecommendation]:
        rows = list(self._recommendations.values())
        if product_type is not None:
            rows = [item for item in rows if item.product_type == product_type]
        if margin_mode is not None:
            rows = [item for item in rows if item.margin_mode == margin_mode]
        if decision_status is not None:
            rows = [item for item in rows if item.decision_status == decision_status]
        return sorted(rows, key=lambda item: item.generated_at, reverse=True)

    def expire_pending_recommendations(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
        now: datetime,
    ) -> int:
        expired = 0
        for recommendation_id, recommendation in list(self._recommendations.items()):
            if (
                recommendation.product_type != product_type
                or recommendation.margin_mode != margin_mode
                or recommendation.allowed_symbols != allowed_symbols
                or recommendation.decision_status != "pending"
                or recommendation.expires_at > now
            ):
                continue
            self._recommendations[recommendation_id] = recommendation.model_copy(
                update={
                    "decision_status": "expired",
                    "decision_reason_code": "recommendation_expired",
                    "decision_reason_detail": "pending recommendation expired before next freshness evaluation",
                }
            )
            expired += 1
        return expired

    def save_activation_record(self, record: StrategyProfileActivationRecord) -> StrategyProfileActivationRecord:
        self._activation_history.append(record)
        return record

    def list_activation_history(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileActivationRecord]:
        rows = list(self._activation_history)
        if product_type is not None:
            rows = [item for item in rows if item.product_type == product_type]
        if margin_mode is not None:
            rows = [item for item in rows if item.margin_mode == margin_mode]
        return sorted(rows, key=lambda item: item.executed_at, reverse=True)

    def save_rejection(self, record: StrategyProfileRejectionRecord) -> StrategyProfileRejectionRecord:
        self._rejections.append(record)
        return record

    def list_rejections(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileRejectionRecord]:
        rows = list(self._rejections)
        if product_type is not None:
            rows = [item for item in rows if item.product_type == product_type]
        if margin_mode is not None:
            rows = [item for item in rows if item.margin_mode == margin_mode]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def save_evaluation(self, record: StrategyProfileEvaluationRecord) -> StrategyProfileEvaluationRecord:
        self._evaluations.append(record)
        return record

    def list_evaluations(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileEvaluationRecord]:
        rows = list(self._evaluations)
        if product_type is not None:
            rows = [item for item in rows if item.product_type == product_type]
        if margin_mode is not None:
            rows = [item for item in rows if item.margin_mode == margin_mode]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)
