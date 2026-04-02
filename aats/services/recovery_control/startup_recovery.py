from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from aats.schemas.exchange import AccountBaselineSnapshot
from aats.schemas.common import utc_now
from aats.schemas.exit_execution import ExitExecutionIntent
from aats.schemas.reconciliation import ReconciliationStateSnapshot
from aats.schemas.system import RecoveryStatus
from aats.services.execution_engine.exit_intent_aggregator import (
    exit_execution_review_items,
    refresh_exit_execution_intents,
)
from aats.services.execution_control.order_service import ExecutionOrderService
from aats.services.execution_engine.recovery import ExecutionRecoveryService, RecoveryArtifacts
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.recovery_control.reconciliation_classifier import RecoveryReconciliationClassifier
from aats.services.runtime_scope import latest_reconciliation_for_scope, latest_snapshot_for_scope, runtime_state_scope


def startup_refresh_exit_execution_truth(
    *,
    settings: object,
    execution_repo: object | None,
    exit_execution_repo: object | None,
    scope: object | None,
) -> tuple[list[ExitExecutionIntent], list[str]]:
    if execution_repo is None or exit_execution_repo is None:
        return [], []
    try:
        refreshed = refresh_exit_execution_intents(
            execution_repo=execution_repo,
            exit_execution_repo=exit_execution_repo,
            settings=settings,
            scope=scope,
        )
    except Exception as exc:  # pragma: no cover - guarded by dedicated unit test via fake repo
        return [], [f"startup_exit_execution_parent_refresh_failed:{type(exc).__name__}"]
    if not refreshed:
        return [], []
    return list(refreshed), [f"startup_exit_execution_parent_refresh_count:{len(refreshed)}"]


def apply_startup_exit_execution_review_overlay(
    *,
    base_status: RecoveryStatus,
    parent_intents: list[ExitExecutionIntent],
) -> RecoveryStatus:
    review_items = exit_execution_review_items(parent_intents)
    if not review_items:
        return base_status
    review_required_count = sum(
        1
        for item in review_items
        if bool(item.get("operator_review_required"))
    )
    blocks_resume = any(bool(item.get("blocks_resume", True)) for item in review_items)
    blocker_kinds = [
        str(item.get("kind") or "").strip()
        for item in review_items
        if str(item.get("kind") or "").strip()
    ]
    notes = list(base_status.notes)
    notes.append(f"startup_exit_execution_overlay_count:{len(review_items)}")
    if review_required_count:
        notes.append(f"startup_exit_execution_review_required_count:{review_required_count}")
    resume_blocked_reasons = list(base_status.resume_blocked_reasons)
    resume_blocked_reasons.extend(
        blocker for blocker in blocker_kinds if blocker not in resume_blocked_reasons
    )
    recovery_state = (
        base_status.recovery_state
        if base_status.recovery_state == "resume_blocked"
        else "review_required"
        if review_required_count
        else "resume_blocked"
    )
    return base_status.model_copy(
        update={
            "recovery_state": recovery_state,
            "review_required": bool(base_status.review_required or review_required_count),
            "safe_startup": False if blocks_resume or review_required_count else base_status.safe_startup,
            "safe_to_trade": False if blocks_resume or review_required_count else base_status.safe_to_trade,
            "resume_eligible": False if blocks_resume or review_required_count else base_status.resume_eligible,
            "rebaseline_available": bool(base_status.rebaseline_available or review_required_count),
            "resume_blocked_reasons": list(dict.fromkeys(resume_blocked_reasons)),
            "unknown_state_details": [
                *base_status.unknown_state_details,
                *review_items,
            ],
            "notes": list(dict.fromkeys(notes)),
        }
    )


def persist_startup_exit_execution_state_snapshot(
    *,
    reconciliation_repo: object,
    scope: object,
    status: RecoveryStatus,
    parent_intents: list[ExitExecutionIntent],
) -> list[str]:
    review_items = exit_execution_review_items(parent_intents)
    if not review_items:
        return []
    latest_reconciliation = latest_reconciliation_for_scope(reconciliation_repo, scope)
    if latest_reconciliation is None:
        return ["startup_exit_execution_review_snapshot_skipped_missing_reconciliation_context"]
    save_snapshot = getattr(reconciliation_repo, "save_state_snapshot", None)
    latest_state_snapshot_getter = getattr(reconciliation_repo, "latest_state_snapshot_for_scope", None)
    if not callable(save_snapshot) or not callable(latest_state_snapshot_getter):
        return []
    latest_state_snapshot = latest_state_snapshot_getter(scope=scope)
    latest_generation_getter = getattr(reconciliation_repo, "latest_baseline_generation_for_scope", None)
    latest_baseline_generation = (
        latest_generation_getter(scope=scope)
        if callable(latest_generation_getter)
        else None
    )
    latest_ack_getter = getattr(reconciliation_repo, "latest_exchange_ack_watermark_for_scope", None)
    latest_exchange_ack_watermark = (
        latest_ack_getter(scope=scope)
        if callable(latest_ack_getter)
        else None
    )
    details = {
        "source": "startup_exit_execution_review",
        "review_item_count": len(review_items),
        "review_items": review_items,
        "reconciliation_severity": latest_reconciliation.severity,
        "recovery_classification": latest_reconciliation.recovery_classification,
    }
    snapshot = ReconciliationStateSnapshot(
        reconciliation_id=latest_reconciliation.reconciliation_id,
        product_type=latest_reconciliation.product_type,
        margin_mode=latest_reconciliation.margin_mode,
        primary_symbol=(latest_reconciliation.allowed_symbols or [None])[0],
        recovery_state=status.recovery_state,
        resume_eligible=status.resume_eligible,
        safe_to_trade=status.safe_to_trade,
        review_required=status.review_required,
        only_reduce_required=status.only_reduce_required,
        halt_required=bool(latest_reconciliation.halt_required),
        bundle_recovery_required=status.bundle_recovery_required,
        resume_blocked_reasons_json=list(status.resume_blocked_reasons),
        derived_from_generation_id=(
            None if latest_baseline_generation is None else latest_baseline_generation.generation_id
        ),
        exchange_ack_watermark_id=(
            None if latest_exchange_ack_watermark is None else latest_exchange_ack_watermark.watermark_id
        ),
        details_json=details,
    )
    should_persist = latest_state_snapshot is None or any(
        (
            latest_state_snapshot.reconciliation_id != snapshot.reconciliation_id,
            latest_state_snapshot.recovery_state != snapshot.recovery_state,
            latest_state_snapshot.resume_eligible != snapshot.resume_eligible,
            latest_state_snapshot.safe_to_trade != snapshot.safe_to_trade,
            latest_state_snapshot.review_required != snapshot.review_required,
            latest_state_snapshot.only_reduce_required != snapshot.only_reduce_required,
            latest_state_snapshot.halt_required != snapshot.halt_required,
            latest_state_snapshot.bundle_recovery_required != snapshot.bundle_recovery_required,
            list(latest_state_snapshot.resume_blocked_reasons_json) != list(snapshot.resume_blocked_reasons_json),
            dict(latest_state_snapshot.details_json) != dict(snapshot.details_json),
        )
    )
    if not should_persist:
        return []
    save_snapshot(snapshot)
    return ["startup_exit_execution_review_snapshot_saved"]


@dataclass(slots=True)
class ExecutionLedgerRecoveryService:
    settings: object
    base_recovery_service: ExecutionRecoveryService
    reconciliation_repo: object
    portfolio_repo: object
    kill_switch: KillSwitch
    reconciliation_classifier: RecoveryReconciliationClassifier
    execution_order_repo: object | None = None
    execution_command_repo: object | None = None
    runtime_scope: object = field(init=False)

    def __post_init__(self) -> None:
        self.runtime_scope = runtime_state_scope(self.settings)

    def recover(
        self,
        *,
        portfolio_state,
        account_baseline: AccountBaselineSnapshot | None = None,
        account_baseline_event_id: str | None = None,
    ) -> RecoveryArtifacts:
        artifacts = self.base_recovery_service.recover(
            portfolio_state=portfolio_state,
            account_baseline=account_baseline,
            account_baseline_event_id=account_baseline_event_id,
        )
        latest_reconciliation = latest_reconciliation_for_scope(self.reconciliation_repo, self.runtime_scope)
        if latest_reconciliation is not None:
            latest_reconciliation = self.reconciliation_classifier.annotate(latest_reconciliation)
        return RecoveryArtifacts(
            status=self._phase4_status(
                base_status=artifacts.status,
                latest_reconciliation=latest_reconciliation,
            ),
            rebuilt_snapshot_saved=artifacts.rebuilt_snapshot_saved,
            rebuilt_snapshot=artifacts.rebuilt_snapshot,
        )

    def _phase4_status(
        self,
        *,
        base_status: RecoveryStatus,
        latest_reconciliation,
    ) -> RecoveryStatus:
        recovered_order_count = base_status.recovered_order_count
        open_order_count = base_status.open_order_count
        pending_command_count = 0
        pending_submit_command_count = 0
        pending_cancel_command_count = 0
        sent_stale_command_count = 0
        sent_stale_submit_command_count = 0
        sent_stale_cancel_command_count = 0
        stranded_submit_order_count = 0
        stuck_sent_submit_order_count = 0
        if self.execution_order_repo is not None:
            count_orders = getattr(self.execution_order_repo, "count_orders", None)
            open_orders = getattr(self.execution_order_repo, "open_orders", None)
            if callable(count_orders):
                recovered_order_count = max(recovered_order_count, int(count_orders()))
            if callable(open_orders):
                scoped_open_orders = list(open_orders())
                open_order_count = max(open_order_count, len(scoped_open_orders))
                stranded_submit_order_count, stuck_sent_submit_order_count = self._submit_command_order_recovery_counts(
                    scoped_open_orders
                )
        if self.execution_command_repo is not None:
            sent_stale_before = utc_now() - timedelta(
                seconds=max(float(getattr(self.settings, "execution_command_sent_retry_after_seconds", 0.0) or 0.0), 0.0)
            )
            command_counts = getattr(self.execution_command_repo, "command_counts", None)
            if callable(command_counts):
                counts = command_counts(sent_stale_before=sent_stale_before)
                pending_command_count = int(counts.get("pending_total", 0))
                pending_submit_command_count = int(counts.get("pending_submit", 0))
                pending_cancel_command_count = int(counts.get("pending_cancel", 0))
                sent_stale_command_count = int(counts.get("sent_stale_total", 0))
                sent_stale_submit_command_count = int(counts.get("sent_stale_submit", 0))
                sent_stale_cancel_command_count = int(counts.get("sent_stale_cancel", 0))
            else:
                pending_commands = getattr(self.execution_command_repo, "pending_commands", None)
                if callable(pending_commands):
                    commands = pending_commands(limit=1000, sent_stale_before=sent_stale_before)
                    pending_rows = [row for row in commands if str(row.get("state") or "").upper() == "PENDING"]
                    stale_sent_rows = [row for row in commands if str(row.get("state") or "").upper() == "SENT"]
                    pending_command_count = len(pending_rows)
                    pending_submit_command_count = sum(
                        1 for row in pending_rows if str(row.get("command_type") or "").strip().lower() == "submit"
                    )
                    pending_cancel_command_count = sum(
                        1 for row in pending_rows if str(row.get("command_type") or "").strip().lower() == "cancel"
                    )
                    sent_stale_command_count = len(stale_sent_rows)
                    sent_stale_submit_command_count = sum(
                        1 for row in stale_sent_rows if str(row.get("command_type") or "").strip().lower() == "submit"
                    )
                    sent_stale_cancel_command_count = sum(
                        1 for row in stale_sent_rows if str(row.get("command_type") or "").strip().lower() == "cancel"
                    )

        notes = list(base_status.notes)
        notes.append("phase4_execution_ledger_recovery_enabled")
        independent_recovery_snapshots = list(base_status.independent_recovery_snapshots)
        if independent_recovery_snapshots:
            notes.append(f"independent_recovery_snapshots:{len(independent_recovery_snapshots)}")
            blocked_snapshot_count = sum(
                1 for item in independent_recovery_snapshots if item.recovery_blockers
            )
            if blocked_snapshot_count:
                notes.append(f"independent_recovery_blocked_books:{blocked_snapshot_count}")
        resume_blocked_reasons = list(base_status.resume_blocked_reasons)
        recovery_state = base_status.recovery_state
        recovery_action = base_status.recovery_action
        safe_startup = base_status.safe_startup
        safe_to_trade = base_status.safe_to_trade
        resume_eligible = base_status.resume_eligible
        review_required = base_status.review_required
        rebaseline_available = base_status.rebaseline_available
        reconciliation_classification = None
        combined_only_reduce_required = base_status.only_reduce_required
        combined_only_reduce_reasons = list(base_status.only_reduce_reasons)

        if latest_reconciliation is not None:
            reconciliation_classification = latest_reconciliation.recovery_classification
            notes.append(f"reconciliation_classification:{reconciliation_classification}")
            if latest_reconciliation.only_reduce_required and not latest_reconciliation.resume_blocking:
                recovery_state = "only_reduce"
                safe_to_trade = False
                resume_eligible = False
                notes.append("derivatives_only_reduce_recovery_mode")
                only_reduce_blockers = list(latest_reconciliation.only_reduce_reasons) or ["only_reduce_required"]
                resume_blocked_reasons.extend(
                    blocker for blocker in only_reduce_blockers if blocker not in resume_blocked_reasons
                )
            combined_only_reduce_required = (
                combined_only_reduce_required or bool(latest_reconciliation.only_reduce_required)
            )
            combined_only_reduce_reasons = list(
                dict.fromkeys([*combined_only_reduce_reasons, *latest_reconciliation.only_reduce_reasons])
            )
            if latest_reconciliation.resume_blocking:
                safe_startup = False
                safe_to_trade = False
                resume_eligible = False
                review_required = bool(latest_reconciliation.review_required)
                rebaseline_available = rebaseline_available or review_required or bool(latest_reconciliation.halt_required)
                recovery_state = "resume_blocked" if latest_reconciliation.halt_required else "review_required"
                if latest_reconciliation.halt_required:
                    self.kill_switch.halt(reason="phase4_reconciliation_halt_required")
                    recovery_action = recovery_action or "halted_phase4_reconciliation_halt_required"
                    if "reconciliation_halt_required" not in resume_blocked_reasons:
                        resume_blocked_reasons.append("reconciliation_halt_required")
                elif "operator_rebaseline_required" not in resume_blocked_reasons:
                    resume_blocked_reasons.append("operator_rebaseline_required")

        if stuck_sent_submit_order_count:
            self.kill_switch.halt(reason="phase4_stuck_sent_submit_commands")
            safe_startup = False
            safe_to_trade = False
            resume_eligible = False
            rebaseline_available = True
            recovery_state = "resume_blocked"
            recovery_action = "halted_stuck_sent_submit_commands"
            if "stuck_sent_submit_commands" not in resume_blocked_reasons:
                resume_blocked_reasons.append("stuck_sent_submit_commands")
            notes.append(f"stuck_sent_submit_commands:{stuck_sent_submit_order_count}")

        if sent_stale_command_count:
            notes.append(f"sent_stale_execution_commands:{sent_stale_command_count}")
        if sent_stale_submit_command_count:
            notes.append(f"sent_stale_submit_commands:{sent_stale_submit_command_count}")
        if sent_stale_cancel_command_count:
            notes.append(f"sent_stale_cancel_commands:{sent_stale_cancel_command_count}")

        if pending_command_count:
            self.kill_switch.halt(reason="phase4_pending_execution_commands")
            safe_startup = False
            safe_to_trade = False
            resume_eligible = False
            rebaseline_available = True
            recovery_state = "resume_blocked"
            if recovery_action != "halted_stuck_sent_submit_commands":
                recovery_action = "halted_pending_execution_commands"
            if "pending_execution_commands" not in resume_blocked_reasons:
                resume_blocked_reasons.append("pending_execution_commands")
            notes.append(f"pending_execution_commands:{pending_command_count}")

        if stranded_submit_order_count:
            self.kill_switch.halt(reason="phase4_created_orders_missing_submit_commands")
            safe_startup = False
            safe_to_trade = False
            resume_eligible = False
            rebaseline_available = True
            recovery_state = "resume_blocked"
            recovery_action = recovery_action or "halted_created_orders_missing_submit_commands"
            if "created_orders_missing_submit_commands" not in resume_blocked_reasons:
                resume_blocked_reasons.append("created_orders_missing_submit_commands")
            notes.append(f"created_orders_missing_submit_commands:{stranded_submit_order_count}")

        latest_snapshot = latest_snapshot_for_scope(self.portfolio_repo, self.runtime_scope)
        return base_status.model_copy(
            update={
                "status": "recovered_halted" if self.kill_switch.halted else base_status.status,
                "recovery_source": "execution_ledger",
                "recovered_order_count": recovered_order_count,
                "open_order_count": open_order_count,
                "pending_command_count": pending_command_count,
                "pending_submit_command_count": pending_submit_command_count,
                "pending_cancel_command_count": pending_cancel_command_count,
                "sent_stale_command_count": sent_stale_command_count,
                "sent_stale_submit_command_count": sent_stale_submit_command_count,
                "sent_stale_cancel_command_count": sent_stale_cancel_command_count,
                "stuck_sent_submit_order_count": stuck_sent_submit_order_count,
                "recovered_snapshot_available": latest_snapshot is not None,
                "latest_reconciliation_id": latest_reconciliation.reconciliation_id if latest_reconciliation is not None else base_status.latest_reconciliation_id,
                "latest_reconciliation_severity": latest_reconciliation.severity if latest_reconciliation is not None else base_status.latest_reconciliation_severity,
                "reconciliation_classification": reconciliation_classification,
                "recovery_state": recovery_state,
                "safe_startup": safe_startup and not self.kill_switch.halted,
                "safe_to_trade": safe_to_trade and not self.kill_switch.halted,
                "resume_eligible": resume_eligible and not self.kill_switch.halted,
                "review_required": review_required,
                "only_reduce_required": combined_only_reduce_required,
                "only_reduce_reasons": combined_only_reduce_reasons,
                "unknown_state_details": (
                    list(latest_reconciliation.unknown_state_details)
                    if latest_reconciliation is not None
                    else list(base_status.unknown_state_details)
                ),
                "rebaseline_available": rebaseline_available,
                "halted": self.kill_switch.halted,
                "recovery_action": recovery_action,
                "independent_recovery_snapshots": independent_recovery_snapshots,
                "resume_blocked_reasons": list(dict.fromkeys(resume_blocked_reasons)),
                "notes": list(dict.fromkeys(notes)),
            }
        )

    def _submit_command_order_recovery_counts(self, open_orders: list[dict]) -> tuple[int, int]:
        if self.execution_command_repo is None:
            return 0, 0
        get_by_idempotency_key = getattr(self.execution_command_repo, "get_by_idempotency_key", None)
        if not callable(get_by_idempotency_key):
            return 0, 0
        stranded = 0
        stuck_sent = 0
        for row in open_orders:
            state = str(row.get("state") or "").upper()
            if state not in {"CREATED", "SUBMITTING"}:
                continue
            if row.get("venue_order_id"):
                continue
            lookup_keys = ExecutionOrderService.submit_command_lookup_keys(
                client_order_id=str(row.get("client_order_id") or row.get("order_id") or ""),
                intent_id=str(row.get("intent_id") or ""),
            )
            if not lookup_keys:
                stranded += 1
                continue
            matched_commands = [
                command
                for key in lookup_keys
                if (command := get_by_idempotency_key(key)) is not None
            ]
            if not matched_commands:
                stranded += 1
                continue
            if any(str(command.get("state") or "").upper() == "SENT" for command in matched_commands):
                stuck_sent += 1
        return stranded, stuck_sent
