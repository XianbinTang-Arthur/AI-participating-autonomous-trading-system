from __future__ import annotations

import unittest
from types import SimpleNamespace

from aats.schemas.blocker_control import BlockerControlItem
from aats.services.blocker_control.service import BlockerControlService


class TestBlockerControlSummary(unittest.TestCase):
    def test_snapshot_builds_mode_from_parallel_context_without_system_mode_fallback(self) -> None:
        mode_builder_calls: list[dict[str, object]] = []

        def build_system_mode(**kwargs):
            mode_builder_calls.append(kwargs)
            return {
                "submit_blocked_reasons": list(kwargs["readiness"].get("submit_blocked_reasons", [])),
                "execution_blocked": False,
            }

        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                kill_switch=SimpleNamespace(halted=False),
                health_service=SimpleNamespace(snapshot=lambda: SimpleNamespace(blockers=["health_blocker"])),
                mode_controller=SimpleNamespace(
                    snapshot=lambda: {
                        "submit_blocked_reasons": ["mode_blocker"],
                    }
                ),
                execution_adapter=SimpleNamespace(
                    readiness=lambda: {
                        "exchange_submit_allowed": False,
                        "submit_blocked_reasons": ["execution_blocker"],
                    }
                ),
            ),
            recovery_queries=SimpleNamespace(build_system_mode=build_system_mode),
            recovery_view=lambda: {
                "safe_to_trade": True,
                "review_required": False,
                "resume_eligible": True,
                "halted": False,
                "rebaseline_available": False,
                "resume_blocked_reasons": [],
            },
            _latest_scoped_reconciliation=lambda: None,
            trial_guard=lambda: {"status": "monitoring"},
            ai_runtime=lambda: {},
            system_mode=lambda: (_ for _ in ()).throw(
                AssertionError("snapshot should pass preloaded context to build_system_mode")
            ),
        )
        service = BlockerControlService(owner)

        snapshot = service.snapshot()

        self.assertEqual(len(mode_builder_calls), 1)
        call = mode_builder_calls[0]
        self.assertEqual(call["snapshot"]["submit_blocked_reasons"], ["mode_blocker"])
        self.assertEqual(call["readiness"]["submit_blocked_reasons"], ["execution_blocker"])
        self.assertEqual(call["health_blockers"], ["health_blocker"])
        self.assertEqual(call["trial_guard"], {"status": "monitoring"})
        self.assertTrue(snapshot.blockers)

    def test_snapshot_uses_dashboard_recovery_summary_when_available(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                kill_switch=SimpleNamespace(halted=False),
                health_service=SimpleNamespace(snapshot=lambda: SimpleNamespace(blockers=[])),
                ai_service=SimpleNamespace(status=lambda: {}),
            ),
            recovery_view_dashboard=lambda: {
                "safe_to_trade": True,
                "review_required": False,
                "resume_eligible": True,
                "halted": False,
                "rebaseline_available": False,
                "resume_blocked_reasons": [],
            },
            recovery_view=lambda: (_ for _ in ()).throw(
                AssertionError("dashboard blocker control must not build full recovery")
            ),
            _latest_scoped_reconciliation=lambda: None,
            system_mode=lambda: {"submit_blocked_reasons": []},
            ai_runtime=lambda: {},
        )
        service = BlockerControlService(owner)

        snapshot = service.snapshot()

        self.assertTrue(snapshot.safe_to_trade)

    def test_execution_blocker_summary_reuses_preloaded_health_snapshot(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                kill_switch=SimpleNamespace(halted=False),
                health_service=SimpleNamespace(
                    snapshot=lambda: (_ for _ in ()).throw(
                        AssertionError("summary should reuse preloaded health snapshot")
                    )
                ),
            )
        )
        service = BlockerControlService(owner)

        summary = service.execution_blocker_summary(
            recovery={
                "safe_to_trade": False,
                "review_required": False,
                "resume_eligible": False,
                "resume_blocked_reasons": ["operator_rebaseline_required"],
            },
            submit_blocked_reasons=["live_submit_disabled"],
            health_snapshot=SimpleNamespace(blockers=["account_state_stale"]),
        )

        blockers = [item["blocker"] for item in summary["blockers"]]
        self.assertEqual(
            blockers,
            ["account_state_stale", "live_submit_disabled", "operator_rebaseline_required"],
        )

    def test_snapshot_panel_version_is_stable_when_state_does_not_change(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                kill_switch=SimpleNamespace(halted=False),
                health_service=SimpleNamespace(snapshot=lambda: SimpleNamespace(blockers=[])),
                ai_service=SimpleNamespace(status=lambda: {}),
            ),
            recovery_view=lambda: {
                "safe_to_trade": True,
                "review_required": False,
                "resume_eligible": True,
                "halted": False,
                "rebaseline_available": False,
                "resume_blocked_reasons": [],
            },
            _latest_scoped_reconciliation=lambda: None,
            system_mode=lambda: {"submit_blocked_reasons": []},
            # Stage 7：blocker_control 现在走 owner.ai_runtime() 拿 stub-aware 字典，
            # 不再直读 runtime.ai_service.status()。fake owner 也要暴露这个方法。
            ai_runtime=lambda: {},
        )
        service = BlockerControlService(owner)

        first = service.snapshot()
        second = service.snapshot()

        self.assertEqual(first.panel_version, second.panel_version)

    def test_build_items_does_not_crash_when_ai_service_is_missing(self) -> None:
        """Stage 7 修复：gateway/market/execution role 下 runtime.ai_service is None。

        blocker_control 此前直接调 self.owner.runtime.ai_service.status() 触发 NPE，
        让 /system/blocker-control 在 gateway 进程返回 500，进而拖崩 dashboard。

        修复后走 self.owner.ai_runtime()，由 RuntimeQueryFacade 在 ai_service is None
        时返回 stub dict（key 齐全、value falsy），blocker_control 链路不再依赖
        ai_service 是否被装载。
        """
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                kill_switch=SimpleNamespace(halted=False),
                health_service=SimpleNamespace(snapshot=lambda: SimpleNamespace(blockers=[])),
                ai_service=None,  # 关键：模拟 gateway role
            ),
            recovery_view=lambda: {
                "safe_to_trade": True,
                "review_required": False,
                "resume_eligible": True,
                "halted": False,
                "rebaseline_available": False,
                "resume_blocked_reasons": [],
            },
            _latest_scoped_reconciliation=lambda: None,
            system_mode=lambda: {"submit_blocked_reasons": []},
            # facade stub 模拟：所有 .get() 都返回 None / falsy
            ai_runtime=lambda: {
                "outcome_review_required": False,
                "ai_service_loaded": False,
                "process_role": "gateway",
            },
        )
        service = BlockerControlService(owner)

        snapshot = service.snapshot()

        # 没有任何 blocker（health_snapshot 空 + 不 halted + 没 reasons），
        # snapshot 应当顺利返回，且 blockers 列表为空
        self.assertEqual(list(snapshot.blockers), [])
        self.assertFalse(snapshot.halted)
        self.assertTrue(snapshot.safe_to_trade)

    def test_next_step_summary_explains_review_without_primary_blocker(self) -> None:
        summary = BlockerControlService._next_step_summary(  # type: ignore[attr-defined]
            None,
            [],
            recovery={
                "safe_to_trade": False,
                "review_required": True,
                "resume_eligible": False,
                "halted": True,
                "resume_blocked_reasons": [],
            },
            latest_reconciliation=None,
        )

        self.assertIn("人工确认流程", summary)

    def test_next_step_summary_explains_observational_drift_without_primary_blocker(self) -> None:
        summary = BlockerControlService._next_step_summary(  # type: ignore[attr-defined]
            None,
            [],
            recovery={
                "safe_to_trade": True,
                "review_required": False,
                "resume_eligible": True,
                "halted": False,
                "resume_blocked_reasons": [],
            },
            latest_reconciliation=SimpleNamespace(observational_only=True),
        )

        self.assertIn("轻度动态漂移", summary)

    def test_surface_halt_never_becomes_first_priority_when_real_root_cause_exists(self) -> None:
        service = BlockerControlService(SimpleNamespace())
        primary, secondary = service._primary_and_secondary_items(  # type: ignore[attr-defined]
            [
                BlockerControlItem(
                    blocker="kill_switch_active",
                    category="system_execution",
                    subsystem="execution_control",
                    priority=90,
                    title="系统当前仍处于暂停状态",
                    description="暂停是结果。",
                    impact="暂停会阻止继续自动运行。",
                    recommended_next_step="先处理更上游的阻断。",
                    derived_from=["operator_rebaseline_required"],
                ),
                BlockerControlItem(
                    blocker="operator_rebaseline_required",
                    category="system_execution",
                    subsystem="reconciliation",
                    priority=50050,
                    title="需要人工确认新基线",
                    description="账实状态需要人工确认。",
                    impact="未确认前系统不会恢复自动交易。",
                    recommended_next_step="先查看最新对账，再决定是否接受当前状态为新基线。",
                    root_cause=True,
                ),
            ]
        )

        self.assertIsNotNone(primary)
        self.assertEqual(primary.blocker, "operator_rebaseline_required")
        self.assertEqual(len(secondary), 1)
        self.assertEqual(secondary[0].blocker, "kill_switch_active")

    def test_primary_task_explains_resume_when_only_manual_pause_remains(self) -> None:
        service = BlockerControlService(SimpleNamespace())
        task = service._primary_task(  # type: ignore[attr-defined]
            primary=None,
            secondary=[],
            recovery={
                "safe_to_trade": False,
                "review_required": False,
                "resume_eligible": True,
                "halted": True,
                "rebaseline_available": False,
                "resume_blocked_reasons": [],
            },
            latest_reconciliation=None,
        )

        self.assertEqual(task.kind, "resume")
        self.assertIn("恢复自动运行", task.summary)
        self.assertIn("resume-system", [item.action_id for item in task.actions])
        self.assertNotIn("reconcile-now", [item.action_id for item in task.actions])
        self.assertNotIn("halt-system", [item.action_id for item in task.actions])

    def test_trial_guard_breach_is_primary_blocker_instead_of_surface_halt(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                kill_switch=SimpleNamespace(halted=True),
                health_service=SimpleNamespace(snapshot=lambda: SimpleNamespace(blockers=[])),
                ai_service=SimpleNamespace(status=lambda: {}),
            ),
            recovery_view=lambda: {
                "safe_to_trade": False,
                "review_required": False,
                "resume_eligible": False,
                "halted": True,
                "rebaseline_available": False,
                "resume_blocked_reasons": ["trial_guard_threshold_breached"],
            },
            _latest_scoped_reconciliation=lambda: None,
            system_mode=lambda: {"submit_blocked_reasons": []},
            ai_runtime=lambda: {},
        )
        service = BlockerControlService(owner)

        snapshot = service.snapshot()

        self.assertIsNotNone(snapshot.primary_blocker)
        self.assertEqual(snapshot.primary_blocker.blocker, "trial_guard_threshold_breached")
        self.assertEqual(snapshot.primary_task.kind, "resolve_blocker")
        self.assertNotIn("resume-system", [item.action_id for item in snapshot.primary_task.actions])
        self.assertIn(
            "open-strategy-view",
            [item.action_id for item in snapshot.primary_blocker.actions],
        )
        self.assertIn(
            "open-execution-view",
            [item.action_id for item in snapshot.primary_blocker.actions],
        )
        self.assertTrue(any(item.blocker == "kill_switch_active" for item in snapshot.secondary_blockers))

    def test_risk_snapshot_auto_halt_surfaces_refresh_exchange_state_actions(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                kill_switch=SimpleNamespace(halted=True),
                health_service=SimpleNamespace(snapshot=lambda: SimpleNamespace(blockers=[])),
                ai_service=SimpleNamespace(status=lambda: {}),
            ),
            recovery_view=lambda: {
                "safe_to_trade": False,
                "review_required": False,
                "resume_eligible": False,
                "halted": True,
                "rebaseline_available": False,
                "resume_blocked_reasons": ["derivatives_risk_snapshot_missing_auto_halt"],
            },
            _latest_scoped_reconciliation=lambda: None,
            system_mode=lambda: {"submit_blocked_reasons": []},
            ai_runtime=lambda: {},
        )
        service = BlockerControlService(owner)

        snapshot = service.snapshot()

        self.assertIsNotNone(snapshot.primary_blocker)
        self.assertEqual(snapshot.primary_blocker.blocker, "derivatives_risk_snapshot_missing_auto_halt")
        action_ids = [item.action_id for item in snapshot.primary_blocker.actions]
        self.assertIn("open-execution-view", action_ids)
        self.assertIn("refresh-exchange-state", action_ids)
        self.assertFalse(any(action_id.startswith("inspect-reconciliation:") for action_id in action_ids))

    def test_primary_task_healthy_state_has_no_manual_buttons(self) -> None:
        service = BlockerControlService(SimpleNamespace())
        task = service._primary_task(  # type: ignore[attr-defined]
            primary=None,
            secondary=[],
            recovery={
                "safe_to_trade": True,
                "review_required": False,
                "resume_eligible": True,
                "halted": False,
                "rebaseline_available": False,
                "resume_blocked_reasons": [],
            },
            latest_reconciliation=SimpleNamespace(
                reconciliation_id="recon_ok",
                severity="CLEAN",
                halt_required=False,
                review_required=False,
                observational_only=False,
                recommended_operator_action=None,
            ),
        )

        self.assertEqual(task.kind, "healthy")
        self.assertEqual(task.actions, [])

    def test_primary_task_review_only_surfaces_reconciliation_actions(self) -> None:
        service = BlockerControlService(SimpleNamespace())
        task = service._primary_task(  # type: ignore[attr-defined]
            primary=None,
            secondary=[],
            recovery={
                "safe_to_trade": False,
                "review_required": True,
                "resume_eligible": False,
                "halted": True,
                "rebaseline_available": True,
                "resume_blocked_reasons": [],
            },
            latest_reconciliation=SimpleNamespace(
                reconciliation_id="recon_review",
                severity="HARD_MISMATCH",
                halt_required=True,
                review_required=True,
                observational_only=False,
                recommended_operator_action="rebaseline_if_expected",
            ),
        )

        action_ids = [item.action_id for item in task.actions]
        self.assertEqual(task.kind, "review_reconciliation")
        self.assertIn("reconcile-now", action_ids)
        self.assertIn("accept-rebaseline", action_ids)
        self.assertTrue(any(action_id.startswith("inspect-reconciliation:") for action_id in action_ids))
        self.assertNotIn("resume-system", action_ids)
        self.assertNotIn("halt-system", action_ids)


if __name__ == "__main__":
    unittest.main()
