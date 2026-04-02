from __future__ import annotations

from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session, sessionmaker

from aats.schemas.common import dump_payload_exact
from aats.schemas.exit_execution import ChildExitOrderRef, ExitExecutionIntent
from aats.storage.sqlalchemy_models import ExitExecutionChildRefModel, ExitExecutionIntentModel


class PostgresExitExecutionRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save_exit_execution_intent(self, intent: ExitExecutionIntent) -> ExitExecutionIntent:
        payload = dump_payload_exact(intent)
        with self.session_factory() as session:
            row = session.get(ExitExecutionIntentModel, intent.parent_intent_id)
            if row is None:
                row = ExitExecutionIntentModel(
                    parent_intent_id=intent.parent_intent_id,
                    execution_chain_id=intent.execution_chain_id,
                    symbol=intent.symbol,
                    aggregate_status=intent.aggregate_status,
                    reconciliation_state=intent.reconciliation_state,
                    target_exit_quantity=intent.target_exit_quantity,
                    aggregated_filled_quantity=intent.aggregated_filled_quantity,
                    open_child_working_quantity=intent.open_child_working_quantity,
                    open_child_unknown_quantity=intent.open_child_unknown_quantity,
                    remaining_dispatchable_quantity=intent.remaining_dispatchable_quantity,
                    remaining_unresolved_quantity=intent.remaining_unresolved_quantity,
                    operator_review_required=intent.operator_review_required,
                    cancel_requested=intent.cancel_requested,
                    created_at=intent.created_at,
                    updated_at=intent.updated_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.execution_chain_id = intent.execution_chain_id
                row.symbol = intent.symbol
                row.aggregate_status = intent.aggregate_status
                row.reconciliation_state = intent.reconciliation_state
                row.target_exit_quantity = intent.target_exit_quantity
                row.aggregated_filled_quantity = intent.aggregated_filled_quantity
                row.open_child_working_quantity = intent.open_child_working_quantity
                row.open_child_unknown_quantity = intent.open_child_unknown_quantity
                row.remaining_dispatchable_quantity = intent.remaining_dispatchable_quantity
                row.remaining_unresolved_quantity = intent.remaining_unresolved_quantity
                row.operator_review_required = intent.operator_review_required
                row.cancel_requested = intent.cancel_requested
                row.created_at = intent.created_at
                row.updated_at = intent.updated_at
                row.payload = payload
            session.commit()
        return intent

    def get_exit_execution_intent(self, parent_intent_id: str) -> ExitExecutionIntent | None:
        with self.session_factory() as session:
            row = session.get(ExitExecutionIntentModel, parent_intent_id)
        return None if row is None else self._to_parent(row)

    def get_exit_execution_intent_by_execution_chain(
        self,
        execution_chain_id: str,
    ) -> ExitExecutionIntent | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ExitExecutionIntentModel)
                .where(ExitExecutionIntentModel.execution_chain_id == execution_chain_id)
                .limit(1)
            )
        return None if row is None else self._to_parent(row)

    def list_exit_execution_intents(self) -> list[ExitExecutionIntent]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ExitExecutionIntentModel).order_by(
                    desc(ExitExecutionIntentModel.updated_at),
                    desc(ExitExecutionIntentModel.parent_intent_id),
                )
            ).all()
        return [self._to_parent(row) for row in rows]

    def save_child_exit_order_ref(self, child_ref: ChildExitOrderRef) -> ChildExitOrderRef:
        payload = dump_payload_exact(child_ref)
        with self.session_factory() as session:
            row = session.get(ExitExecutionChildRefModel, child_ref.client_order_id)
            if row is None:
                row = ExitExecutionChildRefModel(
                    client_order_id=child_ref.client_order_id,
                    parent_intent_id=child_ref.parent_intent_id,
                    child_order_id=child_ref.child_order_id,
                    exchange_order_id=child_ref.exchange_order_id,
                    execution_chain_id=child_ref.execution_chain_id,
                    intent_id=child_ref.intent_id,
                    symbol=child_ref.symbol,
                    planned_quantity=child_ref.planned_quantity,
                    known_filled_quantity=child_ref.known_filled_quantity,
                    remaining_quantity_estimate=child_ref.remaining_quantity_estimate,
                    child_status=child_ref.child_status,
                    aggregate_category=child_ref.aggregate_category,
                    exchange_truth_pending=child_ref.exchange_truth_pending,
                    operator_review_required=child_ref.operator_review_required,
                    risk_reducing_invariant=child_ref.risk_reducing_invariant,
                    created_at=child_ref.created_at,
                    updated_at=child_ref.updated_at,
                    payload=payload,
                )
                session.add(row)
            else:
                row.parent_intent_id = child_ref.parent_intent_id
                row.child_order_id = child_ref.child_order_id
                row.exchange_order_id = child_ref.exchange_order_id
                row.execution_chain_id = child_ref.execution_chain_id
                row.intent_id = child_ref.intent_id
                row.symbol = child_ref.symbol
                row.planned_quantity = child_ref.planned_quantity
                row.known_filled_quantity = child_ref.known_filled_quantity
                row.remaining_quantity_estimate = child_ref.remaining_quantity_estimate
                row.child_status = child_ref.child_status
                row.aggregate_category = child_ref.aggregate_category
                row.exchange_truth_pending = child_ref.exchange_truth_pending
                row.operator_review_required = child_ref.operator_review_required
                row.risk_reducing_invariant = child_ref.risk_reducing_invariant
                row.created_at = child_ref.created_at
                row.updated_at = child_ref.updated_at
                row.payload = payload
            session.commit()
        return child_ref

    def child_refs_for_parent(self, *, parent_intent_id: str) -> list[ChildExitOrderRef]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ExitExecutionChildRefModel)
                .where(ExitExecutionChildRefModel.parent_intent_id == parent_intent_id)
                .order_by(asc(ExitExecutionChildRefModel.updated_at), asc(ExitExecutionChildRefModel.client_order_id))
            ).all()
        return [self._to_child_ref(row) for row in rows]

    def parent_intent_id_for_child(self, *, client_order_id: str) -> str | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ExitExecutionChildRefModel.parent_intent_id)
                .where(ExitExecutionChildRefModel.client_order_id == client_order_id)
                .limit(1)
            )
        return None if row is None else str(row)

    @staticmethod
    def _to_parent(row: ExitExecutionIntentModel) -> ExitExecutionIntent:
        payload = dict(row.payload)
        payload.setdefault("parent_intent_id", row.parent_intent_id)
        payload.setdefault("execution_chain_id", row.execution_chain_id)
        payload.setdefault("symbol", row.symbol)
        payload.setdefault("aggregate_status", row.aggregate_status)
        payload.setdefault("reconciliation_state", row.reconciliation_state)
        payload.setdefault("target_exit_quantity", row.target_exit_quantity)
        payload.setdefault("aggregated_filled_quantity", row.aggregated_filled_quantity)
        payload.setdefault("open_child_working_quantity", row.open_child_working_quantity)
        payload.setdefault("open_child_unknown_quantity", row.open_child_unknown_quantity)
        payload.setdefault("remaining_dispatchable_quantity", row.remaining_dispatchable_quantity)
        payload.setdefault("remaining_unresolved_quantity", row.remaining_unresolved_quantity)
        payload.setdefault("operator_review_required", row.operator_review_required)
        payload.setdefault("cancel_requested", row.cancel_requested)
        payload.setdefault("created_at", row.created_at)
        payload.setdefault("updated_at", row.updated_at)
        return ExitExecutionIntent.model_validate(payload)

    @staticmethod
    def _to_child_ref(row: ExitExecutionChildRefModel) -> ChildExitOrderRef:
        payload = dict(row.payload)
        payload.setdefault("client_order_id", row.client_order_id)
        payload.setdefault("parent_intent_id", row.parent_intent_id)
        payload.setdefault("child_order_id", row.child_order_id)
        payload.setdefault("exchange_order_id", row.exchange_order_id)
        payload.setdefault("execution_chain_id", row.execution_chain_id)
        payload.setdefault("intent_id", row.intent_id)
        payload.setdefault("symbol", row.symbol)
        payload.setdefault("planned_quantity", row.planned_quantity)
        payload.setdefault("known_filled_quantity", row.known_filled_quantity)
        payload.setdefault("remaining_quantity_estimate", row.remaining_quantity_estimate)
        payload.setdefault("child_status", row.child_status)
        payload.setdefault("aggregate_category", row.aggregate_category)
        payload.setdefault("exchange_truth_pending", row.exchange_truth_pending)
        payload.setdefault("operator_review_required", row.operator_review_required)
        payload.setdefault("risk_reducing_invariant", row.risk_reducing_invariant)
        payload.setdefault("created_at", row.created_at)
        payload.setdefault("updated_at", row.updated_at)
        return ChildExitOrderRef.model_validate(payload)
