from __future__ import annotations

from aats.schemas.audit import DecisionAuditRecord


class InMemoryAuditRepository:
    def __init__(self) -> None:
        self._latest_by_decision: dict[str, DecisionAuditRecord] = {}
        self._history_by_decision: dict[str, list[DecisionAuditRecord]] = {}

    def upsert(self, record: DecisionAuditRecord) -> None:
        self._latest_by_decision[record.decision_id] = record
        self._history_by_decision.setdefault(record.decision_id, []).append(record)

    def upsert_batch(self, records: list[DecisionAuditRecord]) -> None:
        for record in records:
            self.upsert(record)

    def get(self, decision_id: str) -> DecisionAuditRecord | None:
        return self._latest_by_decision.get(decision_id)

    def latest(self) -> DecisionAuditRecord | None:
        return max(self._latest_by_decision.values(), key=lambda item: item.created_at, default=None)

    def recent(self, *, limit: int) -> list[DecisionAuditRecord]:
        return sorted(
            self._latest_by_decision.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )[:limit]

    def all(self) -> list[DecisionAuditRecord]:
        return list(self._latest_by_decision.values())

    def history(self, decision_id: str) -> list[DecisionAuditRecord]:
        return list(self._history_by_decision.get(decision_id, []))

    def count(self) -> int:
        return len(self._latest_by_decision)
