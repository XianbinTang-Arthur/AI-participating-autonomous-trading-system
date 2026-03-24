from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aats.schemas.common import EventEnvelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.execution import FillEvent, OrderObligation, OrderState
from aats.schemas.portfolio import FillOutcomeRecord, FundingFeeRecord, PortfolioSnapshot, SleevePnLRecord
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.operator import OperatorUserRecord
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    StrategyExecutionBundle,
    StrategySleeveIntent,
    StrategySleeveRecord,
)
from aats.schemas.strategy_profiles import (
    StrategyProfileActivationRecord,
    StrategyProfileActivationState,
    StrategyProfileEvaluationRecord,
    StrategyProfileRecommendation,
    StrategyProfileRejectionRecord,
    StrategyProfileRevision,
)
from aats.services.runtime_scope import RuntimeStateScope


class EventStore(Protocol):
    def append(self, envelope: EventEnvelope) -> None:
        ...

    def all(self) -> list[EventEnvelope]:
        ...

    def count(self, *, topic: str | None = None, decision_id: str | None = None) -> int:
        ...

    def get(self, event_id: str) -> EventEnvelope | None:
        ...

    def latest(self, topic: str, key: str | None = None) -> EventEnvelope | None:
        ...

    def by_topic(self, topic: str) -> list[EventEnvelope]:
        ...

    def recent_by_topic(self, topic: str, *, limit: int) -> list[EventEnvelope]:
        ...

    def by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        ...

    def latest_by_topic_scoped(
        self,
        topic: str,
        *,
        scope: RuntimeStateScope,
        key: str | None = None,
    ) -> EventEnvelope | None:
        ...

    def by_decision(self, decision_id: str) -> list[EventEnvelope]:
        ...

    def between(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        topic: str | None = None,
        decision_id: str | None = None,
    ) -> list[EventEnvelope]:
        ...


class ExecutionRepository(Protocol):
    def save_order_state(self, state: OrderState) -> OrderState:
        ...

    def has_intent(self, intent_id: str) -> bool:
        ...

    def save_fill(self, fill: FillEvent) -> bool:
        ...

    def order_states(self) -> list[OrderState]:
        ...

    def get_order_state(self, client_order_id: str) -> OrderState | None:
        ...

    def recent_order_states(
        self,
        *,
        limit: int = 20,
        statuses: tuple[str, ...] | None = None,
    ) -> list[OrderState]:
        ...

    def open_order_states(self) -> list[OrderState]:
        ...

    def fills(self) -> list[FillEvent]:
        ...

    def fills_since(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        ...

    def fills_for_order(self, client_order_id: str) -> list[FillEvent]:
        ...

    def order_states_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        statuses: tuple[str, ...] | None = None,
        limit: int | None = None,
        open_only: bool = False,
    ) -> list[OrderState]:
        ...

    def fills_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillEvent]:
        ...


class ExecutionObligationRepository(Protocol):
    def save_obligation(self, obligation: OrderObligation) -> OrderObligation:
        ...

    def get_obligation(self, client_order_id: str) -> OrderObligation | None:
        ...

    def active_obligations(self) -> list[OrderObligation]:
        ...

    def all_obligations(self) -> list[OrderObligation]:
        ...


class PortfolioRepository(Protocol):
    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        ...

    def latest(self) -> PortfolioSnapshot | None:
        ...

    def history(self) -> list[PortfolioSnapshot]:
        ...

    def recent_history(self, *, limit: int) -> list[PortfolioSnapshot]:
        ...

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[PortfolioSnapshot]:
        ...

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
        ...


class FillOutcomeRepository(Protocol):
    def save_outcome(self, outcome: FillOutcomeRecord) -> FillOutcomeRecord:
        ...

    def get_outcome(self, fill_id: str) -> FillOutcomeRecord | None:
        ...

    def outcomes(self) -> list[FillOutcomeRecord]:
        ...

    def outcomes_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FillOutcomeRecord]:
        ...


class FundingFeeRepository(Protocol):
    def save_record(self, record: FundingFeeRecord) -> FundingFeeRecord:
        ...

    def get_record(self, bill_id: str) -> FundingFeeRecord | None:
        ...

    def records(self) -> list[FundingFeeRecord]:
        ...

    def records_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FundingFeeRecord]:
        ...


class SleevePnLRepository(Protocol):
    def save_record(self, record: SleevePnLRecord) -> SleevePnLRecord:
        ...

    def get_record(self, record_id: str) -> SleevePnLRecord | None:
        ...

    def records(self) -> list[SleevePnLRecord]:
        ...

    def records_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[SleevePnLRecord]:
        ...

    def replace_scope(
        self,
        *,
        scope: RuntimeStateScope,
        records: list[SleevePnLRecord],
    ) -> None:
        ...


class ReconciliationRepository(Protocol):
    def save_report(self, report: ReconciliationReport) -> None:
        ...

    def latest(self) -> ReconciliationReport | None:
        ...

    def history(self) -> list[ReconciliationReport]:
        ...

    def recent_history(self, *, limit: int) -> list[ReconciliationReport]:
        ...

    def history_for_scope(
        self,
        *,
        scope: RuntimeStateScope,
        limit: int | None = None,
    ) -> list[ReconciliationReport]:
        ...

    def latest_for_scope(self, *, scope: RuntimeStateScope) -> ReconciliationReport | None:
        ...


class AuditRepository(Protocol):
    def upsert(self, record: DecisionAuditRecord) -> None:
        ...

    def get(self, decision_id: str) -> DecisionAuditRecord | None:
        ...

    def latest(self) -> DecisionAuditRecord | None:
        ...

    def recent(self, *, limit: int) -> list[DecisionAuditRecord]:
        ...

    def all(self) -> list[DecisionAuditRecord]:
        ...

    def history(self, decision_id: str) -> list[DecisionAuditRecord]:
        ...

    def count(self) -> int:
        ...


class OperatorUserRepository(Protocol):
    def save_user(self, user: OperatorUserRecord) -> OperatorUserRecord:
        ...

    def get_by_username(self, username: str) -> OperatorUserRecord | None:
        ...

    def all_users(self) -> list[OperatorUserRecord]:
        ...

    def count(self, *, enabled_only: bool = False) -> int:
        ...

    def record_login(self, username: str, logged_in_at: datetime) -> None:
        ...

    def delete_user(self, username: str) -> bool:
        ...


class StrategyProfileRepository(Protocol):
    def save_revision(self, revision: StrategyProfileRevision) -> StrategyProfileRevision:
        ...

    def get_revision(self, revision_id: str) -> StrategyProfileRevision | None:
        ...

    def list_revisions(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        profile_id: str | None = None,
        status: str | None = None,
    ) -> list[StrategyProfileRevision]:
        ...

    def activation_state(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> StrategyProfileActivationState:
        ...

    def save_activation_state(self, state: StrategyProfileActivationState) -> StrategyProfileActivationState:
        ...


class StrategySleeveRepository(Protocol):
    def save_sleeve(self, sleeve: StrategySleeveRecord) -> StrategySleeveRecord:
        ...

    def get_sleeve(self, sleeve_id: str) -> StrategySleeveRecord | None:
        ...

    def list_sleeves(self) -> list[StrategySleeveRecord]:
        ...

    def save_recommendation(self, recommendation: StrategyProfileRecommendation) -> StrategyProfileRecommendation:
        ...

    def get_recommendation(self, recommendation_id: str) -> StrategyProfileRecommendation | None:
        ...

    def latest_recommendation(
        self,
        *,
        product_type: str,
        margin_mode: str,
        allowed_symbols: tuple[str, ...],
    ) -> StrategyProfileRecommendation | None:
        ...

    def list_recommendations(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        decision_status: str | None = None,
    ) -> list[StrategyProfileRecommendation]:
        ...

    def save_activation_record(self, record: StrategyProfileActivationRecord) -> StrategyProfileActivationRecord:
        ...

    def list_activation_history(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileActivationRecord]:
        ...

    def save_rejection(self, record: StrategyProfileRejectionRecord) -> StrategyProfileRejectionRecord:
        ...

    def list_rejections(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileRejectionRecord]:
        ...

    def save_evaluation(self, record: StrategyProfileEvaluationRecord) -> StrategyProfileEvaluationRecord:
        ...

    def list_evaluations(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[StrategyProfileEvaluationRecord]:
        ...


class StrategyRuntimeRepository(Protocol):
    def save_sleeve_intent(self, intent: StrategySleeveIntent) -> StrategySleeveIntent:
        ...

    def list_sleeve_intents(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategySleeveIntent]:
        ...

    def save_allocation_decision(self, decision: PortfolioAllocationDecision) -> PortfolioAllocationDecision:
        ...

    def latest_allocation_decision(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
    ) -> PortfolioAllocationDecision | None:
        ...

    def save_execution_bundle(self, bundle: StrategyExecutionBundle) -> StrategyExecutionBundle:
        ...

    def recent_execution_bundles(
        self,
        *,
        product_type: str | None = None,
        margin_mode: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[StrategyExecutionBundle]:
        ...
