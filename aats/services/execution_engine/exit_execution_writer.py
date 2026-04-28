from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aats.bootstrap.logging import get_logger, log_event
from aats.schemas.exit_execution import ChildExitOrderRef, ExitExecutionIntent
from aats.storage.base import ExitExecutionRepository
from aats.storage.exit_execution_repo_postgres import PostgresExitExecutionRepository

ParentTransform = Callable[[ExitExecutionIntent], ExitExecutionIntent]
ParentRecompute = Callable[[ExitExecutionIntent, list[ChildExitOrderRef]], ExitExecutionIntent]
_TERMINAL_PARENT_STATUSES = {"COMPLETED", "CANCELED", "FAILED_SAFE"}


@dataclass(slots=True)
class ExitExecutionWriter:
    exit_execution_repo: ExitExecutionRepository
    logger: Any = field(init=False)

    def __post_init__(self) -> None:
        self.logger = get_logger("aats.exit_execution_writer")

    def save_exit_execution_intent(
        self,
        intent: ExitExecutionIntent,
        *,
        source_component: str,
        reason_code: str,
    ) -> ExitExecutionIntent:
        if isinstance(self.exit_execution_repo, PostgresExitExecutionRepository):
            with self.exit_execution_repo.session_factory() as session:
                current = self.exit_execution_repo.get_exit_execution_intent_in_session(
                    session,
                    intent.parent_intent_id,
                    for_update=True,
                )
                merged = self._merge_sticky_parent_fields(current=current, incoming=intent)
                saved = self.exit_execution_repo.save_exit_execution_intent_in_session(session, merged)
                session.commit()
        else:
            current = self.exit_execution_repo.get_exit_execution_intent(intent.parent_intent_id)
            merged = self._merge_sticky_parent_fields(current=current, incoming=intent)
            saved = self.exit_execution_repo.save_exit_execution_intent(merged)
        log_event(
            self.logger,
            "exit_execution_intent_persisted",
            source_component=source_component,
            reason_code=reason_code,
            parent_intent_id=saved.parent_intent_id,
            execution_chain_id=saved.execution_chain_id,
            aggregate_status=saved.aggregate_status,
            reconciliation_state=saved.reconciliation_state,
        )
        return saved

    def save_child_exit_order_ref(
        self,
        child_ref: ChildExitOrderRef,
        *,
        source_component: str,
        reason_code: str,
    ) -> ChildExitOrderRef:
        saved = self.exit_execution_repo.save_child_exit_order_ref(child_ref)
        log_event(
            self.logger,
            "exit_execution_child_ref_persisted",
            source_component=source_component,
            reason_code=reason_code,
            parent_intent_id=saved.parent_intent_id,
            client_order_id=saved.client_order_id,
            child_status=saved.child_status,
            aggregate_category=saved.aggregate_category,
        )
        return saved

    def recompute_parent(
        self,
        *,
        parent_intent_id: str,
        transform_parent: ParentTransform,
        recompute_parent: ParentRecompute,
        source_component: str,
        reason_code: str,
    ) -> ExitExecutionIntent:
        if isinstance(self.exit_execution_repo, PostgresExitExecutionRepository):
            with self.exit_execution_repo.session_factory() as session:
                current = self.exit_execution_repo.get_exit_execution_intent_in_session(
                    session,
                    parent_intent_id,
                    for_update=True,
                )
                if current is None:
                    raise KeyError(f"exit_execution_intent_not_found parent_intent_id={parent_intent_id}")
                child_refs = self.exit_execution_repo.child_refs_for_parent_in_session(
                    session,
                    parent_intent_id=parent_intent_id,
                )
                recomputed = recompute_parent(transform_parent(current), child_refs)
                saved = self.exit_execution_repo.save_exit_execution_intent_in_session(session, recomputed)
                session.commit()
        else:
            current = self.exit_execution_repo.get_exit_execution_intent(parent_intent_id)
            if current is None:
                raise KeyError(f"exit_execution_intent_not_found parent_intent_id={parent_intent_id}")
            child_refs = self.exit_execution_repo.child_refs_for_parent(parent_intent_id=parent_intent_id)
            saved = self.exit_execution_repo.save_exit_execution_intent(
                recompute_parent(transform_parent(current), child_refs)
            )
        self._log_parent_saved(saved, source_component=source_component, reason_code=reason_code)
        return saved

    def save_child_ref_and_recompute_parent(
        self,
        *,
        parent_intent: ExitExecutionIntent,
        child_ref: ChildExitOrderRef,
        recompute_parent: ParentRecompute,
        source_component: str,
        reason_code: str,
    ) -> tuple[ChildExitOrderRef, ExitExecutionIntent]:
        if isinstance(self.exit_execution_repo, PostgresExitExecutionRepository):
            with self.exit_execution_repo.session_factory() as session:
                current = self.exit_execution_repo.get_exit_execution_intent_in_session(
                    session,
                    parent_intent.parent_intent_id,
                    for_update=True,
                )
                parent_base = current or parent_intent
                if current is None:
                    self.exit_execution_repo.save_exit_execution_intent_in_session(session, parent_base)
                    session.flush()
                saved_child = self.exit_execution_repo.save_child_exit_order_ref_in_session(session, child_ref)
                child_refs = self.exit_execution_repo.child_refs_for_parent_in_session(
                    session,
                    parent_intent_id=parent_base.parent_intent_id,
                )
                recomputed = recompute_parent(parent_base, child_refs)
                saved_parent = self.exit_execution_repo.save_exit_execution_intent_in_session(session, recomputed)
                session.commit()
        else:
            existing_parent = self.exit_execution_repo.get_exit_execution_intent(parent_intent.parent_intent_id)
            parent_base = existing_parent or self.exit_execution_repo.save_exit_execution_intent(parent_intent)
            saved_child = self.exit_execution_repo.save_child_exit_order_ref(child_ref)
            child_refs = self.exit_execution_repo.child_refs_for_parent(parent_intent_id=parent_base.parent_intent_id)
            saved_parent = self.exit_execution_repo.save_exit_execution_intent(
                recompute_parent(parent_base, child_refs)
            )
        self._log_child_saved(saved_child, source_component=source_component, reason_code=f"{reason_code}:child_ref")
        self._log_parent_saved(saved_parent, source_component=source_component, reason_code=f"{reason_code}:parent")
        return saved_child, saved_parent

    def _log_parent_saved(
        self,
        saved: ExitExecutionIntent,
        *,
        source_component: str,
        reason_code: str,
    ) -> None:
        log_event(
            self.logger,
            "exit_execution_intent_persisted",
            source_component=source_component,
            reason_code=reason_code,
            parent_intent_id=saved.parent_intent_id,
            execution_chain_id=saved.execution_chain_id,
            aggregate_status=saved.aggregate_status,
            reconciliation_state=saved.reconciliation_state,
        )

    def _log_child_saved(
        self,
        saved: ChildExitOrderRef,
        *,
        source_component: str,
        reason_code: str,
    ) -> None:
        log_event(
            self.logger,
            "exit_execution_child_ref_persisted",
            source_component=source_component,
            reason_code=reason_code,
            parent_intent_id=saved.parent_intent_id,
            client_order_id=saved.client_order_id,
            child_status=saved.child_status,
            aggregate_category=saved.aggregate_category,
        )

    @staticmethod
    def _merge_sticky_parent_fields(
        *,
        current: ExitExecutionIntent | None,
        incoming: ExitExecutionIntent,
    ) -> ExitExecutionIntent:
        if current is None:
            return incoming
        updates: dict[str, Any] = {}
        if current.cancel_requested and not incoming.cancel_requested:
            updates["cancel_requested"] = True
            updates["cancel_requested_ts"] = current.cancel_requested_ts
            if incoming.aggregate_status not in _TERMINAL_PARENT_STATUSES:
                updates["aggregate_status"] = "CANCEL_PENDING"
        if int(incoming.aggregate_version) <= int(current.aggregate_version):
            updates["aggregate_version"] = int(current.aggregate_version) + 1
        if not updates:
            return incoming
        return incoming.model_copy(update=updates)
