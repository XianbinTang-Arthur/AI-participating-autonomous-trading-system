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
                saved = self.exit_execution_repo.save_exit_execution_intent_in_session(
                    session,
                    self._merge_sticky_parent_fields(current=current, incoming=recomputed),
                )
                session.commit()
        else:
            current = self.exit_execution_repo.get_exit_execution_intent(parent_intent_id)
            if current is None:
                raise KeyError(f"exit_execution_intent_not_found parent_intent_id={parent_intent_id}")
            child_refs = self.exit_execution_repo.child_refs_for_parent(parent_intent_id=parent_intent_id)
            recomputed = recompute_parent(transform_parent(current), child_refs)
            saved = self.exit_execution_repo.save_exit_execution_intent(
                self._merge_sticky_parent_fields(current=current, incoming=recomputed)
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
                saved_parent = self.exit_execution_repo.save_exit_execution_intent_in_session(
                    session,
                    self._merge_sticky_parent_fields(current=parent_base, incoming=recomputed),
                )
                session.commit()
        else:
            existing_parent = self.exit_execution_repo.get_exit_execution_intent(parent_intent.parent_intent_id)
            parent_base = existing_parent or self.exit_execution_repo.save_exit_execution_intent(parent_intent)
            saved_child = self.exit_execution_repo.save_child_exit_order_ref(child_ref)
            child_refs = self.exit_execution_repo.child_refs_for_parent(parent_intent_id=parent_base.parent_intent_id)
            recomputed = recompute_parent(parent_base, child_refs)
            saved_parent = self.exit_execution_repo.save_exit_execution_intent(
                self._merge_sticky_parent_fields(current=parent_base, incoming=recomputed)
            )
        self._log_child_saved(saved_child, source_component=source_component, reason_code=f"{reason_code}:child_ref")
        self._log_parent_saved(saved_parent, source_component=source_component, reason_code=f"{reason_code}:parent")
        return saved_child, saved_parent

    def save_child_refs_and_recompute_parent(
        self,
        *,
        parent_intent: ExitExecutionIntent,
        child_refs: list[ChildExitOrderRef],
        transform_parent: ParentTransform,
        recompute_parent: ParentRecompute,
        source_component: str,
        reason_code: str,
    ) -> ExitExecutionIntent:
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
                for child_ref in child_refs:
                    self.exit_execution_repo.save_child_exit_order_ref_in_session(session, child_ref)
                persisted_child_refs = self.exit_execution_repo.child_refs_for_parent_in_session(
                    session,
                    parent_intent_id=parent_base.parent_intent_id,
                )
                recomputed = recompute_parent(transform_parent(parent_base), persisted_child_refs)
                saved_parent = self.exit_execution_repo.save_exit_execution_intent_in_session(
                    session,
                    self._merge_sticky_parent_fields(current=parent_base, incoming=recomputed),
                )
                session.commit()
        else:
            existing_parent = self.exit_execution_repo.get_exit_execution_intent(parent_intent.parent_intent_id)
            parent_base = existing_parent or self.exit_execution_repo.save_exit_execution_intent(parent_intent)
            for child_ref in child_refs:
                self.exit_execution_repo.save_child_exit_order_ref(child_ref)
                self._log_child_saved(
                    child_ref,
                    source_component=source_component,
                    reason_code=f"{reason_code}:child_ref",
                )
            persisted_child_refs = self.exit_execution_repo.child_refs_for_parent(
                parent_intent_id=parent_base.parent_intent_id
            )
            recomputed = recompute_parent(transform_parent(parent_base), persisted_child_refs)
            saved_parent = self.exit_execution_repo.save_exit_execution_intent(
                self._merge_sticky_parent_fields(current=parent_base, incoming=recomputed)
            )
        if isinstance(self.exit_execution_repo, PostgresExitExecutionRepository):
            for child_ref in child_refs:
                self._log_child_saved(
                    child_ref,
                    source_component=source_component,
                    reason_code=f"{reason_code}:child_ref",
                )
        self._log_parent_saved(saved_parent, source_component=source_component, reason_code=f"{reason_code}:parent")
        return saved_parent

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
        stale_incoming = int(incoming.aggregate_version) <= int(current.aggregate_version)
        if current.cancel_requested and not incoming.cancel_requested:
            updates["cancel_requested"] = True
            updates["cancel_requested_ts"] = current.cancel_requested_ts
            if incoming.aggregate_status not in _TERMINAL_PARENT_STATUSES:
                updates["aggregate_status"] = "CANCEL_PENDING"
        terminal_snapshot_is_sticky = current.aggregate_status in _TERMINAL_PARENT_STATUSES and (
            stale_incoming or incoming.aggregate_status != current.aggregate_status
        )
        if terminal_snapshot_is_sticky:
            updates.update(
                {
                    "aggregated_filled_quantity": current.aggregated_filled_quantity,
                    "aggregated_canceled_quantity": current.aggregated_canceled_quantity,
                    "aggregated_rejected_quantity": current.aggregated_rejected_quantity,
                    "open_child_working_quantity": current.open_child_working_quantity,
                    "open_child_unknown_quantity": current.open_child_unknown_quantity,
                    "remaining_dispatchable_quantity": current.remaining_dispatchable_quantity,
                    "remaining_unresolved_quantity": current.remaining_unresolved_quantity,
                    "aggregate_status": current.aggregate_status,
                    "reconciliation_state": current.reconciliation_state,
                    "risk_reducing_invariant": current.risk_reducing_invariant,
                    "child_order_ids": list(current.child_order_ids),
                    "completed_at": current.completed_at,
                    "operator_review_required": current.operator_review_required,
                    "operator_review_reason": current.operator_review_reason,
                    "metadata": dict(current.metadata),
                }
            )
        if stale_incoming:
            if (
                current.aggregate_status == "REVIEW_REQUIRED"
                and incoming.aggregate_status != current.aggregate_status
                and incoming.aggregate_status not in _TERMINAL_PARENT_STATUSES
            ):
                updates["aggregate_status"] = "REVIEW_REQUIRED"
                updates["reconciliation_state"] = "review_required"
            if current.operator_review_required and not incoming.operator_review_required:
                updates["operator_review_required"] = True
                updates["operator_review_reason"] = current.operator_review_reason
                if (
                    current.aggregate_status not in _TERMINAL_PARENT_STATUSES
                    and incoming.aggregate_status not in _TERMINAL_PARENT_STATUSES
                ):
                    updates["aggregate_status"] = "REVIEW_REQUIRED"
                    updates["reconciliation_state"] = "review_required"
            current_resume_issue = ExitExecutionWriter._resume_issue(current)
            incoming_resume_issue = ExitExecutionWriter._resume_issue(incoming)
            incoming_replaces_current_issue = (
                current_resume_issue is not None
                and incoming_resume_issue is not None
                and str(incoming_resume_issue.get("prior_kind") or "").strip()
                == str(current_resume_issue.get("kind") or "").strip()
            )
            if (
                current_resume_issue is not None
                and incoming_resume_issue != current_resume_issue
                and not incoming_replaces_current_issue
            ):
                metadata = dict(updates.get("metadata") or incoming.metadata)
                metadata["resume_issue"] = current_resume_issue
                updates["metadata"] = metadata
                updates["operator_review_required"] = True
                if current.operator_review_reason and not incoming.operator_review_reason:
                    updates["operator_review_reason"] = current.operator_review_reason
                if (
                    current.aggregate_status not in _TERMINAL_PARENT_STATUSES
                    and incoming.aggregate_status not in _TERMINAL_PARENT_STATUSES
                ):
                    updates["aggregate_status"] = "REVIEW_REQUIRED"
                    updates["reconciliation_state"] = "review_required"
            updates["aggregate_version"] = int(current.aggregate_version) + 1
        if not updates:
            return incoming
        return incoming.model_copy(update=updates)

    @staticmethod
    def _resume_issue(parent: ExitExecutionIntent) -> dict[str, Any] | None:
        issue = parent.metadata.get("resume_issue")
        return dict(issue) if isinstance(issue, dict) else None
