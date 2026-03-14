from __future__ import annotations

from typing import Protocol

from aats.schemas.common import EventEnvelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport


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

    def by_decision(self, decision_id: str) -> list[EventEnvelope]:
        ...


class ExecutionRepository(Protocol):
    def save_order_state(self, state: OrderState) -> None:
        ...

    def has_intent(self, intent_id: str) -> bool:
        ...

    def save_fill(self, fill: FillEvent) -> bool:
        ...

    def order_states(self) -> list[OrderState]:
        ...

    def open_order_states(self) -> list[OrderState]:
        ...

    def fills(self) -> list[FillEvent]:
        ...


class PortfolioRepository(Protocol):
    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        ...

    def latest(self) -> PortfolioSnapshot | None:
        ...

    def history(self) -> list[PortfolioSnapshot]:
        ...


class ReconciliationRepository(Protocol):
    def save_report(self, report: ReconciliationReport) -> None:
        ...

    def latest(self) -> ReconciliationReport | None:
        ...

    def history(self) -> list[ReconciliationReport]:
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
