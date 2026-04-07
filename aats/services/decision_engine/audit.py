from __future__ import annotations

import asyncio

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope, publish_model
from aats.schemas.audit import DecisionAuditRecord
from aats.storage.base import AuditRepository


class DecisionAuditService:
    def __init__(self, *, bus: EventBus, audit_repo: AuditRepository) -> None:
        self.bus = bus
        self.audit_repo = audit_repo
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

    async def handle_ai_decision_brief(self, message: dict) -> None:
        await self._update_decision_record(
            message=message,
            ref_field="ai_decision_brief_ref",
        )

    async def handle_ai_shadow_decision(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id in record.ai_shadow_decision_refs:
            return
        updated = record.model_copy(
            update={"ai_shadow_decision_refs": [*record.ai_shadow_decision_refs, envelope.event_id]}
        )
        await self._publish_record(updated)

    async def handle_ai_shadow_evaluation(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_ids = envelope.payload.get("decision_ids")
        if not isinstance(decision_ids, list):
            return
        for decision_id in decision_ids:
            if not isinstance(decision_id, str):
                continue
            record = await self._fetch_existing_record(decision_id)
            if envelope.event_id in record.ai_shadow_evaluation_refs:
                continue
            updated = record.model_copy(
                update={"ai_shadow_evaluation_refs": [*record.ai_shadow_evaluation_refs, envelope.event_id]}
            )
            await self._publish_record(updated)

    async def handle_strategy_coordinator_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={"strategy_coordinator_snapshot_ref": envelope.event_id}
        )
        await self._publish_record(updated)

    async def handle_strategy_sleeve_intent(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id in record.strategy_sleeve_intent_refs:
            return
        updated = record.model_copy(
            update={"strategy_sleeve_intent_refs": [*record.strategy_sleeve_intent_refs, envelope.event_id]}
        )
        await self._publish_record(updated)

    async def handle_portfolio_allocation_decision(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={
                "portfolio_allocation_decision_ref": envelope.event_id,
                "selected_strategy_sleeve_id": envelope.payload.get("primary_strategy_sleeve_id")
                or record.selected_strategy_sleeve_id,
                "allocation_id": envelope.payload.get("allocation_id") or record.allocation_id,
            }
        )
        await self._publish_record(updated)

    async def handle_position_target(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={
                "position_target_ref": envelope.event_id,
                "selected_strategy_sleeve_id": envelope.payload.get("strategy_sleeve_id") or record.selected_strategy_sleeve_id,
                "allocation_id": envelope.payload.get("allocation_id") or record.allocation_id,
            }
        )
        await self._publish_record(updated)

    async def handle_decision_outcome(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={
                "decision_outcome_ref": envelope.event_id,
                "selected_strategy_sleeve_id": envelope.payload.get("selected_strategy_sleeve_id") or record.selected_strategy_sleeve_id,
                "allocation_id": envelope.payload.get("allocation_id") or record.allocation_id,
            }
        )
        await self._publish_record(updated)

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

    async def handle_execution_plan(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updates: dict[str, object] = {"execution_plan_ref": envelope.event_id}
        if envelope.event_id not in record.execution_plan_refs:
            updates["execution_plan_refs"] = [*record.execution_plan_refs, envelope.event_id]
        await self._publish_record(record.model_copy(update=updates))

    async def handle_strategy_execution_bundle(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(
            update={
                "strategy_execution_bundle_ref": envelope.event_id,
                "selected_strategy_sleeve_id": envelope.payload.get("strategy_sleeve_id") or record.selected_strategy_sleeve_id,
                "allocation_id": envelope.payload.get("allocation_id") or record.allocation_id,
            }
        )
        await self._publish_record(updated)

    async def handle_order_intent(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id not in record.order_intent_refs:
            record = record.model_copy(
                update={"order_intent_refs": [*record.order_intent_refs, envelope.event_id]},
            )
            await self._publish_record(record)

    async def handle_order_update(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id in record.order_state_refs:
            return
        record = record.model_copy(
            update={"order_state_refs": [*record.order_state_refs, envelope.event_id]},
        )
        await self._publish_record(record)

    async def handle_fill_event(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        if envelope.event_id not in record.fill_event_refs:
            record = record.model_copy(
                update={"fill_event_refs": [*record.fill_event_refs, envelope.event_id]},
            )
            await self._publish_record(record)

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = envelope.payload.get("decision_id")
        if not isinstance(decision_id, str):
            return
        record = await self._fetch_existing_record(decision_id)
        if record.portfolio_delta_ref == envelope.event_id and envelope.event_id in record.portfolio_delta_refs:
            return
        portfolio_delta_refs = list(record.portfolio_delta_refs)
        if envelope.event_id not in portfolio_delta_refs:
            portfolio_delta_refs.append(envelope.event_id)
        updated = record.model_copy(
            update={
                "portfolio_delta_ref": envelope.event_id,
                "portfolio_delta_refs": portfolio_delta_refs,
            }
        )
        await self._publish_record(updated)

    async def handle_reconciliation_report(self, message: dict) -> None:
        envelope = parse_envelope(message)
        decision_id = envelope.payload.get("decision_id")
        if not isinstance(decision_id, str):
            return
        record = await self._fetch_existing_record(decision_id)
        report_snapshot_ref = envelope.payload.get("portfolio_snapshot_ref")
        valid_snapshot_refs = set(record.portfolio_delta_refs)
        if record.portfolio_delta_ref is not None:
            valid_snapshot_refs.add(record.portfolio_delta_ref)
        if (
            isinstance(report_snapshot_ref, str)
            and valid_snapshot_refs
            and report_snapshot_ref not in valid_snapshot_refs
        ):
            raise RuntimeError(
                "Reconciliation report snapshot reference does not match audit-linked portfolio snapshot "
                f"for decision_id={decision_id}"
            )
        if envelope.event_id in record.reconciliation_refs:
            return
        updated = record.model_copy(
            update={"reconciliation_refs": [*record.reconciliation_refs, envelope.event_id]},
        )
        await self._publish_record(updated)

    async def _fetch_existing_record(self, decision_id: str) -> DecisionAuditRecord:
        # _existing_record 既要 DB get 又可能写一次 synthetic seed，原实现直接在
        # async handler 里同步调用，每条审计事件都会堵住 event loop 至少一次
        # DB 往返。把整个查-或-补操作作为一个原子单元丢到线程池。
        return await asyncio.to_thread(self._existing_record, decision_id)

    def _existing_record(self, decision_id: str) -> DecisionAuditRecord:
        record = self.audit_repo.get(decision_id)
        if record is None:
            record = DecisionAuditRecord(
                decision_id=decision_id,
                decision_context_ref=f"synthetic_execution_seed:{decision_id}",
            )
            self.audit_repo.upsert(record)
            log_event(
                self.logger,
                "decision_audit_synthetic_seeded",
                level="warning",
                **correlation_fields(
                    decision_id=decision_id,
                    reason="missing_audit_record_seeded_from_execution_flow",
                ),
            )
        return record

    async def _update_decision_record(self, *, message: dict, ref_field: str) -> None:
        envelope = parse_envelope(message)
        decision_id = str(envelope.payload["decision_id"])
        record = await self._fetch_existing_record(decision_id)
        updated = record.model_copy(update={ref_field: envelope.event_id})
        await self._publish_record(updated)

    async def _publish_record(self, record: DecisionAuditRecord) -> None:
        # audit_repo.upsert 是同步写 + commit，在 event loop 线程上每条事件都
        # 会阻塞至少一次 DB 往返。审计事件量非常大（几乎每个决策阶段都要落
        # 一条），改用 to_thread 让主协程专心调度，不要被 audit 侧拖慢。
        await asyncio.to_thread(self.audit_repo.upsert, record)
        log_event(
            self.logger,
            "decision_audit_updated",
            level="debug",
            **correlation_fields(
                decision_id=record.decision_id,
                execution_plan_ref=record.execution_plan_ref,
                execution_plan_ref_count=len(record.execution_plan_refs),
                strategy_execution_bundle_ref=record.strategy_execution_bundle_ref,
                strategy_coordinator_snapshot_ref=record.strategy_coordinator_snapshot_ref,
                strategy_sleeve_intent_ref_count=len(record.strategy_sleeve_intent_refs),
                portfolio_allocation_decision_ref=record.portfolio_allocation_decision_ref,
                portfolio_delta_ref_count=len(record.portfolio_delta_refs),
                order_intent_ref_count=len(record.order_intent_refs),
                order_state_ref_count=len(record.order_state_refs),
                fill_event_ref_count=len(record.fill_event_refs),
                ai_shadow_decision_ref_count=len(record.ai_shadow_decision_refs),
                ai_shadow_evaluation_ref_count=len(record.ai_shadow_evaluation_refs),
                reconciliation_ref_count=len(record.reconciliation_refs),
            ),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.AUDIT_RECORDS,
            key=record.decision_id,
            payload_model=record,
            source_component="audit_service",
        )
