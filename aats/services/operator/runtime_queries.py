from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.operator import AuthSource, OperatorActionRecord, OperatorRole
from aats.schemas.common import utc_now
from aats.services.runtime_scope import latest_topic_event_for_scope

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class RuntimeQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def ai_runtime(self) -> dict[str, Any]:
        return self.owner.runtime.ai_service.status()

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
            "latest_takeover": latest.get("takeover"),
            "latest_shadow_decision": shadow_latest.get("shadow_decision"),
            "latest_degradation": self.owner.payload(latest_degradation),
            "takeover_summary": self.owner._ai_takeover_summary(),
            "shadow_summary": self.owner._ai_shadow_summary(),
            "performance_windows": self.owner._ai_shadow_performance_windows(),
            "latest_performance_report": self.owner._latest_ai_performance_report_payload(),
            "performance_view": self.ai_performance_overview(),
            "downgrade_state": self.owner._ai_downgrade_state(),
            "latest_execution_suggestion": None if latest_decision_detail is None else latest_decision_detail.get("ai_execution_suggestion"),
        }

    def ai_latest(self) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"brief": None, "assessment": None, "takeover": None, "execution_suggestion": None}
        brief = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.AI_DECISION_BRIEFS, self.owner.state_scope)
        assessment = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.AI_ASSESSMENTS, self.owner.state_scope)
        takeover = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.AI_TAKEOVER_DECISIONS, self.owner.state_scope)
        execution_plan = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.EXECUTION_PLANS, self.owner.state_scope)
        order_intent = latest_topic_event_for_scope(self.owner.runtime.event_store, topics.ORDER_INTENTS, self.owner.state_scope)
        assessment_payload = self.owner.payload(assessment)
        execution_plan_payload = self.owner.payload(execution_plan)
        order_intent_payload = self.owner.payload(order_intent)
        return {
            "brief": self.owner.payload(brief),
            "assessment": assessment_payload,
            "takeover": self.owner.payload(takeover),
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

    def ai_takeovers_recent(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"takeovers": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        rows = self.owner._recent_ai_takeover_events()
        return self.owner._paginate_rows(
            rows,
            limit=limit,
            offset=offset,
            key="takeovers",
            serializer=lambda envelope: {
                **(self.owner.payload(envelope) or {}),
                "direction_disagreement": (
                    envelope.payload.get("baseline_direction") != envelope.payload.get("ai_direction")
                    if isinstance(envelope.payload, dict)
                    else False
                ),
            },
        )

    async def evaluate_ai_shadow(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
    ) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"evaluation": None, "status": "baseline_only_ai_history_hidden"}
        evaluation, created = self.owner.runtime.ai_service.evaluate_shadow_window(
            limit=self.owner.runtime.settings.ai_shadow_evaluation_window
        )
        if evaluation is None:
            return {"evaluation": None, "status": "no_shadow_decisions"}
        if created:
            await publish_model(
                bus=self.owner.runtime.bus,
                topic=topics.AI_SHADOW_EVALUATIONS,
                key=evaluation.symbol,
                payload_model=evaluation,
                source_component="operator_api",
            )
            status = "evaluation_created"
        else:
            status = "evaluation_reused"
        self.owner._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="ai_shadow",
            payload_model=OperatorActionRecord(
                action="ai_shadow_evaluate",
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
                reason="operator_ai_shadow_evaluate",
                status=status,
                details={
                    "evaluation_id": evaluation.evaluation_id,
                    "window_size": evaluation.summary.get("window_size"),
                    "override_rate": evaluation.summary.get("override_rate"),
                },
            ),
        )
        return {"evaluation": evaluation.model_dump(mode="json"), "status": status}

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
        return self.owner._build_system_mode()

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
