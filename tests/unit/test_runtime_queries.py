from __future__ import annotations

import asyncio
import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aats.services.blocker_control import BlockerControlService
from aats.services.operator.recovery_queries import RecoveryQueryFacade
from aats.services.operator.runtime_queries import RuntimeQueryFacade
from aats.services.operator.ui_capabilities import UI_OPERATING_MODE_OVERRIDE_DISABLED_REASON


class _FakeOwner:
    def __init__(
        self,
        *,
        phase5_enabled: bool,
        financial_convergence_mode_enabled: bool,
        portfolio_ledger_truth_enabled: bool,
    ) -> None:
        self._phase5_enabled = phase5_enabled
        self.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                financial_convergence_mode_enabled=financial_convergence_mode_enabled,
                portfolio_ledger_truth_enabled=portfolio_ledger_truth_enabled,
            )
        )

    def _phase5_control_plane_enabled(self) -> bool:
        return self._phase5_enabled


class _AIRuntimeFakeOwner:
    """Stage 7：模拟 process_role=gateway 时 ai_service 为 None 的 owner。

    构造一个最小 SimpleNamespace runtime，使 RuntimeQueryFacade.ai_runtime()
    能完整跑通而不依赖任何真实 service。strategy_profiles.snapshot() 不会被
    调到（stub 路径会在 ai_service is None 时 early return），所以这里只需
    桩 runtime.ai_service / runtime.settings 两个最小依赖。
    """

    def __init__(self, *, ai_service: object | None, process_role: str | None) -> None:
        self.runtime = SimpleNamespace(
            ai_service=ai_service,
            settings=SimpleNamespace(
                process_role=process_role,
                ai_manual_operating_mode_override_freeze_seconds=900,
            ),
        )


class _RunPacketSummaryOwner:
    def __init__(self, cached_packet: dict | None = None) -> None:
        self.cached_packet = cached_packet
        self.full_packet_called = False

    def cached_guarded_live_run_packet(self) -> dict | None:
        return self.cached_packet

    def guarded_live_run_packet(self) -> dict:
        self.full_packet_called = True
        raise AssertionError("system runtime must not build the full guarded-live run packet")


class _DashboardHealthOwner:
    def __init__(self) -> None:
        self.persist_called = False
        self.recovery_posture = SimpleNamespace(
            execution_blockers=lambda *, health_blockers, recovery_blockers, submit_blocked_reasons: list(
                dict.fromkeys(
                    list(health_blockers)
                    + list(recovery_blockers)
                    + list(submit_blocked_reasons)
                )
            )
        )
        self.runtime = SimpleNamespace(
            kill_switch=SimpleNamespace(halted=False),
            health_service=SimpleNamespace(
                snapshot=lambda: (_ for _ in ()).throw(
                    AssertionError("dashboard health should synthesize health snapshot")
                ),
            ),
            mode_controller=SimpleNamespace(
                snapshot=lambda: {
                    "operating_state": "guarded_live_enabled",
                    "mode": "guarded_live",
                    "halted": False,
                    "submit_blocked_reasons": ["live_submit_disabled"],
                    "exchange_submit_allowed": True,
                }
            ),
            execution_adapter=SimpleNamespace(
                readiness=lambda: (_ for _ in ()).throw(
                    AssertionError("dashboard health should synthesize execution readiness")
                )
            ),
            market_gateway=SimpleNamespace(status=lambda: {"fresh": True}),
            runtime_profile=SimpleNamespace(to_dict=lambda: {}),
            environment_capabilities=SimpleNamespace(to_dict=lambda: {}),
            policy_profile=SimpleNamespace(to_dict=lambda: {}),
            recovery_policy=SimpleNamespace(to_dict=lambda: {}),
            runtime_profile_resolution=SimpleNamespace(profile_source="unit"),
            settings=SimpleNamespace(storage_mode="postgres"),
            audit_repo=SimpleNamespace(
                count=lambda: (_ for _ in ()).throw(
                    AssertionError("dashboard health should defer audit counts")
                )
            ),
            replay_validation_history=[],
        )
        self.recovery_queries = RecoveryQueryFacade(self)
        self.blocker_control_service = BlockerControlService(self)

    def _cached_ttl(self, _key: str, _ttl_seconds: int, loader):
        return loader()

    def _scope_cache_fragment(self) -> str:
        return "derivatives:cross:BTC-USDT-SWAP"

    def recovery_view(self) -> dict:
        return {
            "recovery_state": "normal_operation",
            "safe_to_trade": True,
            "review_required": False,
            "rebaseline_available": False,
            "resume_eligible": True,
            "resume_blocked_reasons": [],
            "recovered_reconciliation_available": True,
            "latest_account_baseline": {"baseline_id": "baseline_from_recovery"},
        }

    def recovery_view_dashboard(self) -> dict:
        return self.recovery_view()

    def system_mode(self) -> dict:
        raise AssertionError("dashboard health should derive mode from the existing recovery context")

    def blockers(self) -> list[dict]:
        raise AssertionError("dashboard health should use a minimal blocker summary")

    def account_service_status(self) -> dict:
        return {"fresh": True}

    def phase1_shadow(self) -> dict:
        return {
            "status": "lagging",
            "summary": "lagging",
            "ready": False,
            "fresh": True,
            "blockers": ["phase1_shadow_recovery_required"],
        }

    def derivatives_live_guard(self) -> dict:
        return {}

    def _latest_scoped_reconciliation(self):
        raise AssertionError("dashboard health should use health snapshot reconciliation summary")

    def _latest_scoped_snapshot(self):
        raise AssertionError("dashboard health should defer latest portfolio snapshot")

    def latest_account_baseline(self) -> dict:
        raise AssertionError("dashboard health should reuse recovery baseline")

    def trial_guard(self) -> dict:
        return {"status": "monitoring"}

    def runtime_profile_snapshot(self) -> dict:
        return {}

    def _persist_blocker_snapshot(self, **_kwargs) -> None:
        self.persist_called = True
        raise AssertionError("dashboard health should not write blocker snapshots")


class _RecoveryStatus:
    def model_dump(self, *, mode: str = "json") -> dict:
        return {
            "recovery_state": "normal_operation",
            "safe_to_trade": True,
            "resume_eligible": True,
            "review_required": False,
            "halt_required": False,
            "bundle_recovery_required": False,
            "only_reduce_required": False,
            "resume_blocked_reasons": [],
            "rebaseline_available": False,
            "independent_recovery_snapshots": [],
        }


class _DashboardStateSnapshot:
    recovery_state = "normal_operation"
    safe_to_trade = True
    resume_eligible = True
    review_required = False
    halt_required = False
    bundle_recovery_required = False
    only_reduce_required = False
    resume_blocked_reasons_json: list[str] = []

    def model_dump(self, *, mode: str = "json") -> dict:
        return {
            "snapshot_id": "snapshot_dashboard",
            "details_json": {
                "source": "startup_exit_execution_review",
                "review_items": [{"parent_intent_id": "parent_dashboard"}],
            },
        }


class _DashboardRebaselineStateSnapshot:
    recovery_state = "resume_blocked"
    safe_to_trade = False
    resume_eligible = False
    review_required = False
    halt_required = True
    bundle_recovery_required = False
    only_reduce_required = False
    resume_blocked_reasons_json = [
        "reconciliation_halt_required",
        "operator_rebaseline_required",
    ]

    def model_dump(self, *, mode: str = "json") -> dict:
        return {
            "snapshot_id": "snapshot_rebaseline",
            "details_json": {
                "reconciliation_severity": "HARD_MISMATCH",
                "finding_summary": {"halt_required_count": 2},
            },
        }


class _DashboardRecoveryOwner:
    def __init__(
        self,
        *,
        latest_state_snapshot=None,
        operator_rebaseline_supported: bool = False,
    ) -> None:
        latest_state_snapshot = latest_state_snapshot or _DashboardStateSnapshot()
        self.state_scope = SimpleNamespace(
            product_type="derivatives",
            margin_mode="cross",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        self.runtime = SimpleNamespace(
            reconciliation_repo=SimpleNamespace(
                latest_state_snapshot_for_scope=lambda *, scope: latest_state_snapshot
            ),
            event_store=SimpleNamespace(),
            recovery_status=_RecoveryStatus(),
            kill_switch=SimpleNamespace(halted=False),
            recovery_policy=SimpleNamespace(
                operator_rebaseline_supported=operator_rebaseline_supported,
            ),
        )
        self.recovery_posture = SimpleNamespace(
            finalize_status=lambda *, latest_reconciliation: (_ for _ in ()).throw(
                AssertionError("dashboard recovery must not finalize full recovery posture")
            )
        )

    def _scope_cache_fragment(self) -> str:
        return "derivatives:cross:BTC-USDT-SWAP"

    def _cached_ttl(self, _key: str, _ttl_seconds: int, loader):
        return loader()

    def _latest_scoped_reconciliation(self):
        raise AssertionError("dashboard recovery must defer latest reconciliation")

    def latest_account_baseline(self) -> dict:
        return {"baseline_id": "baseline_dashboard"}

    def _independent_recovery_snapshots_view(self, snapshots):
        return list(snapshots or [])

    def latest_order(self):
        raise AssertionError("dashboard recovery must not query latest order for claimed-submit gate")

    def latest_operator_action(self, _action: str):
        raise AssertionError("dashboard recovery must not query operator actions")

    def _reconciliation_mismatch_summary(self, _latest_reconciliation):
        raise AssertionError("dashboard recovery must not build full reconciliation summary")

    def _exit_execution_review_items(self):
        raise AssertionError("dashboard recovery must not build exit execution review items")

    def _exit_execution_action_history(self):
        raise AssertionError("dashboard recovery must not build exit execution action history")

    def _enrich_exit_execution_review_items(self, _items):
        raise AssertionError("dashboard recovery must not enrich startup review items")

    def ai_runtime(self):
        raise AssertionError("dashboard recovery must not build AI runtime")

    def payload(self, _envelope):
        raise AssertionError("dashboard recovery must not serialize AI events")


class _DashboardModeOwner:
    def __init__(self) -> None:
        self.runtime = SimpleNamespace(
            mode_controller=SimpleNamespace(
                snapshot=lambda: {
                    "mode": "guarded_live",
                    "operating_state": "guarded_live_enabled",
                    "submit_blocked_reasons": ["live_submit_disabled"],
                    "exchange_submit_allowed": True,
                }
            ),
            execution_adapter=SimpleNamespace(
                readiness=lambda: (_ for _ in ()).throw(
                    AssertionError("dashboard mode must synthesize execution readiness")
                )
            ),
            health_service=SimpleNamespace(
                execution_blockers=lambda: (_ for _ in ()).throw(
                    AssertionError("dashboard mode must defer full health blockers")
                )
            ),
            runtime_profile_resolution=SimpleNamespace(profile_source="unit"),
            settings=SimpleNamespace(
                mode="guarded_live",
                live_submit_enabled=True,
                guarded_execution_dry_run=False,
                okx_simulated_trading=False,
            ),
        )
        self.recovery_posture = SimpleNamespace(
            execution_blockers=lambda *, health_blockers, recovery_blockers, submit_blocked_reasons: list(
                dict.fromkeys(
                    list(health_blockers)
                    + list(recovery_blockers)
                    + list(submit_blocked_reasons)
                )
            )
        )

    def _scope_cache_fragment(self) -> str:
        return "derivatives:cross:BTC-USDT-SWAP"

    def _cached_ttl(self, _key: str, _ttl_seconds: int, loader):
        return loader()

    def recovery_view_dashboard(self) -> dict:
        return {
            "recovery_state": "normal_operation",
            "review_required": False,
            "rebaseline_available": False,
            "resume_blocked_reasons": [],
        }

    def recovery_view(self) -> dict:
        raise AssertionError("dashboard mode must not build full recovery")

    def account_service_status(self) -> dict:
        return {"ready": True, "fresh": True}

    def trial_guard(self) -> dict:
        return {"status": "monitoring"}


class TestRuntimeQueryFacade(unittest.TestCase):
    def test_control_plane_consistency_marks_phase5_without_financial_convergence_as_transitional(self) -> None:
        facade = RuntimeQueryFacade(
            _FakeOwner(
                phase5_enabled=True,
                financial_convergence_mode_enabled=False,
                portfolio_ledger_truth_enabled=True,
            )
        )

        snapshot = facade._control_plane_consistency()

        self.assertEqual(snapshot["status"], "transitional")
        self.assertIn(
            "phase5_control_plane_running_without_financial_convergence",
            snapshot["warning_codes"],
        )

    def test_control_plane_consistency_marks_ledger_truth_without_phase5_as_transitional(self) -> None:
        facade = RuntimeQueryFacade(
            _FakeOwner(
                phase5_enabled=False,
                financial_convergence_mode_enabled=False,
                portfolio_ledger_truth_enabled=True,
            )
        )

        snapshot = facade._control_plane_consistency()

        self.assertEqual(snapshot["status"], "transitional")
        self.assertIn(
            "portfolio_ledger_truth_enabled_without_phase5_control_plane",
            snapshot["warning_codes"],
        )

    def test_system_runtime_does_not_register_full_guarded_live_run_packet_query(self) -> None:
        source = inspect.getsource(RuntimeQueryFacade.build_system_runtime)

        self.assertNotIn(
            '"guarded_live_run_packet": self.owner.guarded_live_run_packet',
            source,
        )
        self.assertIn("guarded_live_run_packet_summary", source)

    def test_system_runtime_defers_event_archive_and_replay_offset_queries(self) -> None:
        source = inspect.getsource(RuntimeQueryFacade.build_system_runtime)

        self.assertNotIn(
            '"event_store_archive": self.owner.runtime.event_store.archive_summary',
            source,
        )
        self.assertNotIn(
            '"latest_replay_offset": lambda: self.owner.runtime.event_store.latest_replay_offset',
            source,
        )
        self.assertIn('"truth_source": "/replay/status"', source)

    def test_dashboard_health_reuses_recovery_context_and_minimal_blockers(self) -> None:
        owner = _DashboardHealthOwner()
        facade = RuntimeQueryFacade(owner)

        payload = facade.system_health_dashboard()

        self.assertTrue(payload["dashboard_summary_only"])
        self.assertEqual(payload["truth_source"], "runtime_health_dashboard_summary")
        self.assertIn("latest_portfolio", payload["deferred_sections"])
        self.assertIn("execution_adapter.readiness", payload["deferred_sections"])
        self.assertIn("latest_reconciliation", payload["deferred_sections"])
        self.assertIn("health_service.snapshot", payload["deferred_sections"])
        self.assertEqual(payload["mode_contract"]["recovery_state"], "normal_operation")
        self.assertEqual(
            payload["subsystems"]["reconciliation"]["last_update_ts"],
            None,
        )
        self.assertEqual(
            payload["last_success_timestamps"]["reconciliation"],
            None,
        )
        self.assertEqual(
            payload["subsystems"]["execution_adapter"]["truth_source"],
            "mode_controller_plus_account_status_dashboard_summary",
        )
        self.assertEqual(payload["account_baseline"]["baseline_id"], "baseline_from_recovery")
        self.assertIsNone(payload["subsystems"]["audit_replay"]["audit_record_count"])
        self.assertEqual(
            payload["subsystems"]["audit_replay"]["audit_record_count_status"],
            "deferred_from_dashboard_summary",
        )
        self.assertTrue(any(item["blocker"] == "phase1_shadow_recovery_required" for item in payload["blockers"]))
        self.assertFalse(owner.persist_called)

    def test_dashboard_recovery_summary_skips_full_recovery_details(self) -> None:
        owner = _DashboardRecoveryOwner()
        facade = RecoveryQueryFacade(owner)

        payload = facade.recovery_view_dashboard()

        self.assertTrue(payload["dashboard_summary_only"])
        self.assertEqual(payload["truth_source"], "recovery_dashboard_summary")
        self.assertEqual(payload["recovery_state"], "normal_operation")
        self.assertEqual(payload["latest_account_baseline"]["baseline_id"], "baseline_dashboard")
        self.assertEqual(payload["exit_execution_review_items"], [])
        self.assertEqual(payload["exit_execution_action_history"], [])
        self.assertEqual(
            payload["latest_state_snapshot"]["details_json"]["review_items"],
            [{"parent_intent_id": "parent_dashboard"}],
        )
        self.assertIsNone(payload["ai_runtime"])
        self.assertEqual(
            payload["claimed_submit_recovery_gate"]["status"],
            "deferred_from_dashboard_summary",
        )
        self.assertIn("latest_reconciliation", payload["deferred_sections"])

    def test_dashboard_recovery_summary_derives_rebaseline_from_state_snapshot(self) -> None:
        owner = _DashboardRecoveryOwner(
            latest_state_snapshot=_DashboardRebaselineStateSnapshot(),
            operator_rebaseline_supported=True,
        )
        facade = RecoveryQueryFacade(owner)

        payload = facade.recovery_view_dashboard()

        self.assertEqual(payload["recovery_state"], "resume_blocked")
        self.assertFalse(payload["resume_eligible"])
        self.assertFalse(payload["safe_to_trade"])
        self.assertTrue(payload["halt_required"])
        self.assertTrue(payload["rebaseline_available"])
        self.assertEqual(
            payload["resume_blocked_reasons"],
            ["reconciliation_halt_required", "operator_rebaseline_required"],
        )

    def test_dashboard_mode_synthesizes_readiness_and_defers_full_blockers(self) -> None:
        owner = _DashboardModeOwner()
        facade = RecoveryQueryFacade(owner)

        payload = facade.system_mode_dashboard()

        self.assertTrue(payload["exchange_submit_allowed"])
        self.assertTrue(payload["submit_blocked"])
        self.assertEqual(payload["blocked_reason"], "live_submit_disabled")
        self.assertEqual(payload["trial_guard"], {"status": "monitoring"})

    def test_lightweight_run_packet_summary_does_not_call_full_packet_loader(self) -> None:
        owner = _RunPacketSummaryOwner()
        facade = RuntimeQueryFacade(owner)

        summary = facade.guarded_live_run_packet_summary(
            preflight={"status": "ready", "launch_ready": True},
            live_guard={"auto_halt_required": True, "only_reduce_required": False},
            trial_guard={"status": "monitoring"},
            margin_buffer={
                "status": "healthy",
                "current": {"initial_margin_usage_fraction": 0.12},
                "liquidation": {"nearest_liquidation_gap_ratio": 0.35},
            },
            recovery={"safe_to_trade": True},
            blocker_control={"blockers": []},
        )

        self.assertFalse(owner.full_packet_called)
        self.assertEqual(summary["status"], "critical")
        self.assertEqual(summary["summary_source"], "runtime_lightweight")
        self.assertFalse(summary["full_packet_cached"])
        self.assertIn("forward_validation", summary["deferred_sections"])
        self.assertEqual(
            summary["forward_validation_summary"]["summary"]["verdict"],
            "deferred",
        )
        self.assertIsNone(summary["summary_metrics"]["combined_net_realized_pnl"])
        self.assertEqual(
            summary["summary_metrics"]["current_initial_margin_usage_fraction"],
            0.12,
        )

    def test_run_packet_summary_reuses_cached_full_packet_when_available(self) -> None:
        cached_packet = {
            "status": "warning",
            "summary": "cached summary",
            "summary_metrics": {"combined_net_realized_pnl": "-1.25"},
            "operator_actions": ["cached action"],
            "forward_validation_summary": {"summary": {"verdict": "warning"}},
        }
        owner = _RunPacketSummaryOwner(cached_packet)
        facade = RuntimeQueryFacade(owner)

        summary = facade.guarded_live_run_packet_summary(
            preflight={"status": "ready", "launch_ready": True},
            live_guard={"auto_halt_required": False, "only_reduce_required": False},
            trial_guard={"status": "monitoring"},
            margin_buffer={"status": "healthy"},
            recovery={"safe_to_trade": True},
            blocker_control={"blockers": []},
        )

        self.assertFalse(owner.full_packet_called)
        self.assertEqual(summary["status"], "warning")
        self.assertEqual(summary["summary"], "cached summary")
        self.assertEqual(summary["summary_metrics"], cached_packet["summary_metrics"])
        self.assertEqual(summary["operator_actions"], ["cached action"])
        self.assertEqual(summary["summary_source"], "cached_full_packet")
        self.assertTrue(summary["full_packet_cached"])

    def test_lightweight_run_packet_summary_marks_execution_blockers_critical(self) -> None:
        owner = _RunPacketSummaryOwner()
        facade = RuntimeQueryFacade(owner)

        summary = facade.guarded_live_run_packet_summary(
            preflight={"status": "ready", "launch_ready": True},
            live_guard={"auto_halt_required": False, "only_reduce_required": False},
            trial_guard={"status": "monitoring"},
            margin_buffer={"status": "healthy"},
            recovery={"safe_to_trade": True},
            blocker_control={
                "blockers": [
                    {
                        "blocker": "phase1_shadow_recovery_required",
                        "affects_execution": True,
                    },
                    {
                        "blocker": "live_submit_disabled",
                        "affects_execution": False,
                    },
                ],
            },
        )

        self.assertFalse(owner.full_packet_called)
        self.assertEqual(summary["status"], "critical")
        self.assertEqual(summary["summary_metrics"]["execution_blocker_count"], 1)
        self.assertEqual(summary["active_blockers"][0]["blocker"], "phase1_shadow_recovery_required")
        self.assertIn("当前仍有执行阻断，先把阻断项处理干净。", summary["operator_actions"])


class TestAiRuntimeStubWhenServiceMissing(unittest.TestCase):
    """Stage 7 修复：gateway/market/execution role 下 runtime.ai_service is None。

    在原版实现里 ai_runtime() 直接 .status() 触发 AttributeError，向上传播到
    build_recovery_view → build_system_mode → build_system_health，让 /system/health
    /system/recovery /system/mode 三个 CORE_SPECS endpoint 全部 500，UI 整体崩。

    修复后 ai_runtime() 在 ai_service is None 时返回稳定 stub，下游消费者
    （recovery_view 内嵌 + UI ai-view）都用 .get() / `||` 安全访问。
    """

    def test_ai_runtime_returns_stub_when_ai_service_is_none(self) -> None:
        facade = RuntimeQueryFacade(
            _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        )

        with patch.dict(os.environ, {}, clear=True):
            result = facade.ai_runtime()

        # 关键标识：UI 一眼能看出本进程没装 AI 切片
        self.assertEqual(result["provider"], "not_loaded")
        self.assertEqual(result["provider_state"], "not_loaded")
        self.assertEqual(result["outcome_state"], "not_loaded")
        self.assertFalse(result["ai_service_loaded"])
        self.assertEqual(result["process_role"], "gateway")

        # 关键状态字段：必须是 falsy 但格式合法（UI 用 ?? 0 / || "unknown" 兜底）
        self.assertFalse(result["configured"])
        self.assertFalse(result["provider_ready"])
        self.assertFalse(result["degraded"])
        self.assertFalse(result["provider_degraded"])
        self.assertFalse(result["auto_downgrade_active"])
        self.assertFalse(result["manual_override_active"])
        self.assertFalse(result["shadow_mode_enabled"])

        # operating_mode 系列必须存在且为 None（UI 用 || "unknown" 兜底）
        self.assertIsNone(result["configured_operating_mode"])
        self.assertIsNone(result["effective_operating_mode"])
        self.assertIsNone(result["canonical_configured_operating_mode"])
        self.assertIsNone(result["canonical_effective_operating_mode"])

        # 计数字段必须是 0 而非 None（UI 直接 formatNumber，None 会渲染成 NaN）
        self.assertEqual(result["consecutive_failures"], 0)
        self.assertEqual(result["consecutive_successes"], 0)
        self.assertEqual(result["recent_assessment_count"], 0)
        self.assertEqual(result["recent_fallback_ratio"], 0.0)

        # 嵌套结构 failure_budget / outcome_policy / legacy_modes 必须存在
        # （recovery_view 内嵌时 .get 链不会 KeyError）
        self.assertIn("failure_budget", result)
        self.assertIn("outcome_policy", result)
        self.assertIn("legacy_modes", result)
        self.assertEqual(result["failure_budget"]["remaining_failures_until_degrade"], 0)
        self.assertEqual(result["outcome_policy"]["bad_window_threshold"], 0)
        self.assertIsNone(result["legacy_modes"]["configured_operating_mode"])

        # strategy_profile 控制字段保持原 dict 形态，给 UI auto-control panel 兜底
        self.assertEqual(
            result["strategy_profile_auto_control_reason"], "ai_service_not_loaded"
        )
        self.assertEqual(
            result["strategy_profile_control_effective_mode"], "manual"
        )
        self.assertEqual(result["operating_mode_source"], "ai_service_not_loaded")
        self.assertEqual(
            result["ui_operating_mode_override"],
            {
                "enabled": False,
                "source": "environment",
                "disabled_reason": UI_OPERATING_MODE_OVERRIDE_DISABLED_REASON,
            },
        )

    def test_ai_runtime_stub_carries_process_role_label(self) -> None:
        """Stage 7：每个非 monolith role 下 stub 都应该带上自己的 process_role 标签
        便于 UI/审计区分是哪个进程提供的诊断（market vs decision vs execution）。"""
        for role in ("gateway", "market", "execution"):
            with self.subTest(role=role):
                facade = RuntimeQueryFacade(
                    _AIRuntimeFakeOwner(ai_service=None, process_role=role)
                )
                result = facade.ai_runtime()
                self.assertEqual(result["process_role"], role)
                self.assertFalse(result["ai_service_loaded"])

    def test_ai_runtime_stub_handles_missing_freeze_setting_gracefully(self) -> None:
        """settings 缺 ai_manual_operating_mode_override_freeze_seconds 字段时
        stub 不应崩，应返回 None。"""
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        # 删字段，模拟极端 settings 缺失
        del owner.runtime.settings.ai_manual_operating_mode_override_freeze_seconds
        facade = RuntimeQueryFacade(owner)

        result = facade.ai_runtime()

        self.assertIsNone(result["manual_override_default_freeze_seconds"])


class TestAiRuntimeLocalServiceRead(unittest.TestCase):
    def test_ai_runtime_uses_lightweight_strategy_activation_status(self) -> None:
        class _StrategyProfiles:
            def activation_status(self) -> dict[str, object]:
                return {"auto_switch_enabled": True}

            def snapshot(self) -> dict[str, object]:
                raise AssertionError("ai_runtime must not build full strategy profile snapshot")

        ai_service = SimpleNamespace(
            status=lambda: {
                "configured_operating_mode": "baseline",
                "effective_operating_mode": "baseline",
                "canonical_configured_operating_mode": "baseline",
                "canonical_effective_operating_mode": "baseline",
                "manual_override_active": False,
            }
        )
        owner = _AIRuntimeFakeOwner(ai_service=ai_service, process_role="decision")
        owner.runtime.settings.strategy_profile_auto_control_configured = True
        owner.strategy_profiles = _StrategyProfiles()
        facade = RuntimeQueryFacade(owner)

        result = facade.ai_runtime()

        self.assertTrue(result["strategy_profile_auto_control_configured"])
        self.assertTrue(result["strategy_profile_auto_control_effective"])
        self.assertEqual(result["strategy_profile_auto_control_reason"], "configured_auto")


class TestAiRuntimeAuthoritativeRead(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        RuntimeQueryFacade.invalidate_authoritative_ai_runtime_cache()

    async def asyncTearDown(self) -> None:
        RuntimeQueryFacade.invalidate_authoritative_ai_runtime_cache()

    async def test_gateway_uses_ai_command_client_for_authoritative_status(self) -> None:
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        owner.runtime.ai_command_client = SimpleNamespace(
            invoke=AsyncMock(
                return_value={
                    "provider": "deepseek",
                    "configured": True,
                    "provider_ready": True,
                    "ai_service_loaded": True,
                    "process_role": "decision",
                }
            )
        )
        facade = RuntimeQueryFacade(owner)

        with patch.dict(os.environ, {}, clear=True):
            result = await facade.ai_runtime_authoritative()

        self.assertEqual(result["provider"], "deepseek")
        self.assertTrue(result["configured"])
        self.assertTrue(result["ai_service_loaded"])
        self.assertEqual(result["process_role"], "decision")
        self.assertEqual(result["ai_runtime_source"], "remote_decision")
        self.assertEqual(result["queried_from_process_role"], "gateway")
        self.assertEqual(
            result["ui_operating_mode_override"]["disabled_reason"],
            UI_OPERATING_MODE_OVERRIDE_DISABLED_REASON,
        )
        owner.runtime.ai_command_client.invoke.assert_awaited_once_with(
            command="ai_runtime_status",
            payload={},
        )

    async def test_gateway_reuses_recent_authoritative_status(self) -> None:
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        owner.runtime.ai_command_client = SimpleNamespace(
            invoke=AsyncMock(
                return_value={
                    "provider": "deepseek",
                    "configured": True,
                    "provider_ready": True,
                    "ai_service_loaded": True,
                    "process_role": "decision",
                }
            )
        )
        facade = RuntimeQueryFacade(owner)

        first = await facade.ai_runtime_authoritative()
        second = await facade.ai_runtime_authoritative()

        self.assertEqual(first["provider"], "deepseek")
        self.assertEqual(second["provider"], "deepseek")
        owner.runtime.ai_command_client.invoke.assert_awaited_once_with(
            command="ai_runtime_status",
            payload={},
        )

    async def test_gateway_coalesces_concurrent_authoritative_status_reads(self) -> None:
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        started = asyncio.Event()
        release = asyncio.Event()

        async def _invoke(**_kwargs: object) -> dict[str, object]:
            started.set()
            await release.wait()
            return {
                "provider": "deepseek",
                "configured": True,
                "provider_ready": True,
                "ai_service_loaded": True,
                "process_role": "decision",
            }

        owner.runtime.ai_command_client = SimpleNamespace(invoke=AsyncMock(side_effect=_invoke))
        facade = RuntimeQueryFacade(owner)

        first_task = asyncio.create_task(facade.ai_runtime_authoritative())
        await started.wait()
        second_task = asyncio.create_task(facade.ai_runtime_authoritative())
        await asyncio.sleep(0)
        self.assertEqual(owner.runtime.ai_command_client.invoke.await_count, 1)

        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        self.assertEqual(first["provider"], "deepseek")
        self.assertEqual(second["provider"], "deepseek")
        self.assertEqual(owner.runtime.ai_command_client.invoke.await_count, 1)

    async def test_gateway_status_inflight_survives_cancelled_waiter(self) -> None:
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        started = asyncio.Event()
        release = asyncio.Event()

        async def _invoke(**_kwargs: object) -> dict[str, object]:
            started.set()
            await release.wait()
            return {
                "provider": "deepseek",
                "configured": True,
                "provider_ready": True,
                "ai_service_loaded": True,
                "process_role": "decision",
            }

        owner.runtime.ai_command_client = SimpleNamespace(invoke=AsyncMock(side_effect=_invoke))
        facade = RuntimeQueryFacade(owner)

        first_task = asyncio.create_task(facade.ai_runtime_authoritative())
        await started.wait()
        first_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_task

        release.set()
        second = await facade.ai_runtime_authoritative()

        self.assertEqual(second["provider"], "deepseek")
        self.assertEqual(owner.runtime.ai_command_client.invoke.await_count, 1)

    async def test_authoritative_status_cache_can_be_invalidated_after_mutation(self) -> None:
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        owner.runtime.ai_command_client = SimpleNamespace(
            invoke=AsyncMock(
                side_effect=[
                    {
                        "provider": "deepseek",
                        "configured": True,
                        "provider_ready": True,
                        "ai_service_loaded": True,
                        "process_role": "decision",
                    },
                    {
                        "provider": "openai",
                        "configured": True,
                        "provider_ready": True,
                        "ai_service_loaded": True,
                        "process_role": "decision",
                    },
                ]
            )
        )
        facade = RuntimeQueryFacade(owner)

        first = await facade.ai_runtime_authoritative()
        RuntimeQueryFacade.invalidate_authoritative_ai_runtime_cache(owner.runtime)
        second = await facade.ai_runtime_authoritative()

        self.assertEqual(first["provider"], "deepseek")
        self.assertEqual(second["provider"], "openai")
        self.assertEqual(owner.runtime.ai_command_client.invoke.await_count, 2)

    async def test_authoritative_runtime_reports_ui_override_capability_when_enabled(self) -> None:
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        owner.runtime.ai_command_client = SimpleNamespace(
            invoke=AsyncMock(
                return_value={
                    "provider": "deepseek",
                    "configured": True,
                    "provider_ready": True,
                    "ai_service_loaded": True,
                    "process_role": "decision",
                }
            )
        )
        facade = RuntimeQueryFacade(owner)

        with patch.dict(
            os.environ,
            {"AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE": "true"},
            clear=False,
        ):
            result = await facade.ai_runtime_authoritative()

        self.assertEqual(
            result["ui_operating_mode_override"],
            {
                "enabled": True,
                "source": "environment",
                "disabled_reason": None,
            },
        )

    async def test_gateway_without_client_preserves_stable_stub(self) -> None:
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        owner.runtime.ai_command_client = None
        facade = RuntimeQueryFacade(owner)

        result = await facade.ai_runtime_authoritative()

        self.assertEqual(result["provider"], "not_loaded")
        self.assertFalse(result["ai_service_loaded"])
        self.assertEqual(result["ai_runtime_source"], "local_stub")
