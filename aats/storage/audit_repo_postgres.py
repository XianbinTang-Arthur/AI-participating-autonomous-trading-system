from __future__ import annotations

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.storage.sqlalchemy_models import DecisionAuditRecordModel


class PostgresAuditRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def upsert(self, record: DecisionAuditRecord) -> None:
        with self.session_factory() as session:
            session.add(
                DecisionAuditRecordModel(
                    decision_id=record.decision_id,
                    updated_at=utc_now(),
                    selected_strategy_sleeve_id=record.selected_strategy_sleeve_id,
                    allocation_id=record.allocation_id,
                    decision_context_ref=record.decision_context_ref,
                    strategy_coordinator_snapshot_ref=record.strategy_coordinator_snapshot_ref,
                    strategy_sleeve_intent_refs=list(record.strategy_sleeve_intent_refs),
                    portfolio_allocation_decision_ref=record.portfolio_allocation_decision_ref,
                    baseline_assessment_ref=record.baseline_assessment_ref,
                    ai_decision_brief_ref=record.ai_decision_brief_ref,
                    ai_market_assessment_ref=record.ai_market_assessment_ref,
                    ai_action_proposal_ref=record.ai_action_proposal_ref,
                    ai_shadow_decision_refs=list(record.ai_shadow_decision_refs),
                    ai_shadow_evaluation_refs=list(record.ai_shadow_evaluation_refs),
                    position_target_ref=record.position_target_ref,
                    decision_outcome_ref=record.decision_outcome_ref,
                    policy_decision_ref=record.policy_decision_ref,
                    risk_decision_ref=record.risk_decision_ref,
                    execution_plan_ref=record.execution_plan_ref,
                    execution_plan_refs=list(record.execution_plan_refs),
                    strategy_execution_bundle_ref=record.strategy_execution_bundle_ref,
                    order_intent_refs=list(record.order_intent_refs),
                    order_state_refs=list(record.order_state_refs),
                    fill_event_refs=list(record.fill_event_refs),
                    portfolio_delta_ref=record.portfolio_delta_ref,
                    reconciliation_refs=list(record.reconciliation_refs),
                    payload=record.model_dump(mode="json"),
                )
            )
            session.commit()

    def get(self, decision_id: str) -> DecisionAuditRecord | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(DecisionAuditRecordModel)
                .where(DecisionAuditRecordModel.decision_id == decision_id)
                .order_by(desc(DecisionAuditRecordModel.audit_revision_id))
                .limit(1)
            )
        return DecisionAuditRecord.model_validate(row.payload) if row is not None else None

    def latest(self) -> DecisionAuditRecord | None:
        with self.session_factory() as session:
            latest_revision = (
                select(
                    DecisionAuditRecordModel.decision_id,
                    func.max(DecisionAuditRecordModel.audit_revision_id).label("max_revision"),
                )
                .group_by(DecisionAuditRecordModel.decision_id)
                .subquery()
            )
            row = session.scalar(
                select(DecisionAuditRecordModel)
                .join(
                    latest_revision,
                    DecisionAuditRecordModel.audit_revision_id == latest_revision.c.max_revision,
                )
                .order_by(
                    desc(DecisionAuditRecordModel.updated_at),
                    desc(DecisionAuditRecordModel.audit_revision_id),
                )
                .limit(1)
            )
        return DecisionAuditRecord.model_validate(row.payload) if row is not None else None

    def recent(self, *, limit: int) -> list[DecisionAuditRecord]:
        with self.session_factory() as session:
            latest_revision = (
                select(
                    DecisionAuditRecordModel.decision_id,
                    func.max(DecisionAuditRecordModel.audit_revision_id).label("max_revision"),
                )
                .group_by(DecisionAuditRecordModel.decision_id)
                .subquery()
            )
            rows = session.scalars(
                select(DecisionAuditRecordModel)
                .join(
                    latest_revision,
                    DecisionAuditRecordModel.audit_revision_id == latest_revision.c.max_revision,
                )
                .order_by(
                    desc(DecisionAuditRecordModel.updated_at),
                    desc(DecisionAuditRecordModel.audit_revision_id),
                )
                .limit(limit)
            ).all()
        return [DecisionAuditRecord.model_validate(row.payload) for row in rows]

    def all(self) -> list[DecisionAuditRecord]:
        with self.session_factory() as session:
            latest_revision = (
                select(
                    DecisionAuditRecordModel.decision_id,
                    func.max(DecisionAuditRecordModel.audit_revision_id).label("max_revision"),
                )
                .group_by(DecisionAuditRecordModel.decision_id)
                .subquery()
            )
            rows = session.scalars(
                select(DecisionAuditRecordModel)
                .join(
                    latest_revision,
                    DecisionAuditRecordModel.audit_revision_id == latest_revision.c.max_revision,
                )
                .order_by(DecisionAuditRecordModel.decision_id)
            ).all()
        return [DecisionAuditRecord.model_validate(row.payload) for row in rows]

    def history(self, decision_id: str) -> list[DecisionAuditRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(DecisionAuditRecordModel)
                .where(DecisionAuditRecordModel.decision_id == decision_id)
                .order_by(DecisionAuditRecordModel.audit_revision_id)
            ).all()
        return [DecisionAuditRecord.model_validate(row.payload) for row in rows]

    def count(self) -> int:
        with self.session_factory() as session:
            count = session.scalar(select(func.count(func.distinct(DecisionAuditRecordModel.decision_id))))
        return int(count or 0)
