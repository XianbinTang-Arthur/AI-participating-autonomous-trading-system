from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aats.schemas.common import EventEnvelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.operator import OperatorUserRecord
from aats.schemas.runtime_profiles import RuntimeProfileActivationState, RuntimeProfileRevision
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


class RuntimeProfileRepository(Protocol):
    def save_revision(self, revision: RuntimeProfileRevision) -> RuntimeProfileRevision:
        ...

    def get_revision(self, revision_id: str) -> RuntimeProfileRevision | None:
        ...

    def list_revisions(self) -> list[RuntimeProfileRevision]:
        ...

    def activation_state(self) -> RuntimeProfileActivationState:
        ...

    def save_activation_state(self, state: RuntimeProfileActivationState) -> RuntimeProfileActivationState:
        ...
