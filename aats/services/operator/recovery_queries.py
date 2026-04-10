from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.services.operator._parallel import parallel_fetch
from aats.services.runtime_scope import latest_topic_event_for_scope

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class RecoveryQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def recovery_view(self) -> dict[str, Any]:
        cache_key = f"recovery_view:{self.owner._scope_cache_fragment()}"
        return self.owner._cached_ttl(cache_key, 35, self.build_recovery_view)

    def build_recovery_view(self) -> dict[str, Any]:
        latest_state_snapshot_getter = getattr(
            self.owner.runtime.reconciliation_repo,
            "latest_state_snapshot_for_scope",
            None,
        )
        latest_generation_getter = getattr(
            self.owner.runtime.reconciliation_repo,
            "latest_baseline_generation_for_scope",
            None,
        )
        latest_ack_getter = getattr(
            self.owner.runtime.reconciliation_repo,
            "latest_exchange_ack_watermark_for_scope",
            None,
        )
        queries: dict[str, Any] = {
            "latest_reconciliation": self.owner._latest_scoped_reconciliation,
            "latest_baseline": self.owner.latest_account_baseline,
            "latest_rebaseline_action": lambda: self.owner.latest_operator_action("rebaseline"),
            "latest_resume_action": lambda: self.owner.latest_operator_action("resume"),
            "latest_ai_degradation": lambda: latest_topic_event_for_scope(
                self.owner.runtime.event_store,
                topics.AI_DEGRADATION_EVENTS,
                self.owner.state_scope,
            ),
            "latest_ai_shadow_evaluation": lambda: latest_topic_event_for_scope(
                self.owner.runtime.event_store,
                topics.AI_SHADOW_EVALUATIONS,
                self.owner.state_scope,
            ),
        }
        if callable(latest_state_snapshot_getter):
            queries["latest_state_snapshot"] = lambda: latest_state_snapshot_getter(scope=self.owner.state_scope)
        if callable(latest_generation_getter):
            queries["latest_baseline_generation"] = lambda: latest_generation_getter(scope=self.owner.state_scope)
        if callable(latest_ack_getter):
            queries["latest_exchange_ack_watermark"] = lambda: latest_ack_getter(scope=self.owner.state_scope)

        r = parallel_fetch(queries)

        latest_reconciliation = r["latest_reconciliation"]
        latest_state_snapshot = r.get("latest_state_snapshot")
        latest_baseline_generation = r.get("latest_baseline_generation")
        latest_exchange_ack_watermark = r.get("latest_exchange_ack_watermark")
        latest_baseline = r["latest_baseline"]
        latest_rebaseline_action = r["latest_rebaseline_action"]
        latest_resume_action = r["latest_resume_action"]
        latest_ai_degradation = r["latest_ai_degradation"]
        latest_ai_shadow_evaluation = r["latest_ai_shadow_evaluation"]

        base = self.owner.recovery_posture.finalize_status(latest_reconciliation=latest_reconciliation)
        if not self.owner._ai_history_visible():
            latest_ai_degradation = None
            latest_ai_shadow_evaluation = None
        latest_state_snapshot_payload = (
            latest_state_snapshot.model_dump(mode="json")
            if latest_state_snapshot is not None
            else None
        )
        if isinstance(latest_state_snapshot_payload, dict):
            details_json = latest_state_snapshot_payload.get("details_json")
            if isinstance(details_json, dict) and str(details_json.get("source") or "").strip() == "startup_exit_execution_review":
                review_items = details_json.get("review_items")
                if isinstance(review_items, list):
                    details_json["review_items"] = self.owner._enrich_exit_execution_review_items(
                        [dict(item) for item in review_items if isinstance(item, dict)]
                    )
                    latest_state_snapshot_payload["details_json"] = details_json

        base_payload = base.model_dump(mode="json")

        # Stage 5d fix: gateway 进程的 recovery_posture 只有占位符
        # multi_process_role_skip，不反映 execution 进程写入 Postgres 的真实
        # recovery 状态。当检测到占位符状态且 Postgres 中有 execution 写入的
        # ReconciliationStateSnapshot 时，用快照的真实字段覆盖 base_payload。
        if (
            base_payload.get("recovery_state") == "multi_process_role_skip"
            and latest_state_snapshot is not None
        ):
            base_payload["recovery_state"] = latest_state_snapshot.recovery_state
            base_payload["safe_to_trade"] = latest_state_snapshot.safe_to_trade
            base_payload["resume_eligible"] = latest_state_snapshot.resume_eligible
            base_payload["review_required"] = latest_state_snapshot.review_required
            base_payload["halt_required"] = latest_state_snapshot.halt_required
            base_payload["bundle_recovery_required"] = latest_state_snapshot.bundle_recovery_required
            base_payload["only_reduce_required"] = latest_state_snapshot.only_reduce_required
            base_payload["resume_blocked_reasons"] = list(
                latest_state_snapshot.resume_blocked_reasons_json
            )

        base_payload["independent_recovery_snapshots"] = self.owner._independent_recovery_snapshots_view(
            base_payload.get("independent_recovery_snapshots") or []
        )

        return {
            **base_payload,
            "last_rebaseline_action": latest_rebaseline_action,
            "last_resume_action": latest_resume_action,
            "latest_account_baseline": latest_baseline,
            "latest_baseline_generation": (
                latest_baseline_generation.model_dump(mode="json")
                if latest_baseline_generation is not None
                else None
            ),
            "latest_exchange_ack_watermark": (
                latest_exchange_ack_watermark.model_dump(mode="json")
                if latest_exchange_ack_watermark is not None
                else None
            ),
            "latest_state_snapshot": latest_state_snapshot_payload,
            "latest_reconciliation": (
                latest_reconciliation.model_dump(mode="json")
                if latest_reconciliation is not None
                else None
            ),
            "latest_reconciliation_summary": self.owner._reconciliation_mismatch_summary(latest_reconciliation),
            "exit_execution_review_items": self.owner._exit_execution_review_items(),
            "exit_execution_action_history": self.owner._exit_execution_action_history(),
            "latest_ai_degradation": self.owner.payload(latest_ai_degradation),
            "latest_ai_shadow_evaluation": self.owner.payload(latest_ai_shadow_evaluation),
            "ai_runtime": self.owner.ai_runtime(),
        }

    def system_recovery(self) -> dict[str, Any]:
        recovery = self.recovery_view()
        return {
            "recovery": recovery,
            "latest_rebaseline_action": recovery["last_rebaseline_action"],
            "latest_resume_action": recovery["last_resume_action"],
            "latest_account_baseline": recovery["latest_account_baseline"],
        }

    def system_mode(self) -> dict[str, Any]:
        cache_key = f"system_mode:{self.owner._scope_cache_fragment()}"
        return self.owner._cached_ttl(cache_key, 35, self.build_system_mode)

    def build_system_mode(self) -> dict[str, Any]:
        r = parallel_fetch({
            "snapshot": lambda: dict(self.owner.runtime.mode_controller.snapshot()),
            "readiness": self.owner.runtime.execution_adapter.readiness,
            "recovery": self.recovery_view,
            "health_blockers": lambda: list(dict.fromkeys(self.owner.runtime.health_service.execution_blockers())),
            "trial_guard": self.owner.trial_guard,
        })
        snapshot = r["snapshot"]
        readiness = r["readiness"]
        recovery = r["recovery"]
        submit_blocked_reasons = list(
            dict.fromkeys(
                list(snapshot.get("submit_blocked_reasons", []))
                + list(readiness.get("submit_blocked_reasons", []))
            )
        )
        health_blockers = r["health_blockers"]
        recovery_blockers = list(dict.fromkeys(recovery["resume_blocked_reasons"]))
        exchange_submit_allowed = bool(
            readiness.get("exchange_submit_allowed", snapshot.get("exchange_submit_allowed", False))
        )
        execution_blockers = self.owner.recovery_posture.execution_blockers(
            health_blockers=health_blockers,
            recovery_blockers=recovery_blockers,
            submit_blocked_reasons=submit_blocked_reasons,
        )

        snapshot["exchange_submit_allowed"] = exchange_submit_allowed
        snapshot["submit_blocked"] = bool(submit_blocked_reasons) or not exchange_submit_allowed
        snapshot["submit_blocked_reasons"] = submit_blocked_reasons
        snapshot["execution_blocked"] = bool(execution_blockers)
        snapshot["blocked_reason"] = execution_blockers[0] if execution_blockers else None
        snapshot["recovery_state"] = recovery["recovery_state"]
        snapshot["review_required"] = recovery["review_required"]
        snapshot["rebaseline_available"] = recovery["rebaseline_available"]
        snapshot["profile_source"] = self.owner.runtime.runtime_profile_resolution.profile_source
        snapshot["active_profile_revision_id"] = None
        snapshot["pending_profile_revision_id"] = None
        snapshot["restart_required"] = False
        snapshot["trial_guard"] = r["trial_guard"]
        return snapshot
