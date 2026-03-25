from __future__ import annotations

import unittest
from types import SimpleNamespace

from aats.schemas.blocker_control import BlockerControlItem
from aats.services.blocker_control.service import BlockerControlService


class TestBlockerControlSummary(unittest.TestCase):
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
        )
        service = BlockerControlService(owner)

        first = service.snapshot()
        second = service.snapshot()

        self.assertEqual(first.panel_version, second.panel_version)

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
        )
        service = BlockerControlService(owner)

        snapshot = service.snapshot()

        self.assertIsNotNone(snapshot.primary_blocker)
        self.assertEqual(snapshot.primary_blocker.blocker, "trial_guard_threshold_breached")
        self.assertEqual(snapshot.primary_task.kind, "resolve_blocker")
        self.assertNotIn("resume-system", [item.action_id for item in snapshot.primary_task.actions])
        self.assertIn(
            "open-execution-view",
            [item.action_id for item in snapshot.primary_blocker.actions],
        )
        self.assertTrue(any(item.blocker == "kill_switch_active" for item in snapshot.secondary_blockers))

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
