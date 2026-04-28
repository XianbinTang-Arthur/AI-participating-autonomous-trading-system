from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import log_event
from aats.events import topics
from aats.services.operator._parallel import parallel_fetch
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
from aats.services.execution_engine.state_writer import save_order_state_direct_legacy_only
from aats.services.runtime_scope import latest_topic_event_for_scope, reconciliation_reports_for_scope

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


def _rebaseline_shadow_obligations_to_sync(
    *,
    obligation_repo: Any,
    reservation_repo: Any,
) -> tuple[list[Any], dict[str, int | str | None]]:
    all_obligations = list(obligation_repo.all_obligations())
    obligation_count = len(all_obligations)
    reservation_count: int | None = None
    count_reservations = getattr(reservation_repo, "count_reservations", None)
    if callable(count_reservations):
        reservation_count = int(count_reservations())
        if reservation_count >= obligation_count:
            return [], {
                "reason": "shadow_reservations_current",
                "obligation_count": obligation_count,
                "reservation_count": reservation_count,
            }
    return all_obligations, {
        "reason": "shadow_reservation_backlog",
        "obligation_count": obligation_count,
        "reservation_count": reservation_count,
    }


class ReconciliationSystemQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def reconciliation_latest(self) -> dict[str, Any]:
        r = parallel_fetch({
            "report": self.owner._latest_scoped_reconciliation,
            "latest_validation": lambda: self.owner.runtime.event_store.latest(topics.RECONCILIATION_VALIDATIONS),
            "recovery": self.owner.recovery_view,
        })
        report = r["report"]
        latest_validation = r["latest_validation"]
        recovery = r["recovery"]
        return {
            "reconciliation": report.model_dump(mode="json") if report is not None else None,
            "mismatch_summary": self.owner._reconciliation_mismatch_summary(report),
            "exchange_bills_summary": self.owner._exchange_bills_summary(),
            "latest_validation": latest_validation.payload if latest_validation is not None else None,
            "baseline_generation": recovery.get("latest_baseline_generation"),
            "exchange_ack_watermark": recovery.get("latest_exchange_ack_watermark"),
            "reconciliation_state_snapshot": recovery.get("latest_state_snapshot"),
            "recovery": recovery,
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
            "recovery_state_snapshot": self.owner.recovery_view().get("latest_state_snapshot"),
        }

    async def validate_reconciliation(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        # 4 进程 gateway role 下 reconciliation_service 为 None，
        # 本操作目前不走代理（极低频调试命令），直接报错引导用户走 execution 节点。
        if self.owner.runtime.reconciliation_service is None:
            raise ValueError(
                "validate_reconciliation_requires_execution_role: "
                "this operation is not available on gateway process"
            )
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
        operator_confirmation: str | None = None,
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        # 4 进程 gateway role 下 reconciliation_service 为 None，
        # 本操作目前不走代理（极低频调试命令），直接报错引导用户走 execution 节点。
        if self.owner.runtime.reconciliation_service is None:
            raise ValueError(
                "resolve_stuck_submission_requires_execution_role: "
                "this operation is not available on gateway process"
            )
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
        command_lookup = getattr(self.owner, "_claimed_submit_command_for_order", None)
        claimed_submit_command = command_lookup(order) if callable(command_lookup) else None
        claimed_submit_command_id = None
        claimed_submit_idempotency_key = None
        operator_confirmation_required = claimed_submit_command is not None
        if isinstance(claimed_submit_command, dict):
            claimed_submit_command_id = str(claimed_submit_command.get("command_id") or "").strip() or None
            claimed_submit_idempotency_key = str(claimed_submit_command.get("idempotency_key") or "").strip() or None
            expected_confirmation = f"resolve_claimed_submit_as_failed:{client_order_id}"
            if str(operator_confirmation or "").strip() != expected_confirmation:
                raise ValueError(
                    "stuck_submission_resolution_blocked:"
                    "claimed_submit_requires_operator_confirmation"
                )
        resolution = dict(resolution)
        resolution.update(
            {
                "claimed_submit_command_present": claimed_submit_command is not None,
                "claimed_submit_command_id": claimed_submit_command_id,
                "claimed_submit_idempotency_key": claimed_submit_idempotency_key,
                "operator_confirmation_required": operator_confirmation_required,
                "operator_confirmation_matched": (
                    not operator_confirmation_required
                    or str(operator_confirmation or "").strip()
                    == f"resolve_claimed_submit_as_failed:{client_order_id}"
                ),
            }
        )

        now = utc_now()
        resolved_state = order.model_copy(
            update={
                "status": "FAILED",
                "last_update_ts": now,
                "execution_error": "operator_resolved_stuck_submission_after_restart",
                "cancel_reason": reason,
            }
        )
        execution_outbox_publisher = getattr(
            self.owner.runtime,
            "execution_outbox_publisher",
            None,
        )
        if execution_outbox_publisher is not None:
            persisted = await execution_outbox_publisher.persist_order_state(
                order_state=resolved_state,
                key=resolved_state.symbol,
                source_component="operator_api",
                emit_execution_error_summary=False,
                sync_execution_order_truth=True,
                history_reason_code="operator_state_sync",
            )
        else:
            persisted = save_order_state_direct_legacy_only(
                execution_repo=self.owner.runtime.execution_repo,
                order_state=resolved_state,
                source_component="operator_api",
                logger=self.owner.logger,
            )
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
                    "operator_confirmation_required": operator_confirmation_required,
                    "claimed_submit_command_present": claimed_submit_command is not None,
                    "claimed_submit_command_id": claimed_submit_command_id,
                    "claimed_submit_idempotency_key": claimed_submit_idempotency_key,
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

        # 4 进程 gateway role 走代理：portfolio_service / reconciliation_service
        # 在本进程为 None（_SLICE_REQUIRED_ROLES 门控），业务逻辑必须在 execution
        # 进程执行。OperatorCommandClient 用 NATS 把命令代理过去，correlation_id
        # 匹配响应后返回与 monolith 路径完全一致的 dict。
        # 设计文档：docs/task/slice_4proc_operator_command_proxy_fix_design.md §4.6
        if (
            self.owner.runtime.portfolio_service is None
            or self.owner.runtime.reconciliation_service is None
        ):
            client = getattr(self.owner.runtime, "operator_command_client", None)
            if client is None:
                raise RuntimeError(
                    "rebaseline_requires_operator_command_client: "
                    "gateway runtime missing client wiring"
                )
            try:
                return await client.invoke(
                    command="rebaseline",
                    payload={
                        "reason": reason,
                        "actor_role": actor_role,
                        "actor_identity": actor_identity,
                        "auth_source": auth_source,
                    },
                )
            finally:
                self.owner._invalidate_cache()

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

        # Stage 6 Slice 6.4：合并的 KillSwitch 提供 halt_async（async 写路径）
        await self.owner.runtime.kill_switch.halt_async(reason="operator_rebaseline_pending")
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
        baseline_importer = AccountBaselineImportService(
            event_store=self.owner.runtime.event_store,
            reconciliation_repo=self.owner.runtime.reconciliation_repo,
        )
        recent_bills_summary_getter = getattr(self.owner.runtime.account_service, "recent_bills_summary", None)
        imported = baseline_importer.rebaseline_snapshot(
            exchange_snapshot=exchange_snapshot,
            portfolio_state=self.owner.runtime.portfolio_service.state,
            product_type=self.owner.state_scope.product_type,
            margin_mode=self.owner.state_scope.margin_mode,
            allowed_symbols=self.owner.state_scope.allowed_symbols,
            previous_baseline_ref=previous_baseline_ref,
            operator_action_ref=action_envelope.event_id,
            trigger_reason=reason,
            exchange_bills_summary=(
                recent_bills_summary_getter()
                if callable(recent_bills_summary_getter)
                else {}
            ),
        )
        await self.owner.runtime.portfolio_service.bootstrap_snapshot(snapshot_origin="operator_rebaseline")
        report = await self.owner.runtime.reconciliation_service.validate_now(reason="operator_rebaseline")
        live_guard_service = getattr(self.owner.runtime, "derivatives_live_guard_service", None)
        if live_guard_service is not None:
            live_guard_service.reset_transient_risk_snapshot_state(reason="operator_rebaseline")
            live_guard_service.evaluate_now()

        # ── rebaseline 后清理历史 bundle 遗留 ──────────────────────
        # operator 已确认接受当前交易所状态为新基线，此时所有仍处于
        # review_required / partial_fill_recovery 的历史 bundle 应视为
        # "已收敛"。保留 bundle 记录但把 status 转为 recovered，避免它
        # 们继续阻断后续 resume。
        try:
            repo = self.owner.runtime.strategy_runtime_repo
            if repo is not None:
                scope = self.owner.state_scope
                candidates = repo.recent_execution_bundles(
                    product_type=scope.product_type,
                    margin_mode=scope.margin_mode,
                    limit=200,
                )
                resolved_count = 0
                for bundle in candidates:
                    if bundle.status not in ("review_required", "partial_fill_recovery"):
                        continue
                    updated = bundle.model_copy(
                        update={
                            "status": "recovered",
                            "reason_codes": list(bundle.reason_codes)
                            + ["operator_rebaseline_resolved"],
                        }
                    )
                    repo.save_execution_bundle(updated)
                    resolved_count += 1
                if resolved_count:
                    log_event(
                        self.owner.logger,
                        "operator.rebaseline.bundles_resolved",
                        resolved_count=resolved_count,
                        reason=reason,
                    )
        except Exception:
            # 清理 bundle 是尽力而为；失败不影响 rebaseline 主流程
            log_event(self.owner.logger, "operator.rebaseline.bundle_cleanup_failed", level="warning")
        # ── end bundle cleanup ─────────────────────────────────────

        # ── rebaseline 后同步 phase1 shadow obligation ─────────────
        # obligation_backlog = obligation_count - reservation_count。
        # rebaseline 表示 operator 已确认账实一致，此时把所有缺少
        # reservation 的 obligation 补写一次，消除 phase1_shadow_lagging。
        try:
            ledger_svc = getattr(self.owner.runtime, "phase1_ledger_mirror_service", None)
            obligation_repo = getattr(self.owner.runtime, "obligation_repo", None)
            if ledger_svc is not None and obligation_repo is not None:
                obligations_to_sync, sync_plan = _rebaseline_shadow_obligations_to_sync(
                    obligation_repo=obligation_repo,
                    reservation_repo=getattr(ledger_svc, "reservation_repo", None),
                )
                synced_count = 0
                for obl in obligations_to_sync:
                    ledger_svc.sync_obligation(obl, reason="operator_rebaseline_shadow_sync", related_fill=None)
                    synced_count += 1
                if synced_count:
                    log_event(
                        self.owner.logger,
                        "operator.rebaseline.shadow_obligations_synced",
                        synced_count=synced_count,
                        reason=reason,
                    )
                elif int(sync_plan.get("obligation_count") or 0) > 0:
                    log_event(
                        self.owner.logger,
                        "operator.rebaseline.shadow_obligations_sync_skipped",
                        reason=reason,
                        skip_reason=sync_plan.get("reason"),
                        obligation_count=sync_plan.get("obligation_count"),
                        reservation_count=sync_plan.get("reservation_count"),
                    )
        except Exception:
            log_event(self.owner.logger, "operator.rebaseline.shadow_obligation_sync_failed", level="warning")
        # ── end shadow obligation sync ─────────────────────────────

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
        auto_resume: dict[str, Any] | None = None
        if recovery_state != "resume_blocked":
            auto_resume = await self.resume(
                reason="auto_resume_after_rebaseline",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
        effective_recovery = self.owner.recovery_view()
        message = self._rebaseline_result_message(
            rebaseline_status=recovery_state,
            effective_recovery=effective_recovery,
            baseline=imported.snapshot.model_dump(mode="json"),
            reconciliation=report.model_dump(mode="json"),
            auto_resume=auto_resume,
        )
        return {
            "status": effective_recovery["recovery_state"],
            "rebaseline_status": recovery_state,
            "halted": self.owner.runtime.kill_switch.halted,
            "reason": reason,
            "message": message,
            "baseline": imported.snapshot.model_dump(mode="json"),
            "baseline_event_ref": imported.event_id,
            "reconciliation": report.model_dump(mode="json"),
            "recovery": effective_recovery,
            "auto_resume": auto_resume,
        }

    @classmethod
    def _rebaseline_result_message(
        cls,
        *,
        rebaseline_status: str,
        effective_recovery: dict[str, Any],
        baseline: dict[str, Any],
        reconciliation: dict[str, Any],
        auto_resume: dict[str, Any] | None,
    ) -> str:
        final_state = str(effective_recovery.get("recovery_state") or "")
        auto_resume_status = str((auto_resume or {}).get("status") or "")
        blockers = cls._rebaseline_message_blockers(
            effective_recovery=effective_recovery,
            baseline=baseline,
            reconciliation=reconciliation,
            auto_resume=auto_resume,
        )
        blocker_text = cls._join_rebaseline_blockers(blockers)

        if auto_resume_status in {"resumed", "already_resumed"} and bool(effective_recovery.get("safe_to_trade")):
            return "已接受当前状态为新基线，并已重新校验后恢复自动运行。"
        if final_state == "normal_operation" or bool(effective_recovery.get("safe_to_trade")):
            return "已接受当前状态为新基线，当前恢复状态允许自动运行。"
        if final_state == "manually_halted" or bool(effective_recovery.get("resume_eligible")):
            return "已接受当前状态为新基线，当前只剩手动暂停；确认无误后可恢复自动运行。"

        if rebaseline_status == "review_required" or final_state == "review_required":
            if blocker_text:
                return f"已接受当前状态为新基线，但最新恢复校验仍需要人工复核：{blocker_text}。"
            return "已接受当前状态为新基线，但最新恢复校验仍需要人工复核。"

        if rebaseline_status == "resume_blocked" or final_state in {"resume_blocked", "only_reduce"}:
            if blocker_text:
                return f"已接受当前状态为新基线，但当前仍不能恢复自动运行：{blocker_text}。"
            return "已接受当前状态为新基线，但当前仍不能恢复自动运行；请查看最新对账和恢复限制。"

        if auto_resume_status == "resume_blocked":
            if blocker_text:
                return f"已接受当前状态为新基线，但自动恢复校验被阻断：{blocker_text}。"
            return "已接受当前状态为新基线，但自动恢复校验被阻断。"

        return "已接受当前状态为新基线，并已重新计算恢复资格。"

    @staticmethod
    def _rebaseline_message_blockers(
        *,
        effective_recovery: dict[str, Any],
        baseline: dict[str, Any],
        reconciliation: dict[str, Any],
        auto_resume: dict[str, Any] | None,
    ) -> list[str]:
        blockers: list[str] = []
        baseline_open_orders = baseline.get("open_order_count")
        if isinstance(baseline_open_orders, int) and baseline_open_orders > 0:
            blockers.append(f"交易所仍有 {baseline_open_orders} 条挂单")
        elif bool(baseline.get("requires_operator_review")):
            blockers.append("基线仍需要人工复核")

        if bool(reconciliation.get("halt_required")):
            blockers.append("最新对账要求暂停")
        elif bool(reconciliation.get("review_required")):
            blockers.append("最新对账需要人工复核")
        if bool(reconciliation.get("only_reduce_required")):
            blockers.append("当前仍处于 only-reduce 限制")

        for reason in effective_recovery.get("resume_blocked_reasons") or []:
            if reason:
                blockers.append(str(reason))
        for blocker in (auto_resume or {}).get("blockers") or []:
            if isinstance(blocker, dict):
                code = blocker.get("blocker") or blocker.get("code")
            else:
                code = blocker
            if code:
                blockers.append(str(code))
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _join_rebaseline_blockers(blockers: list[str]) -> str:
        if not blockers:
            return ""
        visible = blockers[:4]
        suffix = f" 等 {len(blockers)} 项" if len(blockers) > len(visible) else ""
        return "、".join(visible) + suffix

    async def halt(
        self,
        *,
        reason: str,
        actor_role: OperatorRole = "anonymous",
        actor_identity: str | None = None,
        auth_source: AuthSource = "anonymous",
    ) -> dict[str, Any]:
        was_halted = self.owner.runtime.kill_switch.halted
        recovery_before = self.owner.recovery_view()["recovery_state"]
        # Stage 6 Slice 6.4：合并的 KillSwitch 提供 halt_async
        await self.owner.runtime.kill_switch.halt_async(reason=reason)
        log_event(self.owner.logger, "operator_halt", level="warning", reason=reason, already_halted=was_halted)
        status = "already_halted" if was_halted else "halted"
        # Stage 5d hardening: halt 操作必须将 recovery_state 设为 resume_blocked。
        # normal_operation → resume_blocked 是原始逻辑；multi_process_role_skip
        # 是非 execution 进程的占位符，halt 后同样应变为 resume_blocked，否则
        # 占位符会透传到 finalize_status 并污染后续状态评估。
        current_state = self.owner.runtime.recovery_status.recovery_state
        updated_status = self.owner.runtime.recovery_status.model_copy(
            update={
                "recovery_state": (
                    "resume_blocked"
                    if current_state in ("normal_operation", "multi_process_role_skip")
                    else current_state
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
        # 4 进程 gateway role 走代理：resume 的 resume_check 需要
        # reconciliation_service.validate_now()，在 gateway 为 None。
        # 设计文档：docs/task/slice_4proc_operator_command_proxy_fix_design.md §4.6
        if self.owner.runtime.reconciliation_service is None:
            client = getattr(self.owner.runtime, "operator_command_client", None)
            if client is None:
                raise RuntimeError(
                    "resume_requires_operator_command_client: "
                    "gateway runtime missing client wiring"
                )
            try:
                return await client.invoke(
                    command="resume",
                    payload={
                        "reason": reason,
                        "actor_role": actor_role,
                        "actor_identity": actor_identity,
                        "auth_source": auth_source,
                    },
                )
            finally:
                self.owner._invalidate_cache()

        was_halted = self.owner.runtime.kill_switch.halted
        recovery_before = self.owner.recovery_view()["recovery_state"]
        report = None
        refresh_error: Exception | None = None
        if self.owner.runtime.settings.account_backend == "okx" and self.owner.runtime.settings.account_read_enabled:
            try:
                await self.owner._refresh_account_state_for_operator_resolution()
            except Exception as exc:
                refresh_error = exc
                log_event(
                    self.owner.logger,
                    "operator_resume_account_refresh_failed",
                    level="warning",
                    reason=reason,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        # Stage 6 Slice 6.4：合并的 KillSwitch 提供 halt_async / resume_async
        if refresh_error is not None:
            await self.owner.runtime.kill_switch.halt_async(reason="resume_blocked")
            status = "resume_blocked"
            runnable = False
            recovery_after = "resume_blocked"
            report = self.owner._latest_scoped_reconciliation()
            updated_status = self.owner.runtime.recovery_status.model_copy(
                update={
                    "recovery_state": recovery_after,
                    "recovery_action": "operator_resume_blocked",
                    "last_resume_status": status,
                    "last_resume_reason": reason,
                    "resume_blocked_reasons": ["account_snapshot_refresh_failed"],
                }
            )
            self.owner.runtime.recovery_status = self.owner.recovery_posture.finalize_status(
                base_status=updated_status,
                latest_reconciliation=report,
            )
            resume_check_blockers = tuple(self.owner.runtime.recovery_status.resume_blocked_reasons)
        else:
            report = await self.owner.runtime.reconciliation_service.validate_now(reason=f"resume_check:{reason}")
            self.owner._update_recovery_status_for_report(report)
            resume_check = self.owner.recovery_posture.resume_check(include_kill_switch=False, latest_reconciliation=report)
            runnable = resume_check.runnable
            resume_check_blockers = tuple(resume_check.blockers)
            if runnable:
                await self.owner.runtime.kill_switch.resume_async()
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
                # 保留跨进程信号：如果 kill_switch 已有更具体的原因（如
                # trial_guard_threshold_breached）且该原因仍在当前阻断列表中，
                # 不用通用 "resume_blocked" 覆盖。gateway 进程没有本地
                # trial_guard_service，依赖 kill_switch reason 检测跨进程
                # blocker 并在 UI 显示对应操作按钮。
                current_reason = self.owner.runtime.kill_switch.status().get("reason", "")
                if current_reason and current_reason != "resume_blocked" and current_reason in resume_check.blockers:
                    halt_reason = current_reason
                else:
                    halt_reason = "resume_blocked"
                await self.owner.runtime.kill_switch.halt_async(reason=halt_reason)
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
                reconciliation_id=None if report is None else report.reconciliation_id,
                details={
                    "runnable": runnable,
                    "blockers": list(resume_check_blockers),
                    "account_refresh_error": None if refresh_error is None else str(refresh_error),
                },
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
        recovery = self.owner.recovery_view()
        return {
            "status": status,
            "halted": self.owner.runtime.kill_switch.halted,
            "reason": reason,
            "message": self._resume_result_message(
                status=status,
                runnable=runnable,
                blockers=blockers,
                recovery=recovery,
                refresh_error=refresh_error,
            ),
            "runnable": runnable,
            "blockers": blockers,
            "recovery": recovery,
            "reconciliation": None if report is None else report.model_dump(mode="json"),
        }

    @classmethod
    def _resume_result_message(
        cls,
        *,
        status: str,
        runnable: bool,
        blockers: list[dict[str, Any]],
        recovery: dict[str, Any],
        refresh_error: Exception | None = None,
    ) -> str:
        if status in {"resumed", "already_resumed"} and runnable:
            return "恢复校验已通过，系统已恢复自动运行。"
        blocker_text = cls._join_rebaseline_blockers(
            cls._resume_message_blockers(
                blockers=blockers,
                recovery=recovery,
                refresh_error=refresh_error,
            )
        )
        if status == "resume_blocked":
            if blocker_text:
                return f"恢复自动运行被阻断：{blocker_text}。"
            return "恢复自动运行被阻断；请查看最新对账和恢复限制。"
        if bool(recovery.get("resume_eligible")) and not bool(recovery.get("safe_to_trade")):
            if blocker_text:
                return f"恢复请求已重新校验，但仍需要人工确认：{blocker_text}。"
            return "恢复请求已重新校验，但仍需要人工确认。"
        return "恢复请求已重新校验。"

    @staticmethod
    def _resume_message_blockers(
        *,
        blockers: list[dict[str, Any]],
        recovery: dict[str, Any],
        refresh_error: Exception | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        if refresh_error is not None:
            reasons.append("账户状态刷新失败")
        for reason in recovery.get("resume_blocked_reasons") or []:
            if reason:
                reasons.append(str(reason))
        for blocker in blockers:
            if not isinstance(blocker, dict):
                continue
            code = blocker.get("blocker") or blocker.get("code")
            if code:
                reasons.append(str(code))
        return list(dict.fromkeys(reasons))
