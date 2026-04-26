from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.strategy_profiles import (
    StrategyProfileActivationRecord,
    StrategyProfileActivationState,
    StrategyProfileEvaluationRecord,
    StrategyProfileRecommendation,
    StrategyProfileRejectionRecord,
    StrategyProfileRevision,
    strategy_profile_scope_hash,
)
from aats.storage.sqlalchemy_models import (
    StrategyProfileActivationHistoryModel,
    StrategyProfileActivationModel,
    StrategyProfileEvaluationModel,
    StrategyProfileRecommendationModel,
    StrategyProfileRejectionModel,
    StrategyProfileRevisionModel,
)


class PostgresStrategyProfileRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_revision(self, revision: StrategyProfileRevision) -> StrategyProfileRevision:
        payload = revision.model_dump(mode="json")
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileRevisionModel).where(StrategyProfileRevisionModel.revision_id == revision.revision_id)
            )
            if row is None:
                row = StrategyProfileRevisionModel(
                    revision_id=revision.revision_id,
                    profile_family=revision.profile_family,
                    profile_id=revision.profile_id,
                    profile_label=revision.profile_label,
                    version=revision.version,
                    status=revision.status,
                    risk_level=revision.risk_level,
                    market_intent=revision.market_intent,
                    product_type=revision.product_type,
                    margin_mode=revision.margin_mode,
                    allowed_symbols_json=list(revision.allowed_symbols),
                    hot_safe_only=revision.hot_safe_only,
                    auto_switch_allowed=revision.auto_switch_allowed,
                    manual_approval_required=revision.manual_approval_required,
                    payload_json=payload["payload"],
                    guardrails_json=payload["guardrails"],
                    description=revision.description,
                    expected_behavior_json=revision.expected_behavior,
                    created_by=revision.created_by,
                    created_reason=revision.created_reason,
                    source_recommendation_id=revision.source_recommendation_id,
                    created_at=revision.created_at,
                    updated_at=revision.updated_at,
                )
                session.add(row)
            else:
                row.profile_family = revision.profile_family
                row.profile_id = revision.profile_id
                row.profile_label = revision.profile_label
                row.version = revision.version
                row.status = revision.status
                row.risk_level = revision.risk_level
                row.market_intent = revision.market_intent
                row.product_type = revision.product_type
                row.margin_mode = revision.margin_mode
                row.allowed_symbols_json = list(revision.allowed_symbols)
                row.hot_safe_only = revision.hot_safe_only
                row.auto_switch_allowed = revision.auto_switch_allowed
                row.manual_approval_required = revision.manual_approval_required
                row.payload_json = payload["payload"]
                row.guardrails_json = payload["guardrails"]
                row.description = revision.description
                row.expected_behavior_json = revision.expected_behavior
                row.created_by = revision.created_by
                row.created_reason = revision.created_reason
                row.source_recommendation_id = revision.source_recommendation_id
                row.updated_at = revision.updated_at
            session.commit()
        return revision

    def get_revision(self, revision_id: str) -> StrategyProfileRevision | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileRevisionModel).where(StrategyProfileRevisionModel.revision_id == revision_id)
            )
        return self._to_revision(row)

    def list_revisions(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        profile_id: str | None = None,
        status: str | None = None,
    ) -> list[StrategyProfileRevision]:
        query = select(StrategyProfileRevisionModel)
        if product_type is not None:
            query = query.where(StrategyProfileRevisionModel.product_type == product_type)
        if margin_mode is not None:
            query = query.where(StrategyProfileRevisionModel.margin_mode == margin_mode)
        if profile_id is not None:
            query = query.where(StrategyProfileRevisionModel.profile_id == profile_id)
        if status is not None:
            query = query.where(StrategyProfileRevisionModel.status == status)
        query = query.order_by(desc(StrategyProfileRevisionModel.created_at))
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [self._to_revision(row) for row in rows if row is not None]

    def activation_state(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> StrategyProfileActivationState:
        scope_hash = strategy_profile_scope_hash(
            product_type=product_type,
            margin_mode=margin_mode,
            allowed_symbols=allowed_symbols,
        )
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileActivationModel).where(
                    StrategyProfileActivationModel.product_type == product_type,
                    StrategyProfileActivationModel.margin_mode == margin_mode,
                    StrategyProfileActivationModel.allowed_symbols_hash == scope_hash,
                )
            )
            if row is None:
                state = StrategyProfileActivationState(
                    activation_id=f"strategy_profile_activation:{scope_hash[:12]}",
                    product_type=product_type,
                    margin_mode=margin_mode,
                    allowed_symbols=allowed_symbols,
                )
                row = StrategyProfileActivationModel(
                    activation_id=state.activation_id,
                    product_type=product_type,
                    margin_mode=margin_mode,
                    allowed_symbols_hash=scope_hash,
                    payload=state.model_dump(mode="json"),
                )
                session.add(row)
                session.commit()
                return state
        return StrategyProfileActivationState.model_validate(row.payload)

    def save_activation_state(self, state: StrategyProfileActivationState) -> StrategyProfileActivationState:
        scope_hash = strategy_profile_scope_hash(
            product_type=state.product_type,
            margin_mode=state.margin_mode,
            allowed_symbols=state.allowed_symbols,
        )
        payload = state.model_dump(mode="json")
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileActivationModel).where(StrategyProfileActivationModel.activation_id == state.activation_id)
            )
            if row is None:
                row = StrategyProfileActivationModel(
                    activation_id=state.activation_id,
                    product_type=state.product_type,
                    margin_mode=state.margin_mode,
                    allowed_symbols_hash=scope_hash,
                    payload=payload,
                )
                session.add(row)
            else:
                row.product_type = state.product_type
                row.margin_mode = state.margin_mode
                row.allowed_symbols_hash = scope_hash
                row.payload = payload
            session.commit()
        return state

    def save_recommendation(self, recommendation: StrategyProfileRecommendation) -> StrategyProfileRecommendation:
        payload = recommendation.model_dump(mode="json")
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileRecommendationModel).where(
                    StrategyProfileRecommendationModel.recommendation_id == recommendation.recommendation_id
                )
            )
            if row is None:
                row = StrategyProfileRecommendationModel(
                    recommendation_id=recommendation.recommendation_id,
                    product_type=recommendation.product_type,
                    margin_mode=recommendation.margin_mode,
                    allowed_symbols_json=list(recommendation.allowed_symbols),
                    decision_status=recommendation.decision_status,
                    generated_at=recommendation.generated_at,
                    expires_at=recommendation.expires_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.product_type = recommendation.product_type
                row.margin_mode = recommendation.margin_mode
                row.allowed_symbols_json = list(recommendation.allowed_symbols)
                row.decision_status = recommendation.decision_status
                row.generated_at = recommendation.generated_at
                row.expires_at = recommendation.expires_at
                row.payload = payload
            session.commit()
        return recommendation

    def get_recommendation(self, recommendation_id: str) -> StrategyProfileRecommendation | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileRecommendationModel).where(
                    StrategyProfileRecommendationModel.recommendation_id == recommendation_id
                )
            )
        return StrategyProfileRecommendation.model_validate(row.payload) if row is not None else None

    def latest_recommendation(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> StrategyProfileRecommendation | None:
        query = (
            select(StrategyProfileRecommendationModel)
            .where(
                StrategyProfileRecommendationModel.product_type == product_type,
                StrategyProfileRecommendationModel.margin_mode == margin_mode,
            )
            .order_by(desc(StrategyProfileRecommendationModel.generated_at))
        )
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        for row in rows:
            recommendation = StrategyProfileRecommendation.model_validate(row.payload)
            if recommendation.allowed_symbols == allowed_symbols:
                return recommendation
        return None

    def list_recommendations(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        decision_status: str | None = None,
    ) -> list[StrategyProfileRecommendation]:
        query = select(StrategyProfileRecommendationModel)
        if product_type is not None:
            query = query.where(StrategyProfileRecommendationModel.product_type == product_type)
        if margin_mode is not None:
            query = query.where(StrategyProfileRecommendationModel.margin_mode == margin_mode)
        if decision_status is not None:
            query = query.where(StrategyProfileRecommendationModel.decision_status == decision_status)
        query = query.order_by(desc(StrategyProfileRecommendationModel.generated_at))
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [StrategyProfileRecommendation.model_validate(row.payload) for row in rows]

    def expire_pending_recommendations(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
        now: datetime,
    ) -> int:
        query = select(StrategyProfileRecommendationModel).where(
            StrategyProfileRecommendationModel.product_type == product_type,
            StrategyProfileRecommendationModel.margin_mode == margin_mode,
            StrategyProfileRecommendationModel.decision_status == "pending",
            StrategyProfileRecommendationModel.expires_at <= now,
        )
        expired = 0
        with self.session_factory() as session:
            rows = session.scalars(query).all()
            for row in rows:
                recommendation = StrategyProfileRecommendation.model_validate(row.payload)
                if recommendation.allowed_symbols != allowed_symbols:
                    continue
                payload = recommendation.model_copy(
                    update={
                        "decision_status": "expired",
                        "decision_reason_code": "recommendation_expired",
                        "decision_reason_detail": "pending recommendation expired before next freshness evaluation",
                    }
                ).model_dump(mode="json")
                row.decision_status = "expired"
                row.payload = payload
                expired += 1
            if expired:
                session.commit()
        return expired

    def save_activation_record(self, record: StrategyProfileActivationRecord) -> StrategyProfileActivationRecord:
        payload = record.model_dump(mode="json")
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileActivationHistoryModel).where(
                    StrategyProfileActivationHistoryModel.activation_event_id == record.activation_event_id
                )
            )
            if row is None:
                row = StrategyProfileActivationHistoryModel(
                    activation_event_id=record.activation_event_id,
                    product_type=record.product_type,
                    margin_mode=record.margin_mode,
                    executed_at=record.executed_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.product_type = record.product_type
                row.margin_mode = record.margin_mode
                row.executed_at = record.executed_at
                row.payload = payload
            session.commit()
        return record

    def list_activation_history(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileActivationRecord]:
        query = select(StrategyProfileActivationHistoryModel)
        if product_type is not None:
            query = query.where(StrategyProfileActivationHistoryModel.product_type == product_type)
        if margin_mode is not None:
            query = query.where(StrategyProfileActivationHistoryModel.margin_mode == margin_mode)
        query = query.order_by(desc(StrategyProfileActivationHistoryModel.executed_at))
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [StrategyProfileActivationRecord.model_validate(row.payload) for row in rows]

    def save_rejection(self, record: StrategyProfileRejectionRecord) -> StrategyProfileRejectionRecord:
        payload = record.model_dump(mode="json")
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileRejectionModel).where(
                    StrategyProfileRejectionModel.rejection_id == record.rejection_id
                )
            )
            if row is None:
                row = StrategyProfileRejectionModel(
                    rejection_id=record.rejection_id,
                    product_type=record.product_type,
                    margin_mode=record.margin_mode,
                    created_at=record.created_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.product_type = record.product_type
                row.margin_mode = record.margin_mode
                row.created_at = record.created_at
                row.payload = payload
            session.commit()
        return record

    def list_rejections(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileRejectionRecord]:
        query = select(StrategyProfileRejectionModel)
        if product_type is not None:
            query = query.where(StrategyProfileRejectionModel.product_type == product_type)
        if margin_mode is not None:
            query = query.where(StrategyProfileRejectionModel.margin_mode == margin_mode)
        query = query.order_by(desc(StrategyProfileRejectionModel.created_at))
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [StrategyProfileRejectionRecord.model_validate(row.payload) for row in rows]

    def save_evaluation(self, record: StrategyProfileEvaluationRecord) -> StrategyProfileEvaluationRecord:
        payload = record.model_dump(mode="json")
        with self.session_factory() as session:
            row = session.scalar(
                select(StrategyProfileEvaluationModel).where(
                    StrategyProfileEvaluationModel.evaluation_id == record.evaluation_id
                )
            )
            if row is None:
                row = StrategyProfileEvaluationModel(
                    evaluation_id=record.evaluation_id,
                    product_type=record.product_type,
                    margin_mode=record.margin_mode,
                    created_at=record.created_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.product_type = record.product_type
                row.margin_mode = record.margin_mode
                row.created_at = record.created_at
                row.payload = payload
            session.commit()
        return record

    def list_evaluations(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileEvaluationRecord]:
        query = select(StrategyProfileEvaluationModel)
        if product_type is not None:
            query = query.where(StrategyProfileEvaluationModel.product_type == product_type)
        if margin_mode is not None:
            query = query.where(StrategyProfileEvaluationModel.margin_mode == margin_mode)
        query = query.order_by(desc(StrategyProfileEvaluationModel.created_at))
        with self.session_factory() as session:
            rows = session.scalars(query).all()
        return [StrategyProfileEvaluationRecord.model_validate(row.payload) for row in rows]

    @staticmethod
    def _to_revision(row: StrategyProfileRevisionModel | None) -> StrategyProfileRevision | None:
        if row is None:
            return None
        return StrategyProfileRevision.model_validate(
            {
                "revision_id": row.revision_id,
                "profile_family": row.profile_family,
                "profile_id": row.profile_id,
                "profile_label": row.profile_label,
                "version": row.version,
                "status": row.status,
                "risk_level": row.risk_level,
                "market_intent": row.market_intent,
                "product_type": row.product_type,
                "margin_mode": row.margin_mode,
                "allowed_symbols": row.allowed_symbols_json,
                "hot_safe_only": row.hot_safe_only,
                "auto_switch_allowed": row.auto_switch_allowed,
                "manual_approval_required": row.manual_approval_required,
                "payload": row.payload_json,
                "guardrails": row.guardrails_json,
                "description": row.description,
                "expected_behavior": row.expected_behavior_json,
                "created_by": row.created_by,
                "created_reason": row.created_reason,
                "source_recommendation_id": row.source_recommendation_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
