from __future__ import annotations

from threading import Lock
from time import monotonic

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.common import utc_now
from aats.storage.sqlalchemy_models import DecisionAuditRecordModel


_RECENT_DECISION_CANDIDATE_MULTIPLIER = 32
_RECENT_DECISION_MIN_CANDIDATES = 512
_RECENT_DECISION_MAX_CANDIDATES = 100_000
_COUNT_CACHE_TTL_SECONDS = 30.0


class PostgresAuditRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self._count_cache_lock = Lock()
        self._count_cache_value: int | None = None
        self._count_cache_expires_at = 0.0

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
        self._invalidate_count_cache()

    def upsert_batch(self, records: list[DecisionAuditRecord]) -> None:
        """P2-1：批量写入多条 audit records，单 session / 单 commit。"""
        if not records:
            return
        now = utc_now()
        with self.session_factory() as session:
            for record in records:
                session.add(
                    DecisionAuditRecordModel(
                        decision_id=record.decision_id,
                        updated_at=now,
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
        self._invalidate_count_cache()

    def get(self, decision_id: str) -> DecisionAuditRecord | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(DecisionAuditRecordModel)
                .where(DecisionAuditRecordModel.decision_id == decision_id)
                .order_by(desc(DecisionAuditRecordModel.audit_revision_id))
                .limit(1)
            )
        return DecisionAuditRecord.model_validate(row.payload) if row is not None else None

    def get_many_latest(self, decision_ids: list[str]) -> list[DecisionAuditRecord]:
        unique_ids = sorted({str(decision_id).strip() for decision_id in decision_ids if str(decision_id).strip()})
        if not unique_ids:
            return []
        with self.session_factory() as session:
            latest_revision = (
                select(
                    DecisionAuditRecordModel.decision_id,
                    func.max(DecisionAuditRecordModel.audit_revision_id).label("max_revision"),
                )
                .where(DecisionAuditRecordModel.decision_id.in_(unique_ids))
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

    def latest(self) -> DecisionAuditRecord | None:
        rows = self.recent(limit=1)
        return rows[0] if rows else None

    def recent(self, *, limit: int) -> list[DecisionAuditRecord]:
        normalized_limit = max(int(limit), 1)
        candidate_limit = min(
            max(
                normalized_limit * _RECENT_DECISION_CANDIDATE_MULTIPLIER,
                _RECENT_DECISION_MIN_CANDIDATES,
            ),
            _RECENT_DECISION_MAX_CANDIDATES,
        )
        created_at_expr = DecisionAuditRecordModel.payload["created_at"].as_string()
        with self.session_factory() as session:
            while True:
                rows = session.scalars(
                    select(DecisionAuditRecordModel)
                    .order_by(
                        desc(created_at_expr).nulls_last(),
                        desc(DecisionAuditRecordModel.audit_revision_id),
                    )
                    .limit(candidate_limit)
                ).all()

                records: list[DecisionAuditRecord] = []
                seen_decision_ids: set[str] = set()
                for row in rows:
                    if row.decision_id in seen_decision_ids:
                        continue
                    seen_decision_ids.add(row.decision_id)
                    records.append(DecisionAuditRecord.model_validate(row.payload))
                    if len(records) >= normalized_limit:
                        return records

                if len(rows) < candidate_limit or candidate_limit >= _RECENT_DECISION_MAX_CANDIDATES:
                    return records
                candidate_limit = min(candidate_limit * 2, _RECENT_DECISION_MAX_CANDIDATES)

    def _invalidate_count_cache(self) -> None:
        with self._count_cache_lock:
            self._count_cache_value = None
            self._count_cache_expires_at = 0.0

    def _count_distinct_decisions(self) -> int:
        with self.session_factory() as session:
            decisions = (
                select(DecisionAuditRecordModel.decision_id)
                .group_by(DecisionAuditRecordModel.decision_id)
                .subquery()
            )
            count = session.scalar(select(func.count()).select_from(decisions))
        return int(count or 0)

    def count(self) -> int:
        now = monotonic()
        with self._count_cache_lock:
            if self._count_cache_value is not None and now < self._count_cache_expires_at:
                return self._count_cache_value
            count = self._count_distinct_decisions()
            self._count_cache_value = count
            self._count_cache_expires_at = now + _COUNT_CACHE_TTL_SECONDS
            return count

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
