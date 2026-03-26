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

    def _control_plane_consistency(self) -> dict[str, Any]:
        phase5_enabled = bool(self.owner._phase5_control_plane_enabled())
        financial_convergence_enabled = bool(self.owner.runtime.settings.financial_convergence_mode_enabled)
        warnings: list[str] = []
        status = "converged"
        if phase5_enabled and not financial_convergence_enabled:
            status = "transitional"
            warnings.append("phase5_control_plane_running_without_financial_convergence")
        if not phase5_enabled and self.owner.runtime.settings.portfolio_ledger_truth_enabled:
            status = "transitional"
            warnings.append("portfolio_ledger_truth_enabled_without_phase5_control_plane")
        return {
            "status": status,
            "warning_codes": warnings,
            "phase5_enabled": phase5_enabled,
            "financial_convergence_mode_enabled": financial_convergence_enabled,
        }

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
        settings = self.owner.runtime.settings
        strategy_profile_state = self.owner.strategy_profiles.snapshot().get("activation", {})
        auto_control_configured = settings.strategy_profile_auto_control_configured
        auto_control_enabled = bool(strategy_profile_state.get("auto_switch_enabled", auto_control_configured))
        status["strategy_profile_auto_control_configured"] = settings.strategy_profile_auto_control_configured
        status["strategy_profile_auto_control_effective"] = auto_control_enabled
        status["strategy_profile_control_configured_mode"] = "auto" if auto_control_configured else "manual"
        status["strategy_profile_control_effective_mode"] = "auto" if auto_control_enabled else "manual"
        if auto_control_enabled and auto_control_configured:
            reason = "configured_auto"
        elif auto_control_enabled and not auto_control_configured:
            reason = "operator_enabled_auto"
        elif not auto_control_enabled and auto_control_configured:
            reason = "operator_enabled_manual"
        else:
            reason = "configured_manual"
        status["strategy_profile_auto_control_reason"] = reason
        status["operating_mode_source"] = (
            "manual_selection"
            if status.get("manual_override_active")
            else "configured"
        )
        status["legacy_modes"] = legacy_modes
        return status

    def ai_performance_overview(self) -> dict[str, Any]:
        return self.owner._ai_performance_overview_impl()

    def ai_overview(self) -> dict[str, Any]:
        latest = self.owner.ai_latest()
        shadow_latest = self.owner.ai_shadow_latest()
        latest_degradation = latest_topic_event_for_scope(
            self.owner.runtime.event_store,
            topics.AI_DEGRADATION_EVENTS,
            self.owner.state_scope,
        )
        if not self.owner._ai_history_visible():
            latest_degradation = None
        return {
            "runtime": self.owner.ai_runtime(),
            "latest_brief": latest.get("brief"),
            "latest_assessment": latest.get("assessment"),
            "latest_baseline_reference": latest.get("baseline_reference"),
            "latest_ai_decision_intent": latest.get("ai_decision_intent"),
            "latest_profile_control_decision": latest.get("profile_control_decision"),
            "latest_decision_outcome": latest.get("decision_outcome"),
            "latest_shadow_decision": shadow_latest.get("shadow_decision"),
            "latest_degradation": self.owner.payload(latest_degradation),
            "shadow_summary": self.owner._ai_shadow_summary(),
            "performance_windows": self.owner._ai_shadow_performance_windows(),
            "latest_performance_report": self.owner._latest_ai_performance_report_payload(),
            "performance_view": self.ai_performance_overview(),
            "downgrade_state": self.owner._ai_downgrade_state(),
            "latest_execution_suggestion": latest.get("execution_suggestion"),
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

    def system_health(self) -> dict[str, Any]:
        return self.build_system_health()

    def build_system_health(self) -> dict[str, Any]:
        snapshot = self.owner.runtime.health_service.snapshot()
        mode_snapshot = self.owner.system_mode()
        recovery = self.owner.recovery_view()
        market = self.owner.runtime.market_gateway.status()
        account = self.owner.account_service_status()
        execution = self.owner.runtime.execution_adapter.readiness()
        phase1_shadow = self.owner.phase1_shadow()
        derivatives_live_guard = self.owner.derivatives_live_guard()
        latest_reconciliation = self.owner._latest_scoped_reconciliation()
        latest_portfolio = self.owner._latest_scoped_snapshot()
        blockers = self.owner.blockers()
        account_baseline = self.owner.latest_account_baseline()
        reconciliation_component = next(
            (component for component in snapshot.components if component.component == "reconciliation"),
            None,
        )
        warnings = [
            {
                "component": component.component,
                "detail": component.detail,
                "blockers": component.blockers,
            }
            for component in snapshot.components
            if component.status == "warn"
        ]
        if phase1_shadow["status"] in {"degraded", "lagging"}:
            warnings.append(
                {
                    "component": "phase1_shadow",
                    "detail": phase1_shadow["summary"],
                    "blockers": [],
                }
            )
        if self.owner.runtime.kill_switch.halted:
            runtime_state = "halted"
        elif any(item["affects_execution"] for item in blockers):
            runtime_state = "blocked"
        elif warnings:
            runtime_state = "degraded"
        else:
            runtime_state = "healthy"
        self.owner._persist_blocker_snapshot(
            source="system_health",
            runtime_state=runtime_state,
            mode_snapshot=mode_snapshot,
            blockers=blockers,
        )
        return {
            "overall_status": snapshot.status,
            "runtime_state": runtime_state,
            "operating_state": snapshot.operating_state,
            "mode": snapshot.mode,
            "runtime_profile": self.owner.runtime.runtime_profile.to_dict(),
            "environment_capabilities": self.owner.runtime.environment_capabilities.to_dict(),
            "policy_profile": self.owner.runtime.policy_profile.to_dict(),
            "recovery_policy": self.owner.runtime.recovery_policy.to_dict(),
            "profile_control": self.owner.runtime_profile_snapshot(),
            "halted": self.owner.runtime.kill_switch.halted,
            "blockers": blockers,
            "warnings": warnings,
            "execution_blocked": mode_snapshot["execution_blocked"],
            "submit_blocked": mode_snapshot["submit_blocked"],
            "submit_blocked_reasons": mode_snapshot["submit_blocked_reasons"],
            "subsystems": {
                "market_data": market,
                "account_state": account,
                "execution_adapter": execution,
                "reconciliation": {
                    "ready": reconciliation_component.status == "ok" if reconciliation_component is not None else False,
                    "fresh": reconciliation_component.fresh if reconciliation_component is not None else False,
                    "last_update_ts": (
                        reconciliation_component.last_update_ts
                        if reconciliation_component is not None
                        else (latest_reconciliation.as_of_ts if latest_reconciliation else None)
                    ),
                    "severity": latest_reconciliation.severity if latest_reconciliation else None,
                    "halt_required": latest_reconciliation.halt_required if latest_reconciliation else False,
                    "blockers": (
                        list(reconciliation_component.blockers)
                        if reconciliation_component is not None
                        else []
                    ),
                },
                "storage": {
                    "ready": True,
                    "fresh": True,
                    "detail": self.owner.runtime.settings.storage_mode,
                },
                "phase1_shadow": phase1_shadow,
                "derivatives_live_guard": derivatives_live_guard,
                "audit_replay": {
                    "ready": True,
                    "fresh": bool(self.owner.runtime.replay_validation_history),
                    "audit_record_count": self.owner.runtime.audit_repo.count(),
                    "last_replay_validation": (
                        self.owner.runtime.replay_validation_history[-1]
                        if self.owner.runtime.replay_validation_history
                        else None
                    ),
                },
            },
            "freshness": {
                "market_fresh": market.get("fresh", False),
                "account_fresh": account.get("fresh", False),
                "reconciliation_fresh": (
                    reconciliation_component.fresh if reconciliation_component is not None else False
                ),
            },
            "last_success_timestamps": {
                "market": market.get("last_update_ts"),
                "account": account.get("last_update_ts"),
                "portfolio": latest_portfolio.snapshot_ts if latest_portfolio else None,
                "reconciliation": latest_reconciliation.as_of_ts if latest_reconciliation else None,
            },
            "recovery": recovery,
            "recovery_state": recovery["recovery_state"],
            "review_required": recovery["review_required"],
            "rebaseline_available": recovery["rebaseline_available"],
            "account_baseline": account_baseline,
            "mode_contract": mode_snapshot,
        }

    def system_runtime(self) -> dict[str, Any]:
        return self.build_system_runtime()

    def build_system_runtime(self) -> dict[str, Any]:
        latest_decision = self.owner.runtime.event_store.latest(topics.DECISION_CONTEXTS)
        latest_fill = self.owner.latest_fill()
        latest_reconciliation = self.owner._latest_scoped_reconciliation()
        account_baseline = self.owner.latest_account_baseline()
        account_snapshot = self.owner.latest_exchange_snapshot()
        recovery = self.owner.recovery_view()
        guarded_live_preflight = self.owner.guarded_live_preflight()
        guarded_live_run_packet = self.owner.guarded_live_run_packet()
        event_store_archive = self.owner.runtime.event_store.archive_summary()
        latest_replay_offset = self.owner.runtime.event_store.latest_replay_offset(
            projection_key="portfolio_replay",
            scope=self.owner.state_scope,
        )
        control_plane_consistency = self._control_plane_consistency()
        now = utc_now()
        return {
            "runtime_profile": self.owner.runtime.runtime_profile.to_dict(),
            "environment_capabilities": self.owner.runtime.environment_capabilities.to_dict(),
            "policy_profile": self.owner.runtime.policy_profile.to_dict(),
            "recovery_policy": self.owner.runtime.recovery_policy.to_dict(),
            "profile_source": self.owner.runtime.runtime_profile_resolution.profile_source,
            "startup_profile": self.owner.runtime.settings.startup_profile,
            "env_template_profile": self.owner.runtime.settings.env_template_profile,
            "config_profile": self.owner.runtime.settings.config_profile,
            "account_configuration": (
                account_snapshot.account_configuration.model_dump(mode="json")
                if account_snapshot is not None and account_snapshot.account_configuration is not None
                else None
            ),
            "risk_snapshot": (
                account_snapshot.risk_snapshot.model_dump(mode="json")
                if account_snapshot is not None and account_snapshot.risk_snapshot is not None
                else None
            ),
            "primary_instrument_rule": (
                next(
                    (
                        item.model_dump(mode="json")
                        for item in account_snapshot.instruments
                        if item.symbol == self.owner.runtime.settings.default_symbol
                    ),
                    None,
                )
                if account_snapshot is not None
                else None
            ),
            "runtime_profile_control": self.owner.runtime_profile_snapshot(),
            "strategy_runtime_summary": self.owner.strategy_runtime(limit=5).get("summary"),
            "symbols": [self.owner.runtime.settings.default_symbol],
            "enabled_timeframes": list(self.owner.runtime.settings.enabled_decision_timeframes),
            "decision_cadence": {
                "decision_min_interval_seconds_15m": self.owner.runtime.settings.decision_min_interval_seconds_15m,
                "decision_min_interval_seconds_1h": self.owner.runtime.settings.decision_min_interval_seconds_1h,
                "decision_min_price_move_bps": self.owner.runtime.settings.decision_min_price_move_bps,
                "decision_min_momentum_delta": self.owner.runtime.settings.decision_min_momentum_delta,
                "max_decisions_per_minute": self.owner.runtime.settings.max_decisions_per_minute,
            },
            "strategy_family_active": self.owner.runtime.settings.strategy_family_active,
            "storage_mode": self.owner.runtime.settings.storage_mode,
            "operator_auth_enabled": self.owner.runtime.settings.operator_auth_enabled,
            "operator_auth": {
                "auth_enabled": self.owner.runtime.settings.operator_auth_enabled,
                "session_enabled": self.owner.runtime.settings.operator_session_configured,
                "database_backed": self.owner.runtime.database_runtime is not None,
                "stored_user_count": self.owner.runtime.operator_repo.count() if hasattr(self.owner.runtime, "operator_repo") else 0,
                "api_key_compatibility_enabled": bool(
                    self.owner.runtime.settings.operator_read_api_key or self.owner.runtime.settings.operator_write_api_key
                ),
                "unsafe_write_without_auth": self.owner.runtime.settings.operator_unsafe_write_without_auth,
                "phase5_hardened": self.owner.runtime.settings.operator_control_plane_execution_ledger_enabled,
            },
            "startup_timestamp": self.owner.runtime.started_at,
            "uptime_seconds": max((now - self.owner.runtime.started_at).total_seconds(), 0.0),
            "last_decision_timestamp": latest_decision.event_timestamp if latest_decision else None,
            "last_fill_timestamp": (
                latest_fill.get("ingestion_timestamp") if isinstance(latest_fill, dict) else latest_fill.ingestion_timestamp
            ) if latest_fill else None,
            "last_reconciliation_timestamp": latest_reconciliation.as_of_ts if latest_reconciliation else None,
            "recovery": {
                "recovery_state": recovery["recovery_state"],
                "review_required": recovery["review_required"],
                "rebaseline_available": recovery["rebaseline_available"],
                "resume_eligible": recovery["resume_eligible"],
                "safe_to_trade": recovery["safe_to_trade"],
            },
            "baseline_takeover": {
                "status": self.owner.runtime.recovery_status.baseline_status,
                "baseline_imported": self.owner.runtime.recovery_status.baseline_imported,
                "baseline_imported_at": self.owner.runtime.recovery_status.baseline_imported_at,
                "baseline_source": self.owner.runtime.recovery_status.baseline_source,
                "baseline_kind": account_baseline.get("baseline_kind") if account_baseline is not None else None,
                "requires_operator_review": self.owner.runtime.recovery_status.baseline_requires_operator_review,
                "safe_for_automatic_continuation": self.owner.runtime.recovery_status.baseline_safe_for_automatic_continuation,
                "balance_count": self.owner.runtime.recovery_status.baseline_balance_count,
                "position_count": self.owner.runtime.recovery_status.baseline_position_count,
                "open_order_count": self.owner.runtime.recovery_status.baseline_open_order_count,
                "fill_count": self.owner.runtime.recovery_status.baseline_fill_count,
                "event_ref": self.owner.runtime.recovery_status.baseline_event_ref,
                "last_rebaseline_event_ref": self.owner.runtime.recovery_status.last_rebaseline_event_ref,
                "last_rebaseline_at": self.owner.runtime.recovery_status.last_rebaseline_at,
                "snapshot": account_baseline,
            },
            "control_plane": {
                "phase5_enabled": control_plane_consistency["phase5_enabled"],
                "order_truth_source": "execution_order_repo" if control_plane_consistency["phase5_enabled"] else "execution_repo",
                "fill_truth_source": "execution_fill_repo_v2" if control_plane_consistency["phase5_enabled"] else "execution_repo",
                "balance_truth_source": "ledger_accounts" if control_plane_consistency["phase5_enabled"] else "portfolio_snapshot",
                "legacy_layer_authoritative": not control_plane_consistency["phase5_enabled"],
                "auth_hardened": self.owner.runtime.settings.operator_control_plane_execution_ledger_enabled,
                "financial_convergence_mode_enabled": control_plane_consistency["financial_convergence_mode_enabled"],
                "truth_consistency_status": control_plane_consistency["status"],
                "consistency_warning_codes": control_plane_consistency["warning_codes"],
            },
            "trial_guard": self.owner.trial_guard(),
            "margin_buffer_overview": self.owner.margin_buffer_risk(),
            "derivatives_live_guard": self.owner.derivatives_live_guard(),
            "guarded_live_preflight": guarded_live_preflight,
            "guarded_live_run_packet_summary": {
                "status": guarded_live_run_packet.get("status"),
                "summary": guarded_live_run_packet.get("summary"),
                "summary_metrics": guarded_live_run_packet.get("summary_metrics"),
                "operator_actions": guarded_live_run_packet.get("operator_actions"),
            },
            "event_store_archive": event_store_archive,
            "replay_offsets": {
                "portfolio_replay": None if latest_replay_offset is None else latest_replay_offset.model_dump(mode="json"),
            },
        }

    def metrics(self) -> dict[str, Any]:
        return self.owner._build_metrics()

    def phase1_shadow_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.owner._build_phase1_shadow_history(limit=limit, offset=offset)
