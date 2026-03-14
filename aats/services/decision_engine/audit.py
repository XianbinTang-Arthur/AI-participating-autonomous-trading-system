from __future__ import annotations

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.audit import DecisionAuditRecord
from aats.storage.base import AuditRepository


class DecisionAuditService:
    def __init__(self, *, bus: EventBus, audit_repo: AuditRepository) -> None:
        self.bus = bus
        self.audit_repo = audit_repo
        self._pending_portfolio_refs: set[str] = set()
        self._pending_reconciliation_refs: set[str] = set()
        self.logger = get_logger("aats.audit")

    async def handle_decision_context(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = DecisionAuditRecord(
            decision_id=decision_id,
            decision_context_ref=envelope.event_id,
        )
        await self._publish_record(record)

    async def handle_baseline_assessment(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="baseline_assessment_ref",
        )

    async def handle_ai_assessment(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="ai_market_assessment_ref",
        )

    async def handle_position_target(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="position_target_ref",
        )

    async def handle_policy_decision(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="policy_decision_ref",
        )

    async def handle_risk_decision(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="risk_decision_ref",
        )

    async def handle_order_intent(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = self._existing_record(decision_id)
        if envelope.event_id not in record.order_intent_refs:
            record = record.model_copy(
                update={"order_intent_refs": [*record.order_intent_refs, envelope.event_id]},
            )
            await self._publish_record(record)

    async def handle_fill_event(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = self._existing_record(decision_id)
        if envelope.event_id not in record.fill_event_refs:
            record = record.model_copy(
                update={"fill_event_refs": [*record.fill_event_refs, envelope.event_id]},
            )
            self._pending_portfolio_refs.add(decision_id)
            await self._publish_record(record)

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        for decision_id in sorted(self._pending_portfolio_refs):
            record = self._existing_record(decision_id)
            updated = record.model_copy(update={"portfolio_delta_ref": envelope.event_id})
            self._pending_reconciliation_refs.add(decision_id)
            await self._publish_record(updated)
        self._pending_portfolio_refs.clear()

    async def handle_reconciliation_report(self, message: dict) -> None:
        envelope = parse_envelope(message)
        for decision_id in sorted(self._pending_reconciliation_refs):
            record = self._existing_record(decision_id)
            if envelope.event_id in record.reconciliation_refs:
                continue
            updated = record.model_copy(
                update={"reconciliation_refs": [*record.reconciliation_refs, envelope.event_id]},
            )
            await self._publish_record(updated)
        self._pending_reconciliation_refs.clear()

    def _existing_record(self, decision_id: str) -> DecisionAuditRecord:
        record = self.audit_repo.get(decision_id)
        if record is None:
            raise RuntimeError(f"Audit record missing for decision_id={decision_id}")
        return record

    async def _update_decision_record(self, *, message: dict, ref_field: str) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = self._existing_record(decision_id)
        updated = record.model_copy(update={ref_field: envelope.event_id})
        await self._publish_record(updated)

    async def _publish_record(self, record: DecisionAuditRecord) -> None:
        self.audit_repo.upsert(record)
        log_event(
            self.logger,
            "decision_audit_updated",
            decision_id=record.decision_id,
            order_intent_ref_count=len(record.order_intent_refs),
            fill_event_ref_count=len(record.fill_event_refs),
            reconciliation_ref_count=len(record.reconciliation_refs),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.AUDIT_RECORDS,
            key=record.decision_id,
            payload_model=record,
            source_component="audit_service",
        )
