from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import sessionmaker, Session

from aats.bootstrap.logging import get_logger, log_event
from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import EventEnvelope
from aats.schemas.execution import FillEvent, OrderIntent, OrderObligation, OrderState
from aats.schemas.operator import ExecutionErrorSummary
from aats.services.execution_control.order_service import ExecutionOrderService
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.storage.event_store_postgres import PostgresEventStore
from aats.storage.base import ExecutionRepository
from aats.storage.execution_command_repo_postgres import PostgresExecutionCommandRepository
from aats.storage.execution_fill_repo_v2_postgres import PostgresExecutionFillRepositoryV2
from aats.storage.execution_order_repo_postgres import (
    PostgresExecutionOrderHistoryRepository,
    PostgresExecutionOrderRepository,
)
from aats.storage.obligation_repo_postgres import PostgresExecutionObligationRepository
from aats.storage.outbox_repo_postgres import PostgresOutboxRepository


@dataclass(slots=True)
class PostgresExecutionOutboxPublisher:
    _MAX_PUBLISH_ATTEMPTS = 3
    session_factory: sessionmaker[Session]
    event_store: PostgresEventStore
    execution_repo: ExecutionRepository
    obligation_repo: PostgresExecutionObligationRepository
    outbox_repo: PostgresOutboxRepository
    bus: InMemoryEventBus
    execution_command_repo: PostgresExecutionCommandRepository | None = None
    execution_order_repo: PostgresExecutionOrderRepository | None = None
    execution_order_history_repo: PostgresExecutionOrderHistoryRepository | None = None
    execution_fill_repo: PostgresExecutionFillRepositoryV2 | None = None
    logger: Any = field(init=False)

    def __post_init__(self) -> None:
        self.logger = get_logger("aats.execution_outbox")

    async def persist_order_state(
        self,
        *,
        order_state: OrderState,
        key: str,
        obligation: OrderObligation | None = None,
    ) -> OrderState:
        with self.session_factory() as session:
            persisted, previous = self.execution_repo.save_order_state_in_session(session, order_state)  # type: ignore[attr-defined]
            self._ensure_execution_order_row(session, order_state=persisted)
            if obligation is not None:
                self.obligation_repo.save_obligation_in_session(session, obligation)
            envelopes = [self._order_update_envelope(key=key, persisted=persisted)]
            summary = self._execution_error_summary(previous=previous, persisted=persisted)
            if summary is not None:
                envelopes.append(summary)
            for envelope in envelopes:
                self.event_store.append_in_session(session, envelope)
                self.outbox_repo.enqueue_in_session(session, envelope)
            session.commit()
        await self.flush_pending()
        return persisted

    async def persist_order_state_and_command(
        self,
        *,
        order_state: OrderState,
        key: str,
        command_id: str,
        command_type: str,
        command_idempotency_key: str,
        command_payload: dict[str, Any],
        command_created_at,
        obligation: OrderObligation | None = None,
    ) -> OrderState:
        if self.execution_command_repo is None:
            return await self.persist_order_state(order_state=order_state, key=key, obligation=obligation)
        with self.session_factory() as session:
            persisted, previous = self.execution_repo.save_order_state_in_session(session, order_state)  # type: ignore[attr-defined]
            self._ensure_execution_order_row(
                session,
                order_state=persisted,
                command_type=command_type,
                command_payload=command_payload,
            )
            if obligation is not None:
                self.obligation_repo.save_obligation_in_session(session, obligation)
            envelopes = [self._order_update_envelope(key=key, persisted=persisted)]
            summary = self._execution_error_summary(previous=previous, persisted=persisted)
            if summary is not None:
                envelopes.append(summary)
            for envelope in envelopes:
                self.event_store.append_in_session(session, envelope)
                self.outbox_repo.enqueue_in_session(session, envelope)
            self.execution_command_repo.enqueue_command_in_session(
                session,
                command_id=command_id,
                order_id=persisted.client_order_id,
                command_type=command_type,
                idempotency_key=command_idempotency_key,
                payload=command_payload,
                created_at=command_created_at,
            )
            session.commit()
        await self.flush_pending()
        return persisted

    def _ensure_execution_order_row(
        self,
        session: Session,
        *,
        order_state: OrderState,
        command_type: str | None = None,
        command_payload: dict[str, Any] | None = None,
    ) -> None:
        if self.execution_order_repo is None:
            return
        existing = self.execution_order_repo.get_order_by_client_order_id_in_session(
            session,
            order_state.client_order_id,
            for_update=True,
        )
        if existing is not None:
            return
        intent = self._seed_intent_from_command_payload(command_type=command_type, command_payload=command_payload)
        if intent is None:
            intent = ExecutionOrderService._intent_from_order_state(order_state)
        created_at = order_state.created_at
        self.execution_order_repo.create_order_in_session(
            session,
            order_id=order_state.client_order_id,
            intent=intent,
            initial_state=order_state.status,
            created_at=created_at,
            raw_payload={
                "client_order_id": order_state.client_order_id,
                "venue_order_id": order_state.exchange_order_id,
                "source_system": order_state.submission_mode or "execution_outbox_publisher",
                "intent": intent.model_dump(mode="python"),
                "order_state": order_state.model_dump(mode="python"),
            },
        )
        if self.execution_order_history_repo is not None:
            self.execution_order_history_repo.append_transition_in_session(
                session,
                order_id=order_state.client_order_id,
                from_state=None,
                to_state=order_state.status,
                reason_code="execution_outbox_seed",
                source="execution_outbox",
                source_message_id=order_state.intent_id,
                payload=order_state.model_dump(mode="python"),
                created_at=created_at,
            )

    def _ensure_execution_fill_row(
        self,
        session: Session,
        *,
        fill: FillEvent,
    ) -> None:
        if self.execution_fill_repo is None:
            return
        order_id = fill.client_order_id
        if self.execution_order_repo is not None:
            existing = self.execution_order_repo.get_order_by_client_order_id_in_session(
                session,
                fill.client_order_id,
                for_update=True,
            )
            if existing is None:
                intent = Phase1ExecutionShadowService.intent_from_fill(fill)
                self.execution_order_repo.create_order_in_session(
                    session,
                    order_id=fill.client_order_id,
                    intent=intent,
                    initial_state=fill.order_status_after_fill or "FILLED",
                    created_at=fill.created_at,
                    raw_payload={
                        "client_order_id": fill.client_order_id,
                        "venue_order_id": fill.exchange_order_id,
                        "source_system": "execution_outbox_fill_backfill",
                        "fill_event": fill.model_dump(mode="python"),
                    },
                )
            else:
                order_id = str(existing["order_id"])
        self.execution_fill_repo.save_fill_in_session(
            session,
            fill=fill,
            order_id=order_id,
            source=fill.venue.lower(),
            raw_payload={
                "venue_fill_id": fill.fill_id,
                "fill_event": fill.model_dump(mode="python"),
            },
        )

    async def persist_fill(
        self,
        *,
        fill: FillEvent,
        obligation: OrderObligation | None = None,
    ) -> bool:
        with self.session_factory() as session:
            saved = self.execution_repo.save_fill_in_session(session, fill)  # type: ignore[attr-defined]
            if not saved:
                session.rollback()
                return False
            self._ensure_execution_fill_row(session, fill=fill)
            if obligation is not None:
                self.obligation_repo.save_obligation_in_session(session, obligation)
            envelope = build_envelope(
                topic=topics.FILL_EVENTS,
                key=fill.symbol,
                payload_model=fill,
                source_component="execution_engine",
            )
            self.event_store.append_in_session(session, envelope)
            self.outbox_repo.enqueue_in_session(session, envelope)
            session.commit()
        await self.flush_pending()
        return True

    async def flush_pending(self, *, limit: int = 100) -> None:
        pending = self.outbox_repo.pending(limit=limit)
        for envelope in pending:
            try:
                await self.bus.publish_envelope(envelope, persist=False)
                self.outbox_repo.mark_published(envelope.event_id)
            except Exception as exc:
                status = self.outbox_repo.record_failure_with_threshold(
                    envelope.event_id,
                    str(exc),
                    max_attempts=self._MAX_PUBLISH_ATTEMPTS,
                )
                log_event(
                    self.logger,
                    "execution_outbox_publish_failed",
                    level="error",
                    topic=envelope.topic,
                    key=envelope.key,
                    event_id=envelope.event_id,
                    outbox_status=status,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if status != "FAILED":
                    break  # FIFO: retriable failure blocks subsequent messages until resolved

    @staticmethod
    def _seed_intent_from_command_payload(
        *,
        command_type: str | None,
        command_payload: dict[str, Any] | None,
    ) -> OrderIntent | None:
        if str(command_type or "").lower() != "submit":
            return None
        payload = dict(command_payload or {})
        intent_payload = payload.get("intent")
        if not isinstance(intent_payload, dict):
            return None
        try:
            return OrderIntent.model_validate(intent_payload)
        except Exception:
            return None

    @staticmethod
    def _order_update_envelope(*, key: str, persisted: OrderState) -> EventEnvelope:
        return build_envelope(
            topic=topics.ORDER_UPDATES,
            key=key,
            payload_model=persisted,
            source_component="execution_engine",
        )

    @staticmethod
    def _execution_error_summary(
        *,
        previous: OrderState | None,
        persisted: OrderState,
    ) -> EventEnvelope | None:
        if persisted.status not in {"FAILED", "REJECTED", "BLOCKED"}:
            return None
        if previous is not None and previous.status == persisted.status and previous.execution_error == persisted.execution_error:
            return None
        summary = ExecutionErrorSummary(
            subsystem="execution_engine",
            severity="error" if persisted.status == "FAILED" else "warning",
            message=persisted.execution_error or persisted.cancel_reason or persisted.status,
            decision_id=persisted.decision_id,
            intent_id=persisted.intent_id,
            order_id=persisted.client_order_id,
            status=persisted.status,
            observed_at=persisted.last_update_ts or persisted.created_at,
        )
        return build_envelope(
            topic=topics.EXECUTION_ERROR_SUMMARIES,
            key=persisted.symbol,
            payload_model=summary,
            source_component="execution_engine",
        )
