from __future__ import annotations

import unittest

from aats.bus.memory_bus import InMemoryEventBus
from aats.services.decision_engine.audit import DecisionAuditService
from aats.storage.audit_repo import InMemoryAuditRepository
from aats.storage.event_store import InMemoryEventStore


class TestDecisionAuditService(unittest.TestCase):
    def test_missing_audit_record_is_seeded_for_execution_only_flows(self) -> None:
        audit_repo = InMemoryAuditRepository()
        service = DecisionAuditService(
            bus=InMemoryEventBus(event_store=InMemoryEventStore(), persistence_mode="strict"),
            audit_repo=audit_repo,
        )

        record = service._existing_record("decision_execution_only")

        self.assertEqual(record.decision_id, "decision_execution_only")
        self.assertEqual(
            record.decision_context_ref,
            "synthetic_execution_seed:decision_execution_only",
        )
        self.assertIsNotNone(audit_repo.get("decision_execution_only"))
