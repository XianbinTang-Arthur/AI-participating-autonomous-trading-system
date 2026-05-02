from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.schemas.common import utc_now
from aats.services.execution_engine.okx_account import derivatives_position_mode_contract
from aats.services.operator._parallel import parallel_fetch
from aats.services.operator.ui_capabilities import ui_operating_mode_override_policy
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

        status = dict(
            await client.invoke(
                command="ai_runtime_status",
                payload={},
            )
        )
        status["ui_operating_mode_override"] = ui_operating_mode_override_policy()
        status.setdefault("ai_runtime_source", "remote_decision")
        status.setdefault(
            "queried_from_process_role",
            getattr(self.owner.runtime.settings, "process_role", None),
        )
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
        snapshot = r["snapshot"]
        mode_snapshot = r["mode_snapshot"]
        recovery = r["recovery"]
        market = r["market"]
        account = r["account"]
        execution = r["execution"]
        phase1_shadow = r["phase1_shadow"]
        derivatives_live_guard = r["derivatives_live_guard"]
        latest_reconciliation = r["latest_reconciliation"]
        latest_portfolio = r["latest_portfolio"]
        blockers = r["blockers"]
        account_baseline = r["account_baseline"]
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

    def build_system_runtime(self) -> dict[str, Any]:
        # ── 阶段 0：预热 strategy_runtime 的 30s TTL 缓存 ──────────
        # strategy_runtime 内部用 ThreadPoolExecutor(5) 并行发 8 个 DB 查询。
        # 如果放进下面的 parallel_fetch（17 路并发），会与其他 16 个查询同时
        # 竞争 DB 连接池（pool_size=10），导致冷启动从 ~5s 膨胀到 30s+。
        # 先单独计算并填充缓存，parallel_fetch 中的 lambda 直接命中热缓存。
        self.owner.strategy_runtime(limit=5)

        # ── 阶段 1：并行获取所有独立子查询 ──────────────────────
        r = parallel_fetch({
            "latest_decision": lambda: self.owner.runtime.event_store.latest(topics.DECISION_CONTEXTS),
            "latest_fill": self.owner.latest_fill,
            "latest_reconciliation": self.owner._latest_scoped_reconciliation,
            "account_baseline": self.owner.latest_account_baseline,
            "account_snapshot": self.owner.latest_exchange_snapshot,
            "recovery": self.owner.recovery_view,
            "guarded_live_preflight": self.owner.guarded_live_preflight,
            "blocker_control": self.owner.blocker_control,
            "control_plane_consistency": self._control_plane_consistency,
            "account_status": self.owner.account_service_status,
            "runtime_profile_control": self.owner.runtime_profile_snapshot,
            "strategy_runtime_summary": lambda: self.owner.strategy_runtime(limit=5).get("summary"),
            "trial_guard": self.owner.trial_guard,
            "margin_buffer_overview": self.owner.margin_buffer_risk,
            "derivatives_live_guard": self.owner.derivatives_live_guard,
        })

        # ── 阶段 2：依赖性计算（需要上面的结果） ─────────────────
        latest_decision = r["latest_decision"]
        latest_fill = r["latest_fill"]
        latest_reconciliation = r["latest_reconciliation"]
        account_baseline = r["account_baseline"]
        account_snapshot = r["account_snapshot"]
        recovery = r["recovery"]
        guarded_live_preflight = r["guarded_live_preflight"]
        control_plane_consistency = r["control_plane_consistency"]
        account_status = r["account_status"]
        guarded_live_run_packet_summary = self.guarded_live_run_packet_summary(
            preflight=guarded_live_preflight,
            live_guard=r["derivatives_live_guard"],
            trial_guard=r["trial_guard"],
            margin_buffer=r["margin_buffer_overview"],
            recovery=recovery,
            blocker_control=r["blocker_control"],
        )
        position_mode_contract = account_status.get("position_mode_contract") or derivatives_position_mode_contract(
            settings=self.owner.runtime.settings,
            snapshot=account_snapshot,
        )
        now = utc_now()

        # ── 阶段 3：组装返回 ──────────────────────────────────
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
            "strategy_runtime_summary": r["strategy_runtime_summary"],
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

    def metrics(self) -> dict[str, Any]:
        return self.owner._build_metrics()

    def phase1_shadow_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.owner._build_phase1_shadow_history(limit=limit, offset=offset)
