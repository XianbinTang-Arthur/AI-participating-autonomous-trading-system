from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import log_event
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.common import utc_now
from aats.schemas.operator import (
    AuthSource,
    ExecutionErrorSummary,
    OperatorActionRecord,
    OperatorRole,
    ReconciliationValidationSummary,
)
from aats.services.execution_engine.baseline_import import AccountBaselineImportService
from aats.services.runtime_scope import latest_topic_event_for_scope, reconciliation_reports_for_scope

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class ReconciliationSystemQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def reconciliation_latest(self) -> dict[str, Any]:
        report = self.owner._latest_scoped_reconciliation()
        latest_validation = self.owner.runtime.event_store.latest(topics.RECONCILIATION_VALIDATIONS)
        return {
            "reconciliation": report.model_dump(mode="json") if report is not None else None,
            "mismatch_summary": self.owner._reconciliation_mismatch_summary(report),
            "exchange_bills_summary": self.owner._exchange_bills_summary(),
            "latest_validation": latest_validation.payload if latest_validation is not None else None,
            "recovery": self.owner.recovery_view(),
        }

    def reconciliation_recent(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        history = list(reversed(reconciliation_reports_for_scope(self.owner.runtime.reconciliation_repo, self.owner.state_scope)))
        return self.owner._paginate_rows(
            history,
            limit=limit,
            offset=offset,
            key="reconciliations",
            serializer=lambda report: report.model_dump(mode="json"),
        ) | {"exchange_bills_summary": self.owner._exchange_bills_summary()}

    def reconciliation_mismatches(self, *, limit: int = 20) -> dict[str, Any]:
        reports = [
            report
            for report in reconciliation_reports_for_scope(
                self.owner.runtime.reconciliation_repo,
                self.owner.state_scope,
                limit=limit * 4,
            )
            if report.severity != "CLEAN"
        ][-limit:]
        return {
            "mismatches": [self.owner._reconciliation_mismatch_summary(report) for report in reports],
            "exchange_bills_summary": self.owner._exchange_bills_summary(),
        }

    def reconciliation_detail(self, reconciliation_id: str) -> dict[str, Any]:
        report = next(
            (
                item
                for item in reconciliation_reports_for_scope(self.owner.runtime.reconciliation_repo, self.owner.state_scope)
                if item.reconciliation_id == reconciliation_id
            ),
            None,
        )
        if report is None:
            raise KeyError(f"reconciliation_not_found:{reconciliation_id}")
        return {
            "reconciliation": report.model_dump(mode="json"),
            "mismatch_summary": self.owner._reconciliation_mismatch_summary(report),
            "exchange_bills_summary": self.owner._exchange_bills_summary(),
            "exchange_bills_explanations": report.exchange_bills_explanations,
        }

    async def validate_reconciliation(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        report = await self.owner.runtime.reconciliation_service.validate_now(reason=reason)
        summary = ReconciliationValidationSummary(
            trigger=reason,
            reconciliation_id=report.reconciliation_id,
            decision_id=report.decision_id,
            severity=report.severity,
            halt_required=report.halt_required,
            exchange_comparison_enabled=report.exchange_comparison_enabled,
            mismatch_reasons=list(report.mismatch_reasons),
            safety_impacts=list(report.safety_impacts),
            validated_at=utc_now(),
        )
        self.owner._append_event(
            topic=topics.RECONCILIATION_VALIDATIONS,
            key=report.decision_id or "portfolio",
            payload_model=summary,
        )
        self.owner._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="reconciliation_validate",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status="completed",
                decision_id=report.decision_id,
                recovery_state_before=self.owner.recovery_view()["recovery_state"],
                recovery_state_after=(
                    "resume_blocked"
                    if report.halt_required
                    else "review_required"
                    if report.review_required
                    else self.owner.recovery_view()["recovery_state"]
                ),
                reconciliation_id=report.reconciliation_id,
            ),
        )
        self.owner._update_recovery_status_for_report(report)
        self.owner._persist_blocker_snapshot(
            source="reconciliation_validate",
            runtime_state=self.owner.system_health()["runtime_state"],
            mode_snapshot=self.owner.system_mode(),
            blockers=self.owner.blockers(),
        )
        return {
            "reconciliation": report.model_dump(mode="json"),
            "validation": summary.model_dump(mode="json"),
        }

    async def resolve_stuck_submission(
        self,
        *,
        client_order_id: str,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        order = self.owner._control_plane_order_state(client_order_id)
        if order is None:
            raise KeyError(f"order_not_found:{client_order_id}")

        fills = self.owner._control_plane_fills_for_order(client_order_id)
        recovery_before = self.owner.recovery_view()["recovery_state"]
        exchange_snapshot = await self.owner._refresh_exchange_snapshot_for_resolution()
        resolution = self.owner._stuck_submission_resolution(
            order=order,
            fills=fills,
            exchange_snapshot=exchange_snapshot,
        )
        if not resolution["eligible"]:
            raise ValueError(f"stuck_submission_resolution_blocked:{resolution['reason_code']}")

        now = utc_now()
        resolved_state = order.model_copy(
            update={
                "status": "FAILED",
                "last_update_ts": now,
                "execution_error": "operator_resolved_stuck_submission_after_restart",
                "cancel_reason": reason,
            }
        )
        persisted = self.owner.runtime.execution_repo.save_order_state(resolved_state)
        self.owner._sync_execution_order_truth(persisted)
        if self.owner.runtime.audit_repo.get(persisted.decision_id) is not None:
            await publish_model(
                bus=self.owner.runtime.bus,
                topic=topics.ORDER_UPDATES,
                key=persisted.symbol,
                payload_model=persisted,
                source_component="operator_api",
            )
        else:
            self.owner._append_event(
                topic=topics.ORDER_UPDATES,
                key=persisted.symbol,
                payload_model=persisted,
            )
        await publish_model(
            bus=self.owner.runtime.bus,
            topic=topics.EXECUTION_ERROR_SUMMARIES,
            key=persisted.symbol,
            payload_model=ExecutionErrorSummary(
                subsystem="operator_recovery",
                severity="warning",
                message="Operator resolved a stuck pre-restart submission as FAILED.",
                decision_id=persisted.decision_id,
                intent_id=persisted.intent_id,
                order_id=persisted.client_order_id,
                status=persisted.status,
                observed_at=now,
            ),
            source_component="operator_api",
        )
        report = await self.owner.runtime.reconciliation_service.validate_now(
            reason=f"resolve_stuck_submission:{client_order_id}"
        )
        self.owner._update_recovery_status_for_report(report)
        self.owner._invalidate_cache()
        recovery_after = self.owner.recovery_view()["recovery_state"]
        self.owner._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="resolve_stuck_submission",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status="resolved_as_failed",
                decision_id=persisted.decision_id,
                order_id=persisted.client_order_id,
                recovery_state_before=recovery_before,
                recovery_state_after=recovery_after,
                reconciliation_id=report.reconciliation_id,
                details={
                    "previous_order_status": order.status,
                    "final_order_status": persisted.status,
                    "resolution": resolution,
                },
            ),
        )
        self.owner._persist_blocker_snapshot(
            source="resolve_stuck_submission",
            runtime_state=self.owner.system_health()["runtime_state"],
            mode_snapshot=self.owner.system_mode(),
            blockers=self.owner.blockers(),
        )
        log_event(
            self.owner.logger,
            "resolve_stuck_submission",
            level="warning",
            order_id=persisted.client_order_id,
            previous_status=order.status,
            final_status=persisted.status,
            reason=reason,
        )
        return {
            "order": persisted.model_dump(mode="json"),
            "resolution": resolution,
            "reconciliation": report.model_dump(mode="json"),
            "recovery": self.owner.recovery_view(),
        }

    async def rebaseline(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        if not self.owner.runtime.settings.account_read_enabled or self.owner.runtime.settings.account_backend != "okx":
            raise ValueError("rebaseline_requires_okx_account_read")
        if not self.owner.runtime.recovery_policy.operator_rebaseline_supported:
            raise ValueError("rebaseline_not_supported_for_runtime_profile")

        recovery_before = self.owner.recovery_view()["recovery_state"]
        previous_baseline_event = latest_topic_event_for_scope(
            self.owner.runtime.event_store,
            topics.ACCOUNT_BASELINES,
            self.owner.state_scope,
        )
        previous_baseline_ref = previous_baseline_event.event_id if previous_baseline_event is not None else None

        exchange_snapshot = await self.owner.runtime.account_service.refresh(force=True)
        if exchange_snapshot is None:
            raise ValueError("rebaseline_requires_account_snapshot")

        self.owner.runtime.kill_switch.halt(reason="operator_rebaseline_pending")
        pending_status = self.owner.runtime.recovery_status.model_copy(
            update={"recovery_state": "rebaseline_pending", "recovery_action": "operator_rebaseline_pending"}
        )
        self.owner.runtime.recovery_status = self.owner.recovery_posture.finalize_status(base_status=pending_status)

        action_record = OperatorActionRecord(
            action="rebaseline",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
            status="completed",
            recovery_state_before=recovery_before,
            recovery_state_after="rebaseline_pending",
        )
        action_envelope = self.owner._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=action_record,
        )
        baseline_importer = AccountBaselineImportService(event_store=self.owner.runtime.event_store)
        imported = baseline_importer.rebaseline_snapshot(
            exchange_snapshot=exchange_snapshot,
            portfolio_state=self.owner.runtime.portfolio_service.state,
            product_type=self.owner.state_scope.product_type,
            margin_mode=self.owner.state_scope.margin_mode,
            allowed_symbols=self.owner.state_scope.allowed_symbols,
            previous_baseline_ref=previous_baseline_ref,
            operator_action_ref=action_envelope.event_id,
            trigger_reason=reason,
        )
        await self.owner.runtime.portfolio_service.bootstrap_snapshot(snapshot_origin="operator_rebaseline")
        report = await self.owner.runtime.reconciliation_service.validate_now(reason="operator_rebaseline")

        recovery_state = (
            "resume_blocked"
            if imported.snapshot.requires_operator_review or report.halt_required
            else "review_required"
            if report.review_required
            else "rebaseline_completed"
        )
        updated_status = self.owner.runtime.recovery_status.model_copy(
            update={
                "status": imported.snapshot.baseline_status,
                "recovery_state": recovery_state,
                "safe_startup": False,
                "recovery_action": (
                    "operator_rebaseline_completed"
                    if recovery_state == "rebaseline_completed"
                    else "operator_rebaseline_requires_review"
                    if recovery_state == "review_required"
                    else "operator_rebaseline_resume_blocked"
                ),
                "baseline_imported": True,
                "baseline_status": imported.snapshot.baseline_status,
                "baseline_imported_at": imported.snapshot.imported_at,
                "baseline_event_ref": imported.event_id,
                "baseline_source": imported.snapshot.account_source,
                "baseline_safe_for_automatic_continuation": imported.snapshot.safe_for_automatic_continuation,
                "baseline_requires_operator_review": imported.snapshot.requires_operator_review,
                "baseline_balance_count": imported.snapshot.balance_count,
                "baseline_position_count": imported.snapshot.position_count,
                "baseline_open_order_count": imported.snapshot.open_order_count,
                "baseline_fill_count": imported.snapshot.fill_count,
                "last_rebaseline_at": imported.snapshot.imported_at,
                "last_rebaseline_event_ref": imported.event_id,
                "last_rebaseline_action_ref": action_envelope.event_id,
                "notes": self.owner.runtime.recovery_status.notes
                + [
                    "operator_rebaseline_confirmed",
                    f"baseline_switch:{previous_baseline_ref or 'none'}->{imported.event_id}",
                ],
            }
        )
        self.owner.runtime.recovery_status = self.owner.recovery_posture.finalize_status(
            base_status=updated_status,
            latest_reconciliation=report,
        )
        self.owner._invalidate_cache()
        resume_eligible = self.owner.runtime.recovery_status.resume_eligible
        self.owner._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=action_record.model_copy(
                update={
                    "status": recovery_state,
                    "recovery_state_after": recovery_state,
                    "baseline_event_ref": imported.event_id,
                    "reconciliation_id": report.reconciliation_id,
                    "details": {
                        "previous_baseline_ref": previous_baseline_ref,
                        "resume_eligible": resume_eligible,
                    },
                }
            ),
        )
        self.owner._persist_blocker_snapshot(
            source="operator_rebaseline",
            runtime_state="halted",
            mode_snapshot=self.owner.system_mode(),
            blockers=self.owner.blockers(),
        )
        return {
            "status": recovery_state,
            "halted": True,
            "reason": reason,
            "baseline": imported.snapshot.model_dump(mode="json"),
            "baseline_event_ref": imported.event_id,
            "reconciliation": report.model_dump(mode="json"),
            "recovery": self.owner.recovery_view(),
        }

    def halt(
        self,
        *,
        reason: str,
        actor_role: OperatorRole = "anonymous",
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        was_halted = self.owner.runtime.kill_switch.halted
        recovery_before = self.owner.recovery_view()["recovery_state"]
        self.owner.runtime.kill_switch.halt(reason=reason)
        log_event(self.owner.logger, "operator_halt", level="warning", reason=reason, already_halted=was_halted)
        status = "already_halted" if was_halted else "halted"
        updated_status = self.owner.runtime.recovery_status.model_copy(
            update={
                "recovery_state": (
                    "resume_blocked"
                    if self.owner.runtime.recovery_status.recovery_state == "normal_operation"
                    else self.owner.runtime.recovery_status.recovery_state
                ),
                "last_resume_status": None,
                "last_resume_reason": None,
            }
        )
        self.owner.runtime.recovery_status = self.owner.recovery_posture.finalize_status(base_status=updated_status)
        self.owner._invalidate_cache()
        self.owner._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="halt",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status=status,
                recovery_state_before=recovery_before,
                recovery_state_after=self.owner.recovery_view()["recovery_state"],
            ),
        )
        self.owner._persist_blocker_snapshot(
            source="operator_halt",
            runtime_state="halted",
            mode_snapshot=self.owner.system_mode(),
            blockers=self.owner.blockers(),
        )
        return {"status": status, "halted": True, "reason": reason}

    async def resume(
        self,
        *,
        reason: str,
        actor_role: OperatorRole = "anonymous",
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        was_halted = self.owner.runtime.kill_switch.halted
        recovery_before = self.owner.recovery_view()["recovery_state"]
        if self.owner.runtime.settings.account_backend == "okx" and self.owner.runtime.settings.account_read_enabled:
            await self.owner.runtime.account_service.refresh(force=True)
        report = await self.owner.runtime.reconciliation_service.validate_now(reason=f"resume_check:{reason}")
        self.owner._update_recovery_status_for_report(report)
        resume_check = self.owner.recovery_posture.resume_check(include_kill_switch=False, latest_reconciliation=report)
        runnable = resume_check.runnable
        if runnable:
            self.owner.runtime.kill_switch.resume()
            status = "already_resumed" if not was_halted else "resumed"
            recovery_after = "normal_operation"
            updated_status = self.owner.runtime.recovery_status.model_copy(
                update={
                    "recovery_state": recovery_after,
                    "recovery_action": "operator_resume_completed",
                    "last_resume_status": status,
                    "last_resume_reason": reason,
                    "resume_blocked_reasons": [],
                }
            )
            self.owner.runtime.recovery_status = self.owner.recovery_posture.finalize_status(
                base_status=updated_status,
                latest_reconciliation=report,
            )
        else:
            self.owner.runtime.kill_switch.halt(reason="resume_blocked")
            status = "resume_blocked"
            recovery_after = (
                "review_required"
                if "operator_rebaseline_required" in resume_check.blockers
                else "resume_blocked"
            )
            updated_status = self.owner.runtime.recovery_status.model_copy(
                update={
                    "recovery_state": recovery_after,
                    "recovery_action": "operator_resume_blocked",
                    "last_resume_status": status,
                    "last_resume_reason": reason,
                    "resume_blocked_reasons": list(resume_check.blockers),
                }
            )
            self.owner.runtime.recovery_status = self.owner.recovery_posture.finalize_status(
                base_status=updated_status,
                latest_reconciliation=report,
            )
        self.owner._invalidate_cache()
        mode_state = self.owner.system_mode()
        blockers = self.owner.blockers()
        log_event(
            self.owner.logger,
            "operator_resume",
            level="info",
            reason=reason,
            was_halted=was_halted,
            blockers=[item["blocker"] for item in blockers],
            status=status,
        )
        action_envelope = self.owner._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="resume",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason=reason,
                status=status,
                recovery_state_before=recovery_before,
                recovery_state_after=recovery_after,
                reconciliation_id=report.reconciliation_id,
                details={"runnable": runnable, "blockers": list(resume_check.blockers)},
            ),
        )
        self.owner.runtime.recovery_status = self.owner.runtime.recovery_status.model_copy(
            update={"last_resume_action_ref": action_envelope.event_id}
        )
        self.owner._persist_blocker_snapshot(
            source="operator_resume",
            runtime_state="blocked" if mode_state["execution_blocked"] else "healthy",
            mode_snapshot=mode_state,
            blockers=blockers,
        )
        return {
            "status": status,
            "halted": self.owner.runtime.kill_switch.halted,
            "reason": reason,
            "runnable": runnable,
            "blockers": blockers,
            "recovery": self.owner.recovery_view(),
            "reconciliation": report.model_dump(mode="json"),
        }
