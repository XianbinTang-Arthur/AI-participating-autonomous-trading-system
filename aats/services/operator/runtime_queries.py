from __future__ import annotations

import asyncio
import threading
from time import monotonic
from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.system import ComponentHealth, SystemHealthSnapshot
from aats.services.execution_engine.okx_account import derivatives_position_mode_contract
from aats.services.operator._parallel import parallel_fetch
from aats.services.operator.ui_capabilities import ui_operating_mode_override_policy
from aats.services.runtime_scope import latest_topic_event_for_scope

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


_AI_RUNTIME_AUTHORITATIVE_CACHE_TTL_SECONDS = 20.0
_AI_RUNTIME_AUTHORITATIVE_CACHE: dict[tuple[int, int, int], tuple[float, dict[str, Any]]] = {}
_AI_RUNTIME_AUTHORITATIVE_INFLIGHT: dict[tuple[int, int, int], tuple[int, asyncio.Task[dict[str, Any]]]] = {}
_AI_RUNTIME_AUTHORITATIVE_GENERATION: dict[tuple[int, int], int] = {}


class RuntimeQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def _authoritative_ai_runtime_cache_key(self) -> tuple[int, int, int]:
        runtime = self.owner.runtime
        client = getattr(runtime, "ai_command_client", None)
        return (
            id(runtime),
            id(client),
            id(asyncio.get_running_loop()),
        )

    @staticmethod
    def invalidate_authoritative_ai_runtime_cache(runtime: Any | None = None) -> None:
        if runtime is None:
            _AI_RUNTIME_AUTHORITATIVE_CACHE.clear()
            _AI_RUNTIME_AUTHORITATIVE_INFLIGHT.clear()
            _AI_RUNTIME_AUTHORITATIVE_GENERATION.clear()
            return

        runtime_id = id(runtime)
        client_id = id(getattr(runtime, "ai_command_client", None))
        generation_keys = {(runtime_id, client_id)}
        generation_keys.update(
            (cache_key[0], cache_key[1])
            for cache_key in _AI_RUNTIME_AUTHORITATIVE_CACHE
            if cache_key[0] == runtime_id
        )
        generation_keys.update(
            (cache_key[0], cache_key[1])
            for cache_key in _AI_RUNTIME_AUTHORITATIVE_INFLIGHT
            if cache_key[0] == runtime_id
        )
        for generation_key in generation_keys:
            _AI_RUNTIME_AUTHORITATIVE_GENERATION[generation_key] = (
                _AI_RUNTIME_AUTHORITATIVE_GENERATION.get(generation_key, 0) + 1
            )
        for cache_key in list(_AI_RUNTIME_AUTHORITATIVE_CACHE):
            if cache_key[0] == runtime_id:
                _AI_RUNTIME_AUTHORITATIVE_CACHE.pop(cache_key, None)
        for cache_key in list(_AI_RUNTIME_AUTHORITATIVE_INFLIGHT):
            if cache_key[0] == runtime_id:
                _AI_RUNTIME_AUTHORITATIVE_INFLIGHT.pop(cache_key, None)

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
        # Stage 7 修复（gateway-only role /system/health 500）：
        # ai_service 在 process_role=gateway/market/execution 下未装配（见
        # aats/bootstrap/config.py:_SLICE_REQUIRED_ROLES）。原本直接 .status()
        # 触发 AttributeError，向上传播到 build_recovery_view → build_system_mode
        # → build_system_health，让 /system/health /system/recovery /system/mode
        # 三个 CORE_SPECS endpoint 全部 500，UI 整体崩。
        #
        # 这里返回一个稳定的 "not_loaded" stub：本进程没有 AI 推理切片，所以
        # 没有 provider/degraded/recovery_probe 等真实状态。下游 (recovery_view
        # 内嵌 + UI ai-view) 都用 .get() / `||` 安全访问，stub 字段保持原 dict
        # 形态即可被透明消费。完整诊断由后续 cross-process query aggregator 提供
        # （Stage 7 之后的 task #3）。
        ai_service = getattr(self.owner.runtime, "ai_service", None)
        if ai_service is None:
            settings = self.owner.runtime.settings
            return {
                "configured_operating_mode": None,
                "canonical_configured_operating_mode": None,
                "effective_operating_mode": None,
                "canonical_effective_operating_mode": None,
                "manual_override_mode": None,
                "manual_override_active": False,
                "manual_override_freeze_until": None,
                "manual_override_default_freeze_seconds": getattr(
                    settings, "ai_manual_operating_mode_override_freeze_seconds", None
                ),
                "review_resolution": None,
                "provider": "not_loaded",
                "configured": False,
                "provider_ready": False,
                "degraded": False,
                "provider_degraded": False,
                "outcome_review_required": False,
                "auto_downgrade_active": False,
                "outcome_auto_downgrade_active": False,
                "degradation_reason": None,
                "outcome_degradation_reason": None,
                "recovery_probe_after": None,
                "recovery_probe_ready": False,
                "consecutive_failures": 0,
                "consecutive_successes": 0,
                "outcome_bad_window_streak": 0,
                "provider_state": "not_loaded",
                "outcome_state": "not_loaded",
                "last_provider_degraded_at": None,
                "last_provider_recovered_at": None,
                "last_outcome_degraded_at": None,
                "last_outcome_recovered_at": None,
                "shadow_mode_enabled": False,
                "execution_suggestion_mode": None,
                "failure_budget": {
                    "degrade_after_failures": 0,
                    "recover_after_successes": 0,
                    "remaining_failures_until_degrade": 0,
                    "remaining_successes_until_recover": 0,
                },
                "outcome_policy": {
                    "bad_window_threshold": 0,
                    "warmup_evaluations": 0,
                    "min_trade_count": 0,
                    "remaining_bad_windows_until_review": 0,
                    "max_fee_ratio_delta": None,
                    "max_churn_ratio_delta": None,
                },
                "recent_assessment_count": 0,
                "recent_shadow_evaluation_count": 0,
                "recent_execution_suggestion_count": 0,
                "recent_fallback_ratio": 0.0,
                "recent_timeout_count": 0,
                "recent_invalid_output_count": 0,
                "legacy_modes": {
                    "configured_operating_mode": None,
                    "effective_operating_mode": None,
                },
                "strategy_profile_auto_control_configured": False,
                "strategy_profile_auto_control_effective": False,
                "strategy_profile_control_configured_mode": "manual",
                "strategy_profile_control_effective_mode": "manual",
                "strategy_profile_auto_control_reason": "ai_service_not_loaded",
                "operating_mode_source": "ai_service_not_loaded",
                "ui_operating_mode_override": ui_operating_mode_override_policy(),
                "ai_service_loaded": False,
                "process_role": getattr(settings, "process_role", None),
            }
        status = dict(ai_service.status())
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
        activation_status = getattr(self.owner.strategy_profiles, "activation_status", None)
        if activation_status is None:
            strategy_profile_state = self.owner.strategy_profiles.snapshot().get("activation", {})
        else:
            strategy_profile_state = activation_status()
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
        status["ui_operating_mode_override"] = ui_operating_mode_override_policy()
        # Stage 7：与 stub 路径对称的 loaded 标记，UI/审计可统一判断 ai 子系统是否在本进程
        status["ai_service_loaded"] = True
        status["process_role"] = getattr(settings, "process_role", None)
        return status

    async def ai_runtime_authoritative(self) -> dict[str, Any]:
        """Return authoritative AI runtime status for HTTP read paths.

        In the 4-process topology the gateway does not load ``ai_service``.
        The synchronous ``ai_runtime()`` method must keep returning a stable
        local stub for health/recovery callers. The public ``/ai/runtime`` read
        path can afford an async bridge call, so when gateway has an
        ``ai_command_client`` we ask the decision process for its local status.
        """
        if getattr(self.owner.runtime, "ai_service", None) is not None:
            status = self.ai_runtime()
            status.setdefault("ai_runtime_source", "local")
            return status

        client = getattr(self.owner.runtime, "ai_command_client", None)
        if client is None:
            status = self.ai_runtime()
            status.setdefault("ai_runtime_source", "local_stub")
            return status

        status = await self._remote_ai_runtime_authoritative_cached(client)
        status["ui_operating_mode_override"] = ui_operating_mode_override_policy()
        status.setdefault("ai_runtime_source", "remote_decision")
        status.setdefault(
            "queried_from_process_role",
            getattr(self.owner.runtime.settings, "process_role", None),
        )
        return status

    async def _remote_ai_runtime_authoritative_cached(self, client: Any) -> dict[str, Any]:
        cache_key = self._authoritative_ai_runtime_cache_key()
        generation_key = (cache_key[0], cache_key[1])
        generation = _AI_RUNTIME_AUTHORITATIVE_GENERATION.get(generation_key, 0)
        now = monotonic()
        cached = _AI_RUNTIME_AUTHORITATIVE_CACHE.get(cache_key)
        if cached is not None:
            expires_at, payload = cached
            if expires_at > now:
                return dict(payload)
            _AI_RUNTIME_AUTHORITATIVE_CACHE.pop(cache_key, None)

        inflight = _AI_RUNTIME_AUTHORITATIVE_INFLIGHT.get(cache_key)
        if inflight is not None and inflight[0] == generation and inflight[1].done():
            result = self._complete_authoritative_ai_runtime_inflight(
                cache_key,
                generation_key,
                inflight,
            )
            if result is not None:
                return result
            inflight = None

        if inflight is None or inflight[0] != generation:
            task = asyncio.create_task(
                self._remote_ai_runtime_authoritative_uncached(client),
            )
            inflight = (generation, task)
            _AI_RUNTIME_AUTHORITATIVE_INFLIGHT[cache_key] = inflight

        inflight_generation, task = inflight
        try:
            result = dict(await asyncio.shield(task))
        finally:
            if task.done() and _AI_RUNTIME_AUTHORITATIVE_INFLIGHT.get(cache_key) == inflight:
                _AI_RUNTIME_AUTHORITATIVE_INFLIGHT.pop(cache_key, None)

        if _AI_RUNTIME_AUTHORITATIVE_GENERATION.get(generation_key, 0) == inflight_generation:
            _AI_RUNTIME_AUTHORITATIVE_CACHE[cache_key] = (
                monotonic() + _AI_RUNTIME_AUTHORITATIVE_CACHE_TTL_SECONDS,
                dict(result),
            )
        return result

    @staticmethod
    async def _remote_ai_runtime_authoritative_uncached(client: Any) -> dict[str, Any]:
        return dict(
            await client.invoke(
                command="ai_runtime_status",
                payload={},
            )
        )

    @staticmethod
    def _complete_authoritative_ai_runtime_inflight(
        cache_key: tuple[int, int, int],
        generation_key: tuple[int, int],
        inflight: tuple[int, asyncio.Task[dict[str, Any]]],
    ) -> dict[str, Any] | None:
        inflight_generation, task = inflight
        if _AI_RUNTIME_AUTHORITATIVE_INFLIGHT.get(cache_key) == inflight:
            _AI_RUNTIME_AUTHORITATIVE_INFLIGHT.pop(cache_key, None)
        try:
            result = dict(task.result())
        except asyncio.CancelledError:
            return None
        except Exception:
            return None
        if _AI_RUNTIME_AUTHORITATIVE_GENERATION.get(generation_key, 0) == inflight_generation:
            _AI_RUNTIME_AUTHORITATIVE_CACHE[cache_key] = (
                monotonic() + _AI_RUNTIME_AUTHORITATIVE_CACHE_TTL_SECONDS,
                dict(result),
            )
        return result

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
        return self.owner._paginate_recent_scoped_topic(
            topics.AI_ASSESSMENTS,
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
        return self.owner._paginate_recent_scoped_topic(
            topics.AI_SHADOW_DECISIONS,
            limit=limit,
            offset=offset,
            key="shadow_decisions",
            serializer=self.owner.payload,
        )

    def ai_shadow_evaluations(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"evaluations": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        return self.owner._paginate_recent_scoped_topic(
            topics.AI_SHADOW_EVALUATIONS,
            limit=limit,
            offset=offset,
            key="evaluations",
            serializer=self.owner.payload,
        )

    def ai_performance_reports(self, *, limit: int, offset: int) -> dict[str, Any]:
        if not self.owner._ai_history_visible():
            return {"reports": [], "limit": limit, "offset": offset, "total_available": 0, "has_more": False}
        return self.owner._paginate_recent_scoped_topic(
            topics.AI_PERFORMANCE_REPORTS,
            limit=limit,
            offset=offset,
            key="reports",
            serializer=self.owner.payload,
        )

    def system_health(self) -> dict[str, Any]:
        return self.build_system_health()

    def system_health_dashboard(self) -> dict[str, Any]:
        return self.build_system_health(dashboard_summary_only=True)

    def build_system_health(self, *, dashboard_summary_only: bool = False) -> dict[str, Any]:
        if dashboard_summary_only:
            r = parallel_fetch({
                "mode_controller_snapshot": lambda: dict(self.owner.runtime.mode_controller.snapshot()),
                "recovery": self.owner.recovery_view_dashboard,
                "market": self.owner.runtime.market_gateway.status,
                "account": self.owner.account_service_status,
                "phase1_shadow": self.owner.phase1_shadow,
                "derivatives_live_guard": self.owner.derivatives_live_guard,
                "trial_guard": self.owner.trial_guard,
            })
        else:
            r = parallel_fetch({
                "snapshot": self.owner.runtime.health_service.snapshot,
                "mode_snapshot": self.owner.system_mode,
                "recovery": self.owner.recovery_view,
                "market": self.owner.runtime.market_gateway.status,
                "account": self.owner.account_service_status,
                "execution": self.owner.runtime.execution_adapter.readiness,
                "phase1_shadow": self.owner.phase1_shadow,
                "derivatives_live_guard": self.owner.derivatives_live_guard,
                "latest_reconciliation": self.owner._latest_scoped_reconciliation,
                "latest_portfolio": self.owner._latest_scoped_snapshot,
                "blockers": self.owner.blockers,
                "account_baseline": self.owner.latest_account_baseline,
            })
        recovery = r["recovery"]
        market = r["market"]
        account = r["account"]
        if dashboard_summary_only:
            execution = self._dashboard_execution_readiness(
                mode_controller_snapshot=r["mode_controller_snapshot"],
                account_status=account,
            )
        else:
            execution = r["execution"]
        phase1_shadow = r["phase1_shadow"]
        derivatives_live_guard = r["derivatives_live_guard"]
        latest_reconciliation = None if dashboard_summary_only else r["latest_reconciliation"]
        latest_portfolio = None if dashboard_summary_only else r["latest_portfolio"]
        if dashboard_summary_only:
            snapshot = self._dashboard_health_snapshot(
                mode_controller_snapshot=r["mode_controller_snapshot"],
                market_status=market,
                account_status=account,
                recovery=recovery,
                phase1_shadow=phase1_shadow,
                derivatives_live_guard=derivatives_live_guard,
            )
        else:
            snapshot = r["snapshot"]
        if dashboard_summary_only:
            account_baseline = (
                recovery.get("latest_account_baseline")
                if isinstance(recovery, dict)
                else None
            )
        else:
            account_baseline = r["account_baseline"]
        if dashboard_summary_only:
            mode_snapshot = self.owner.recovery_queries.build_system_mode(
                recovery=recovery,
                snapshot=r["mode_controller_snapshot"],
                readiness=execution,
                health_blockers=list(getattr(snapshot, "blockers", []) or []),
                trial_guard=r["trial_guard"],
            )
            blocker_control = self.owner.blocker_control_service.execution_blocker_summary(
                recovery=recovery,
                submit_blocked_reasons=list(mode_snapshot.get("submit_blocked_reasons") or []),
                health_snapshot=snapshot,
            )
            blockers = [
                item
                for item in list(blocker_control.get("blockers") or [])
                if isinstance(item, dict)
            ]
        else:
            mode_snapshot = r["mode_snapshot"]
            blockers = r["blockers"]
        reconciliation_component = next(
            (component for component in snapshot.components if component.component == "reconciliation"),
            None,
        )
        reconciliation_last_update_ts = (
            reconciliation_component.last_update_ts
            if reconciliation_component is not None
            else (latest_reconciliation.as_of_ts if latest_reconciliation else None)
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
        if not dashboard_summary_only:
            threading.Thread(
                target=self.owner._persist_blocker_snapshot,
                kwargs=dict(
                    source="system_health",
                    runtime_state=runtime_state,
                    mode_snapshot=mode_snapshot,
                    blockers=blockers,
                ),
                daemon=True,
            ).start()
        payload = {
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
                    "last_update_ts": reconciliation_last_update_ts,
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
                    "audit_record_count": (
                        None
                        if dashboard_summary_only
                        else self.owner._cached_ttl(
                            f"audit_record_count:{self.owner._scope_cache_fragment()}",
                            300,
                            self.owner.runtime.audit_repo.count,
                        )
                    ),
                    "audit_record_count_status": (
                        "deferred_from_dashboard_summary"
                        if dashboard_summary_only
                        else "available"
                    ),
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
                "reconciliation": reconciliation_last_update_ts,
            },
            "recovery": recovery,
            "recovery_state": recovery["recovery_state"],
            "review_required": recovery["review_required"],
            "rebaseline_available": recovery["rebaseline_available"],
            "account_baseline": account_baseline,
            "mode_contract": mode_snapshot,
        }
        if dashboard_summary_only:
            payload["dashboard_summary_only"] = True
            payload["truth_source"] = "runtime_health_dashboard_summary"
            payload["deferred_sections"] = [
                "health_service.snapshot",
                "execution_adapter.readiness",
                "latest_portfolio",
                "latest_reconciliation",
            ]
        return payload

    def _dashboard_health_snapshot(
        self,
        *,
        mode_controller_snapshot: dict[str, Any],
        market_status: dict[str, Any],
        account_status: dict[str, Any],
        recovery: dict[str, Any],
        phase1_shadow: dict[str, Any],
        derivatives_live_guard: dict[str, Any],
    ) -> SystemHealthSnapshot:
        components = [
            self._dashboard_component_from_status("market_data", market_status),
            self._dashboard_component_from_status("account_state", account_status),
            self._dashboard_reconciliation_component(recovery),
            self._dashboard_component_from_status("phase1_shadow", phase1_shadow),
            self._dashboard_component_from_status("derivatives_live_guard", derivatives_live_guard),
        ]
        blockers = [blocker for component in components for blocker in component.blockers]
        if self.owner.runtime.kill_switch.halted:
            blockers.append("kill_switch_active")
        status = "blocked" if blockers else "warn" if any(component.status == "warn" for component in components) else "ok"
        mode = mode_controller_snapshot.get("mode")
        if mode is None:
            mode = getattr(self.owner.runtime.settings, "mode", "unknown")
        operating_state = mode_controller_snapshot.get("operating_state")
        if operating_state is None:
            operating_state_getter = getattr(self.owner.runtime.mode_controller, "operating_state", None)
            operating_state = operating_state_getter() if callable(operating_state_getter) else "guarded_live_enabled"
        return SystemHealthSnapshot(
            mode=str(mode),
            operating_state=operating_state,
            status=status,
            halted=bool(self.owner.runtime.kill_switch.halted),
            blockers=list(dict.fromkeys(blockers)),
            components=components,
        )

    @staticmethod
    def _dashboard_component_from_status(component: str, status: dict[str, Any]) -> ComponentHealth:
        blockers = list(status.get("blockers", []) or [])
        connected = bool(status.get("connected", True))
        ready = bool(status.get("ready", status.get("fresh", True)))
        detail = status.get("detail") or status.get("last_error") or status.get("summary")
        return ComponentHealth(
            component=component,
            status="ok" if ready and connected else "warn" if connected else "blocked",
            connected=connected,
            fresh=bool(status.get("fresh", True)),
            last_update_ts=status.get("last_update_ts"),
            detail=str(detail) if detail is not None else None,
            blockers=blockers,
        )

    def _dashboard_reconciliation_component(self, recovery: dict[str, Any]) -> ComponentHealth:
        blockers = list(dict.fromkeys(recovery.get("resume_blocked_reasons", []) or []))
        if recovery.get("halt_required"):
            blockers.append("reconciliation_halt_required")
        if recovery.get("review_required"):
            blockers.append("operator_rebaseline_required")
        recovered = bool(recovery.get("recovered_reconciliation_available", False))
        if not recovered:
            blockers.append("reconciliation_status_deferred")
        safe_to_trade = bool(recovery.get("safe_to_trade", False))
        status = "blocked" if blockers else "ok" if safe_to_trade else "warn"
        return ComponentHealth(
            component="reconciliation",
            status=status,
            connected=True,
            fresh=recovered,
            last_update_ts=None,
            detail=str(recovery.get("recovery_state") or "unknown"),
            blockers=list(dict.fromkeys(blockers)),
        )

    def _dashboard_execution_readiness(
        self,
        *,
        mode_controller_snapshot: dict[str, Any],
        account_status: dict[str, Any],
    ) -> dict[str, Any]:
        settings = self.owner.runtime.settings
        credentials_configured = bool(account_status.get("credentials_configured", account_status.get("ready", True)))
        account_enabled = bool(account_status.get("enabled", True))
        exchange_submit_allowed = bool(mode_controller_snapshot.get("exchange_submit_allowed", False))
        return {
            "ready": credentials_configured and account_enabled,
            "backend": self.owner.runtime.execution_adapter.__class__.__name__,
            "mode": mode_controller_snapshot.get("mode", getattr(settings, "mode", None)),
            "execution_mode": "dashboard_summary",
            "live_submit_enabled": getattr(settings, "live_submit_enabled", None),
            "guarded_execution_dry_run": getattr(settings, "guarded_execution_dry_run", None),
            "okx_simulated_trading": getattr(settings, "okx_simulated_trading", None),
            "exchange_submit_allowed": exchange_submit_allowed,
            "submit_blocked_reasons": list(mode_controller_snapshot.get("submit_blocked_reasons") or []),
            "account_status": account_status,
            "truth_source": "mode_controller_plus_account_status_dashboard_summary",
        }

    def system_runtime(self) -> dict[str, Any]:
        return self.build_system_runtime()

    def system_runtime_dashboard(self) -> dict[str, Any]:
        return self.build_system_runtime(dashboard_summary_only=True)

    def guarded_live_run_packet_summary(
        self,
        *,
        preflight: dict[str, Any],
        live_guard: dict[str, Any],
        trial_guard: dict[str, Any],
        margin_buffer: dict[str, Any],
        recovery: dict[str, Any],
        blocker_control: dict[str, Any],
    ) -> dict[str, Any]:
        cached_packet_getter = getattr(self.owner, "cached_guarded_live_run_packet", None)
        cached_packet = cached_packet_getter() if callable(cached_packet_getter) else None
        if isinstance(cached_packet, dict):
            return {
                "status": cached_packet.get("status"),
                "summary": cached_packet.get("summary"),
                "summary_metrics": cached_packet.get("summary_metrics"),
                "operator_actions": cached_packet.get("operator_actions"),
                "forward_validation_summary": cached_packet.get("forward_validation_summary"),
                "summary_source": "cached_full_packet",
                "full_packet_cached": True,
                "deferred_sections": [],
            }
        return self._lightweight_guarded_live_run_packet_summary(
            preflight=preflight,
            live_guard=live_guard,
            trial_guard=trial_guard,
            margin_buffer=margin_buffer,
            recovery=recovery,
            blocker_control=blocker_control,
        )

    def _lightweight_guarded_live_run_packet_summary(
        self,
        *,
        preflight: dict[str, Any],
        live_guard: dict[str, Any],
        trial_guard: dict[str, Any],
        margin_buffer: dict[str, Any],
        recovery: dict[str, Any],
        blocker_control: dict[str, Any],
    ) -> dict[str, Any]:
        preflight_status = preflight.get("status")
        margin_status = margin_buffer.get("status")
        auto_halt_required = bool(live_guard.get("auto_halt_required"))
        only_reduce_required = bool(live_guard.get("only_reduce_required"))
        trial_breached = trial_guard.get("status") == "breached"
        blocker_items = blocker_control.get("blockers")
        execution_blockers = []
        if isinstance(blocker_items, list):
            execution_blockers = [
                item
                for item in blocker_items
                if isinstance(item, dict) and item.get("affects_execution") is not False
            ]

        status = "ready"
        if auto_halt_required or trial_breached or execution_blockers:
            status = "critical"
        elif (
            preflight_status in {"warning", "fail"}
            or only_reduce_required
            or margin_status in {"warning", "critical"}
            or not recovery.get("safe_to_trade")
        ):
            status = "warning"

        summary_map = {
            "ready": "当前运行包状态健康，可以继续保持小资金受控运行。",
            "warning": "当前运行包存在明显风险或约束，必须保持 only-reduce / 小资金 / 人工盯盘。",
            "critical": "当前运行包已经触发自动停机或存在硬阻断，不应继续自动运行。",
        }
        operator_actions: list[str] = []
        if preflight_status == "fail":
            operator_actions.append("先处理启盘前自检里的硬失败项，再讨论继续实盘。")
        if auto_halt_required:
            operator_actions.append("当前已经进入自动停机区间，先减仓并核对交易所保证金状态。")
        elif only_reduce_required:
            operator_actions.append("当前只允许继续减仓或平仓，先把保证金缓冲拉回健康区间。")
        if trial_breached:
            operator_actions.append("试盘守护已经触发暂停，先复盘最近收益、滑点和资金费拖累。")
        if execution_blockers:
            operator_actions.append("当前仍有执行阻断，先把阻断项处理干净。")
        if margin_status in {"warning", "critical"} and not (auto_halt_required or only_reduce_required):
            operator_actions.append("保证金缓冲已经偏紧，先确认仓位、杠杆和强平距离。")
        if not recovery.get("safe_to_trade"):
            operator_actions.append("恢复状态仍不允许自动交易，先处理恢复阻断或人工复核。")

        current_margin = margin_buffer.get("current") if isinstance(margin_buffer.get("current"), dict) else {}
        liquidation = margin_buffer.get("liquidation") if isinstance(margin_buffer.get("liquidation"), dict) else {}
        return {
            "status": status,
            "summary": summary_map[status],
            "summary_metrics": {
                "launch_ready": preflight.get("launch_ready"),
                "safe_to_trade": recovery.get("safe_to_trade"),
                "execution_blocker_count": len(execution_blockers),
                "current_initial_margin_usage_fraction": current_margin.get("initial_margin_usage_fraction"),
                "nearest_liquidation_gap_ratio": liquidation.get("nearest_liquidation_gap_ratio"),
                "combined_net_realized_pnl": None,
                "funding_fee_net_pnl": None,
                "open_position_count": None,
                "current_open_order_count": None,
            },
            "operator_actions": list(dict.fromkeys(operator_actions)),
            "forward_validation_summary": {
                "summary": {
                    "verdict": "deferred",
                    "summary": "前向验证未在首屏主请求中同步计算，请打开完整运行包查看。",
                    "reasons": ["forward_validation_deferred_from_runtime_summary"],
                },
                "latest_period": None,
            },
            "summary_source": "runtime_lightweight",
            "full_packet_cached": False,
            "active_blockers": execution_blockers,
            "deferred_sections": [
                "forward_validation",
                "execution_blockers",
                "positions",
                "account",
            ],
        }

    @staticmethod
    def _dashboard_submit_blocked_reasons_from_context(
        *,
        mode_snapshot: dict[str, Any],
        execution_readiness: dict[str, Any],
    ) -> list[str]:
        return list(
            dict.fromkeys(
                list(mode_snapshot.get("submit_blocked_reasons", []) or [])
                + list(execution_readiness.get("submit_blocked_reasons", []) or [])
            )
        )

    def guarded_live_preflight_runtime_summary(
        self,
        *,
        recovery: dict[str, Any],
        account_status: dict[str, Any],
        margin_buffer: dict[str, Any],
        live_guard: dict[str, Any],
        trial_guard: dict[str, Any],
        submit_blocked_reasons: list[str],
        blocker_control: dict[str, Any],
    ) -> dict[str, Any]:
        settings = self.owner.runtime.settings
        if not (
            getattr(settings, "mode", None) == "guarded_live"
            and getattr(settings, "trading_product_type", None) == "derivatives"
        ):
            return {
                "generated_at": utc_now(),
                "status": "not_applicable",
                "launch_ready": False,
                "summary": "当前不是合约 guarded_live 运行线，不需要执行这份启盘前摘要。",
                "counts": {"pass": 0, "warn": 0, "fail": 0},
                "checks": [],
                "operator_actions": ["先切到合约 guarded_live 运行线，再执行启盘前检查。"],
                "dashboard_summary_only": True,
                "truth_source": "runtime_context_guarded_live_preflight_summary",
                "deferred_sections": ["full_guarded_live_preflight_check_matrix"],
            }

        blocker_items = blocker_control.get("blockers")
        active_blockers = [
            item
            for item in (blocker_items if isinstance(blocker_items, list) else [])
            if isinstance(item, dict) and item.get("affects_execution") is not False
        ]
        policy_profile = self.owner.runtime.policy_profile
        real_money_blocked = bool(
            getattr(policy_profile, "real_money_submission_structurally_blocked", False)
        )
        margin_status = str(margin_buffer.get("status") or "unknown")
        trial_status = str(trial_guard.get("status") or "unknown")

        checks = [
            {
                "check_id": "runtime_contract_dashboard_summary",
                "category": "runtime_contract",
                "label": "运行线必须是合约 guarded_live",
                "status": "pass",
                "detail": "当前运行线为合约 guarded_live。",
                "required": True,
                "observed": {
                    "mode": getattr(settings, "mode", None),
                    "trading_product_type": getattr(settings, "trading_product_type", None),
                },
            },
            {
                "check_id": "real_money_route_ready_dashboard_summary",
                "category": "execution_route",
                "label": "真实资金报单路径必须不再处于结构性阻断",
                "status": "fail" if real_money_blocked else "pass",
                "detail": (
                    "当前执行线路仍然被结构性阻断。"
                    if real_money_blocked
                    else "当前执行线路没有结构性真实资金阻断。"
                ),
                "required": True,
                "observed": {
                    "submit_blocked_reasons": submit_blocked_reasons,
                    "real_money_submission_structurally_blocked": real_money_blocked,
                },
            },
            {
                "check_id": "account_status_dashboard_summary",
                "category": "account_readiness",
                "label": "账户服务必须可用且新鲜",
                "status": (
                    "pass"
                    if account_status.get("connected")
                    and account_status.get("fresh")
                    and account_status.get("ready")
                    else "fail"
                ),
                "detail": (
                    "账户服务状态可用于首屏判断。"
                    if account_status.get("connected")
                    and account_status.get("fresh")
                    and account_status.get("ready")
                    else "账户服务状态仍未满足首屏启盘判断。"
                ),
                "required": True,
                "observed": {
                    "connected": account_status.get("connected"),
                    "fresh": account_status.get("fresh"),
                    "ready": account_status.get("ready"),
                    "blockers": account_status.get("blockers"),
                },
            },
            {
                "check_id": "no_active_execution_blockers_dashboard_summary",
                "category": "recovery_and_blockers",
                "label": "当前不能存在活动中的执行阻断",
                "status": "pass" if not active_blockers else "fail",
                "detail": (
                    "当前没有活动中的执行阻断。"
                    if not active_blockers
                    else "当前仍有执行阻断，启盘前必须先处理。"
                ),
                "required": True,
                "observed": [item.get("blocker") for item in active_blockers],
            },
            {
                "check_id": "recovery_state_safe_dashboard_summary",
                "category": "recovery_and_blockers",
                "label": "恢复状态必须允许安全继续交易",
                "status": (
                    "pass"
                    if recovery.get("safe_to_trade") and not recovery.get("review_required")
                    else "fail"
                ),
                "detail": (
                    "当前恢复状态允许继续自动交易。"
                    if recovery.get("safe_to_trade") and not recovery.get("review_required")
                    else "当前恢复状态仍不允许安全继续交易。"
                ),
                "required": True,
                "observed": {
                    "recovery_state": recovery.get("recovery_state"),
                    "review_required": recovery.get("review_required"),
                    "resume_blocked_reasons": recovery.get("resume_blocked_reasons"),
                },
            },
            {
                "check_id": "margin_buffer_safe_dashboard_summary",
                "category": "risk_buffer",
                "label": "当前保证金缓冲不能处于 critical 或 only-reduce",
                "status": (
                    "pass"
                    if margin_status == "healthy" and not live_guard.get("only_reduce_required")
                    else "fail"
                ),
                "detail": (
                    "当前保证金缓冲处于健康区间。"
                    if margin_status == "healthy" and not live_guard.get("only_reduce_required")
                    else "当前保证金缓冲或 only-reduce 状态不允许启盘。"
                ),
                "required": True,
                "observed": {
                    "margin_buffer_status": margin_status,
                    "only_reduce_required": live_guard.get("only_reduce_required"),
                    "auto_halt_required": live_guard.get("auto_halt_required"),
                },
            },
            {
                "check_id": "trial_guard_status_dashboard_summary",
                "category": "trial_guard",
                "label": "试盘守护不能处于 breached",
                "status": (
                    "fail"
                    if trial_status == "breached"
                    else "warn"
                    if trial_status in {"disabled", "not_configured", "warming_up"}
                    else "pass"
                ),
                "detail": (
                    "当前试盘守护已经进入监控中。"
                    if trial_status == "monitoring"
                    else "当前试盘守护已经触发自动停机。"
                    if trial_status == "breached"
                    else "当前试盘守护还没有形成稳定样本。"
                ),
                "required": False,
                "observed": {"status": trial_status},
            },
        ]
        fail_count = sum(1 for item in checks if item["status"] == "fail")
        warn_count = sum(1 for item in checks if item["status"] == "warn")
        pass_count = sum(1 for item in checks if item["status"] == "pass")
        required_failures = [
            item for item in checks if item["required"] and item["status"] == "fail"
        ]
        status = "fail" if required_failures else "warning" if warn_count else "ready"
        summary = {
            "ready": "当前合约 guarded_live 首屏预检摘要已通过。",
            "warning": "当前首屏预检摘要没有硬失败，但仍有需要人工确认的告警项。",
            "fail": "当前首屏预检摘要仍有硬失败项。",
        }[status]
        operator_actions = [
            item["detail"]
            for item in checks
            if item["status"] in {"fail", "warn"}
        ]
        return {
            "generated_at": utc_now(),
            "status": status,
            "launch_ready": not required_failures,
            "summary": summary,
            "counts": {"pass": pass_count, "warn": warn_count, "fail": fail_count},
            "checks": checks,
            "operator_actions": list(dict.fromkeys(operator_actions)),
            "dashboard_summary_only": True,
            "truth_source": "runtime_context_guarded_live_preflight_summary",
            "deferred_sections": ["full_guarded_live_preflight_check_matrix"],
        }

    def build_system_runtime(self, *, dashboard_summary_only: bool = False) -> dict[str, Any]:
        # ── 阶段 0：预热 strategy_runtime 的 30s TTL 缓存 ──────────
        # strategy_runtime 内部用 ThreadPoolExecutor(5) 并行发 8 个 DB 查询。
        # 如果放进下面的 parallel_fetch（17 路并发），会与其他 16 个查询同时
        # 竞争 DB 连接池（pool_size=10），导致冷启动从 ~5s 膨胀到 30s+。
        # 先单独计算并填充缓存，parallel_fetch 中的 lambda 直接命中热缓存。
        if not dashboard_summary_only:
            self.owner.strategy_runtime(limit=5)

        # ── 阶段 1：并行获取所有独立子查询 ──────────────────────
        if not dashboard_summary_only:
            def strategy_runtime_summary_loader() -> dict[str, Any] | None:
                return self.owner.strategy_runtime(limit=5).get("summary")

        runtime_loaders = {
            "latest_decision": lambda: self.owner.runtime.event_store.latest(topics.DECISION_CONTEXTS),
            "latest_fill": self.owner.latest_fill,
            "account_baseline": self.owner.latest_account_baseline,
            "account_snapshot": self.owner.latest_exchange_snapshot,
            "recovery": self.owner.recovery_view_dashboard if dashboard_summary_only else self.owner.recovery_view,
            "control_plane_consistency": self._control_plane_consistency,
            "account_status": self.owner.account_service_status,
            "runtime_profile_control": self.owner.runtime_profile_snapshot,
            "trial_guard": self.owner.trial_guard,
            "margin_buffer_overview": self.owner.margin_buffer_risk,
            "derivatives_live_guard": self.owner.derivatives_live_guard,
        }
        if dashboard_summary_only:
            runtime_loaders["mode_controller_snapshot"] = lambda: dict(self.owner.runtime.mode_controller.snapshot())
        else:
            runtime_loaders["latest_reconciliation"] = self.owner._latest_scoped_reconciliation
            runtime_loaders["guarded_live_preflight"] = self.owner.guarded_live_preflight
            runtime_loaders["strategy_runtime_summary"] = strategy_runtime_summary_loader
            runtime_loaders["blocker_control"] = self.owner.blocker_control
        r = parallel_fetch(runtime_loaders)

        # ── 阶段 2：依赖性计算（需要上面的结果） ─────────────────
        latest_decision = r["latest_decision"]
        latest_fill = r["latest_fill"]
        latest_reconciliation = None if dashboard_summary_only else r["latest_reconciliation"]
        account_baseline = r["account_baseline"]
        account_snapshot = r["account_snapshot"]
        recovery = r["recovery"]
        control_plane_consistency = r["control_plane_consistency"]
        account_status = r["account_status"]
        if dashboard_summary_only:
            execution_readiness = self._dashboard_execution_readiness(
                mode_controller_snapshot=r["mode_controller_snapshot"],
                account_status=account_status,
            )
            submit_blocked_reasons = self._dashboard_submit_blocked_reasons_from_context(
                mode_snapshot=r["mode_controller_snapshot"],
                execution_readiness=execution_readiness,
            )
            blocker_control = self.owner.blocker_control_service.execution_blocker_summary(
                recovery=recovery,
                submit_blocked_reasons=submit_blocked_reasons,
            )
            guarded_live_preflight = self.guarded_live_preflight_runtime_summary(
                recovery=recovery,
                account_status=account_status,
                margin_buffer=r["margin_buffer_overview"],
                live_guard=r["derivatives_live_guard"],
                trial_guard=r["trial_guard"],
                submit_blocked_reasons=submit_blocked_reasons,
                blocker_control=blocker_control,
            )
            strategy_runtime_summary = {
                "status": "deferred",
                "summary": "策略运行摘要已从 runtime 首屏拆出，请读取 strategyRuntime panel。",
                "truth_source": "/strategy/runtime",
                "deferred_from_dashboard_summary": True,
            }
        else:
            blocker_control = r["blocker_control"]
            guarded_live_preflight = r["guarded_live_preflight"]
            strategy_runtime_summary = r["strategy_runtime_summary"]
        guarded_live_run_packet_summary = self.guarded_live_run_packet_summary(
            preflight=guarded_live_preflight,
            live_guard=r["derivatives_live_guard"],
            trial_guard=r["trial_guard"],
            margin_buffer=r["margin_buffer_overview"],
            recovery=recovery,
            blocker_control=blocker_control,
        )
        position_mode_contract = account_status.get("position_mode_contract") or derivatives_position_mode_contract(
            settings=self.owner.runtime.settings,
            snapshot=account_snapshot,
        )
        now = utc_now()

        # ── 阶段 3：组装返回 ──────────────────────────────────
        payload = {
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
            "account_position_mode_contract": position_mode_contract,
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
            "runtime_profile_control": r["runtime_profile_control"],
            "strategy_runtime_summary": strategy_runtime_summary,
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
                "order_truth_source": (
                    "execution_order_repo"
                    if control_plane_consistency["phase5_enabled"]
                    else self.owner._execution_read_truth_source()
                ),
                "fill_truth_source": (
                    "execution_fill_repo_v2"
                    if control_plane_consistency["phase5_enabled"]
                    else self.owner._execution_read_truth_source()
                ),
                "balance_truth_source": "ledger_accounts" if control_plane_consistency["phase5_enabled"] else "portfolio_snapshot",
                "legacy_layer_authoritative": (
                    not control_plane_consistency["phase5_enabled"]
                    and self.owner._execution_read_truth_source() == "execution_repo"
                ),
                "auth_hardened": self.owner.runtime.settings.operator_control_plane_execution_ledger_enabled,
                "financial_convergence_mode_enabled": control_plane_consistency["financial_convergence_mode_enabled"],
                "truth_consistency_status": control_plane_consistency["status"],
                "consistency_warning_codes": control_plane_consistency["warning_codes"],
            },
            "trial_guard": r["trial_guard"],
            "margin_buffer_overview": r["margin_buffer_overview"],
            "derivatives_live_guard": r["derivatives_live_guard"],
            "guarded_live_preflight": guarded_live_preflight,
            "guarded_live_run_packet_summary": guarded_live_run_packet_summary,
            "event_store_archive": {
                "status": "deferred",
                "summary": "事件归档统计已移出运行时首屏摘要，请在回放页查看完整归档状态。",
                "truth_source": "/replay/status",
            },
            "replay_offsets": {
                "portfolio_replay": None,
                "status": "deferred",
                "truth_source": "/replay/status",
            },
        }
        if dashboard_summary_only:
            payload["dashboard_summary_only"] = True
            payload["truth_source"] = "system_runtime_dashboard_summary"
            payload["deferred_sections"] = [
                "full_strategy_runtime",
                "strategy_runtime_summary",
                "full_recovery_view",
                "full_guarded_live_preflight",
                "full_guarded_live_preflight_check_matrix",
                "full_blocker_control",
                "execution_adapter.readiness",
                "latest_reconciliation",
            ]
        return payload

    def metrics(self) -> dict[str, Any]:
        return self.owner._build_metrics()

    def phase1_shadow_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.owner._build_phase1_shadow_history(limit=limit, offset=offset)
