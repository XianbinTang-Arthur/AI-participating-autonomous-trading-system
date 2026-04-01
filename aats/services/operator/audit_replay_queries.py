from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.operator import ReplayValidationSummary
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.reconciliation_service.replay import ReplayEngine, ReplayResult

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class AuditReplayQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def audit_latest(self) -> dict[str, Any]:
        latest = max(self.owner.runtime.audit_repo.all(), key=lambda item: item.created_at, default=None)
        return {"audit": latest.model_dump(mode="json") if latest is not None else None}

    def audit_detail(self, decision_id: str) -> dict[str, Any]:
        detail = self.owner.decision_view(decision_id)
        context = detail["decision_context"] or {}
        return {
            "audit": detail["audit"],
            "history_length": len(self.owner.runtime.audit_repo.history(decision_id)),
            "hedge_mode_audit": detail.get("hedge_mode_audit"),
            "baseline_switches": self._baseline_switch_history(
                as_of_ts=context.get("as_of_ts"),
                limit=10,
            ),
            "linked_events": {
                "decision_context": detail["decision_context"],
                "baseline_assessment": detail["baseline_assessment"],
                "ai_decision_brief": detail["ai_decision_brief"],
                "ai_assessment": detail["ai_assessment"],
                "ai_shadow_decisions": detail["ai_shadow_decisions"],
                "ai_shadow_evaluations": detail["ai_shadow_evaluations"],
                "position_target": detail["position_target"],
                "policy_decision": detail["policy_decision"],
                "risk_decision": detail["risk_decision"],
                "execution_plan": detail["execution_plan"],
                "order_intents": detail["order_intents"],
                "order_updates": detail["order_updates"],
                "fills": detail["fills"],
                "portfolio_snapshot": detail["portfolio_snapshot"],
                "reconciliations": detail["reconciliations"],
            },
        }

    def replay_status(self) -> dict[str, Any]:
        persisted = self.owner.runtime.event_store.recent_by_topic(topics.REPLAY_VALIDATIONS, limit=10)
        latest = persisted[-1].payload if persisted else (
            self.owner.runtime.replay_validation_history[-1] if self.owner.runtime.replay_validation_history else None
        )
        recent = [item.payload for item in persisted] if persisted else list(self.owner.runtime.replay_validation_history[-10:])
        latest_offset = self.owner.runtime.event_store.latest_replay_offset(
            projection_key="portfolio_replay",
            scope=self.owner.state_scope,
        )
        return {
            "supported": True,
            "healthy": latest is None or latest["divergence_count"] == 0,
            "last_validation": latest,
            "recent_validations": recent,
            "baseline_switches": self._baseline_switch_history(limit=10),
            "event_store_archive": self.owner.runtime.event_store.archive_summary(),
            "latest_replay_offset": None if latest_offset is None else latest_offset.model_dump(mode="json"),
        }

    def replay_validate(self, *, decision_id: str) -> dict[str, Any]:
        engine = ReplayEngine(
            event_store=self.owner.runtime.event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=self.owner.runtime.settings.initial_usdt_balance,
                snapshot_builder=self.owner.runtime.portfolio_service.snapshot_builder,
            ),
            audit_repo=self.owner.runtime.audit_repo,
            portfolio_repo=self.owner.runtime.portfolio_repo,
            reconciliation_repo=self.owner.runtime.reconciliation_repo,
            fill_outcome_repo=self.owner.runtime.fill_outcome_repo,
            funding_fee_repo=getattr(self.owner.runtime, "funding_fee_repo", None),
            sleeve_pnl_repo=getattr(self.owner.runtime, "sleeve_pnl_repo", None),
            scope=self.owner.state_scope,
        )
        result = engine.replay(decision_id=decision_id)
        detail = self.owner.decision_view(decision_id)
        baseline = detail.get("baseline_assessment") or {}
        context = detail.get("decision_context") or {}
        profile_state = self.owner.strategy_profiles.snapshot().get("activation", {})
        overlay_parent_exposure_summary = self.owner._overlay_parent_exposure_summary_from_payload(
            detail.get("decision_outcome")
        ) or self.owner._overlay_parent_exposure_summary_from_payload(detail.get("position_target"))
        independent_adaptive_summary = self.owner._independent_adaptive_summary_from_payload(
            detail.get("decision_outcome")
        ) or self.owner._independent_adaptive_summary_from_payload(detail.get("position_target"))
        summary = self._replay_summary(
            result,
            symbol=context.get("symbol"),
            regime=baseline.get("regime"),
            active_profile_id=profile_state.get("active_profile_id"),
            margin_mode=(detail.get("position_target") or {}).get("margin_mode"),
            independent_adaptive_summary=independent_adaptive_summary,
            overlay_parent_exposure_summary=overlay_parent_exposure_summary,
        )
        self.owner._append_event(
            topic=topics.REPLAY_VALIDATIONS,
            key=decision_id or "all",
            payload_model=ReplayValidationSummary(**summary),
        )
        self.owner.runtime.replay_validation_history.append(summary)
        self.owner.runtime.replay_validation_history[:] = self.owner.runtime.replay_validation_history[-20:]
        return summary

    def replay_recent_validations(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        persisted = self.owner.runtime.event_store.by_topic(topics.REPLAY_VALIDATIONS)
        if persisted:
            rows = [item.payload for item in reversed(persisted)]
        else:
            rows = list(reversed(self.owner.runtime.replay_validation_history))
        return self.owner._paginate_rows(rows, limit=limit, offset=offset, key="validations")

    def _replay_summary(
        self,
        result: ReplayResult,
        *,
        symbol: str | None = None,
        regime: str | None = None,
        active_profile_id: str | None = None,
        margin_mode: str | None = None,
        independent_adaptive_summary: dict[str, Any] | None = None,
        overlay_parent_exposure_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        replayed_event_count = max(result.replayed_event_count, 1)
        portfolio_issue_count = len(result.portfolio_issues)
        decision_chain_issue_count = len(result.decision_chain_issues)
        execution_chain_issue_count = len(result.execution_chain_issues)
        audit_issue_count = len(result.audit_issues)
        baseline_switch_issue_count = len(result.baseline_switch_issues)
        independent_expected_vs_realized_summary = self.owner._independent_expected_vs_realized_summary(
            decision_ids={result.selected_decision_id} if result.selected_decision_id else None,
            limit=1,
        )
        total_issues = (
            portfolio_issue_count
            + decision_chain_issue_count
            + execution_chain_issue_count
            + audit_issue_count
            + baseline_switch_issue_count
        )
        return {
            "validated_at": utc_now(),
            "decision_id": result.selected_decision_id,
            "symbol": symbol,
            "regime": regime,
            "active_profile_id": active_profile_id,
            "product_type": self.owner.runtime.settings.trading_product_type,
            "margin_mode": margin_mode or self.owner.runtime.settings.margin_mode,
            "allowed_symbols": tuple(self.owner.runtime.settings.allowed_symbols),
            "replayed_event_count": result.replayed_event_count,
            "stored_snapshot_count": result.stored_snapshot_count,
            "divergence_count": result.divergence_count,
            "portfolio_issues": result.portfolio_issues,
            "portfolio_issue_count": portfolio_issue_count,
            "decision_chain_issues": result.decision_chain_issues,
            "decision_chain_issue_count": decision_chain_issue_count,
            "execution_chain_issues": result.execution_chain_issues,
            "execution_chain_issue_count": execution_chain_issue_count,
            "audit_issues": result.audit_issues,
            "audit_issue_count": audit_issue_count,
            "baseline_switch_count": result.baseline_switch_count,
            "baseline_switch_issues": result.baseline_switch_issues,
            "baseline_switch_issue_count": baseline_switch_issue_count,
            "incremental_window_start_at": result.incremental_window_start_at,
            "baseline_generation_id": result.baseline_generation_id,
            "exchange_ack_watermark_id": result.exchange_ack_watermark_id,
            "replay_offset_id": result.replay_offset_id,
            "divergence_density": round(result.divergence_count / replayed_event_count, 6),
            "chain_health_score": round(max(0.0, 1.0 - (total_issues / replayed_event_count)), 6),
            "healthy": result.divergence_count == 0,
            "independent_expected_vs_realized_summary": independent_expected_vs_realized_summary,
            "independent_adaptive_summary": independent_adaptive_summary,
            "overlay_parent_exposure_summary": overlay_parent_exposure_summary,
        }

    def _baseline_switch_history(
        self,
        *,
        as_of_ts: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if as_of_ts is None:
            events = self.owner.runtime.event_store.recent_by_topic(topics.ACCOUNT_BASELINES, limit=limit)
        else:
            events = self.owner.runtime.event_store.by_topic(topics.ACCOUNT_BASELINES)
        if as_of_ts is not None:
            events = [event for event in events if event.payload.get("imported_at") <= as_of_ts]
        rows = []
        for event in events[-limit:]:
            payload = dict(event.payload)
            payload["_event_id"] = event.event_id
            rows.append(payload)
        return rows
