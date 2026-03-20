from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.schemas.common import utc_now
from aats.services.runtime_scope import latest_topic_event_for_scope

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class RuntimeQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def ai_runtime(self) -> dict[str, Any]:
        status = dict(self.owner.runtime.ai_service.status())
        legacy_modes = {
            "configured_operating_mode": status.get("configured_operating_mode"),
            "effective_operating_mode": status.get("effective_operating_mode"),
        }
        status["configured_operating_mode"] = status.get(
            "canonical_configured_operating_mode",
            status.get("configured_operating_mode"),
        )
        status["effective_operating_mode"] = status.get(
            "canonical_effective_operating_mode",
            status.get("effective_operating_mode"),
        )
        status["legacy_modes"] = legacy_modes
        return status

    def ai_performance_overview(self) -> dict[str, Any]:
        return self.owner._ai_performance_overview_impl()

    def ai_overview(self) -> dict[str, Any]:
        latest = self.ai_latest()
        shadow_latest = self.ai_shadow_latest()
        latest_decision_id = self.owner.latest_decision_id()
        latest_decision_detail = self.owner.decision_view(latest_decision_id) if latest_decision_id is not None else None
        latest_degradation = latest_topic_event_for_scope(
            self.owner.runtime.event_store,
            topics.AI_DEGRADATION_EVENTS,
            self.owner.state_scope,
        )
        if not self.owner._ai_history_visible():
            latest_degradation = None
        return {
            "runtime": self.ai_runtime(),
            "latest_brief": latest.get("brief"),
            "latest_assessment": latest.get("assessment"),
            "latest_baseline_reference": None if latest_decision_detail is None else latest_decision_detail.get("baseline_reference"),
            "latest_ai_decision_intent": None if latest_decision_detail is None else latest_decision_detail.get("ai_decision_intent"),
            "latest_profile_control_decision": None if latest_decision_detail is None else latest_decision_detail.get("profile_control_decision"),
            "latest_decision_outcome": None if latest_decision_detail is None else latest_decision_detail.get("decision_outcome"),
            "latest_shadow_decision": shadow_latest.get("shadow_decision"),
            "latest_degradation": self.owner.payload(latest_degradation),
            "shadow_summary": self.owner._ai_shadow_summary(),
            "performance_windows": self.owner._ai_shadow_performance_windows(),
            "latest_performance_report": self.owner._latest_ai_performance_report_payload(),
            "performance_view": self.ai_performance_overview(),
            "downgrade_state": self.owner._ai_downgrade_state(),
            "latest_execution_suggestion": None if latest_decision_detail is None else latest_decision_detail.get("ai_execution_suggestion"),
        }

    def ai_latest(self) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {
                "brief": None,
                "assessment": None,
                "execution_suggestion": None,
                "baseline_reference": None,
                "ai_decision_intent": None,
                "profile_control_decision": None,
                "decision_outcome": None,
            }
        brief = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.AI_DECISION_BRIEFS, self.owner.state_scope)
        assessment = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.AI_ASSESSMENTS, self.owner.state_scope)
        execution_plan = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.EXECUTION_PLANS, self.owner.state_scope)
        order_intent = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.ORDER_INTENTS, self.owner.state_scope)
        latest_decision_id = self.owner.latest_decision_id()
        latest_decision_detail = self.owner.decision_view(latest_decision_id) if latest_decision_id is not None else None
        assessment_payload = self.owner.payload(assessment)
        execution_plan_payload = self.owner.payload(execution_plan)
        order_intent_payload = self.owner.payload(order_intent)
        return {
            "brief": self.owner.payload(brief),
            "assessment": assessment_payload,
            "baseline_reference": None if latest_decision_detail is None else latest_decision_detail.get("baseline_reference"),
            "ai_decision_intent": None if latest_decision_detail is None else latest_decision_detail.get("ai_decision_intent"),
            "profile_control_decision": None if latest_decision_detail is None else latest_decision_detail.get("profile_control_decision"),
            "decision_outcome": None if latest_decision_detail is None else latest_decision_detail.get("decision_outcome"),
            "execution_suggestion": self.owner._ai_execution_suggestion_summary(
                ai_assessment=assessment_payload,
                execution_plan=execution_plan_payload,
                latest_order_intent=order_intent_payload,
            ),
        }

    def ai_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"assessments": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self.owner.runtime.event_store.by_topic_scoped(topics.AI_ASSESSMENTS, scope=self.owner.state_scope)
        rows = list(reversed(rows))
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="assessments",
            serializer=self.owner.payload,
        )

    def ai_shadow_latest(self) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"shadow_decision": None}
        shadow = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.AI_SHADOW_DECISIONS, self.owner.state_scope)
        return {"shadow_decision": self.owner.payload(shadow)}

    def ai_shadow_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"shadow_decisions": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self.owner.runtime.event_store.by_topic_scoped(topics.AI_SHADOW_DECISIONS, scope=self.owner.state_scope)
        rows = list(reversed(rows))
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="shadow_decisions",
            serializer=self.owner.payload,
        )

    def ai_shadow_evaluations(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"evaluations": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self.owner.runtime.event_store.by_topic_scoped(topics.AI_SHADOW_EVALUATIONS, scope=self.owner.state_scope)
        rows = list(reversed(rows))
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="evaluations",
            serializer=self.owner.payload,
        )

    def ai_performance_reports(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"reports": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self.owner._recent_ai_performance_report_events()
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="reports",
            serializer=self.owner.payload,
        )

    def recovery_view(self) -> dict[str, Any]:
        return self.owner._cached("recovery_view", self.owner._build_recovery_view)

    def system_recovery(self) -> dict[str, Any]:
        recovery = self.recovery_view()
        return {
            "recovery": recovery,
            "latest_rebaseline_action": recovery["last_rebaseline_action"],
            "latest_resume_action": recovery["last_resume_action"],
            "latest_account_baseline": recovery["latest_account_baseline"],
        }

    def system_mode(self) -> dict[str, Any]:
        return self.owner._cached("system_mode", self.owner._build_system_mode)

    def blockers(self) -> list[dict[str, Any]]:
        return self.owner._build_blockers()

    def blocker_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = [item.payload for item in reversed(self.owner.runtime.event_store.by_topic(topics.BLOCKER_SNAPSHOTS))]
        return self.owner._paginate_rows(rows, limit=limit, offset=offset, key="history")

    def system_health(self) -> dict[str, Any]:
        return self.owner._build_system_health()

    def system_runtime(self) -> dict[str, Any]:
        return self.owner._build_system_runtime()

    def metrics(self) -> dict[str, Any]:
        return self.owner._build_metrics()
