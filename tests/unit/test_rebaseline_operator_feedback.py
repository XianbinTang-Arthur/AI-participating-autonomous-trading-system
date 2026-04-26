from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from aats.schemas.blocker_control import BlockerControlSnapshot, BlockerControlTask
from aats.services.blocker_control.actions import BlockerActionService
from aats.services.operator.reconciliation_system_queries import ReconciliationSystemQueryFacade


class _ProxyClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def invoke(self, *, command: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append({"command": command, "payload": payload})
        return {"status": "normal_operation", "message": "代理完成"}


class _FailingProxyClient:
    async def invoke(self, *, command: str, payload: dict[str, object]) -> dict[str, object]:
        _ = command, payload
        raise RuntimeError("proxy_timeout_after_remote_command")


def _blocker_snapshot() -> BlockerControlSnapshot:
    return BlockerControlSnapshot(
        panel_version="panel_v1",
        halted=True,
        review_required=True,
        resume_eligible=False,
        safe_to_trade=False,
        next_step_summary="先查看最新对账。",
        primary_task=BlockerControlTask(
            kind="review_reconciliation",
            title="先确认当前账实状态",
            summary="先查看最新对账。",
            reason="当前需要人工确认。",
            completion_outcome="确认后重新评估。",
        ),
    )


class TestRebaselineOperatorFeedback(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_proxy_rebaseline_invalidates_cache_after_success(self) -> None:
        proxy = _ProxyClient()
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(account_read_enabled=True, account_backend="okx"),
                recovery_policy=SimpleNamespace(operator_rebaseline_supported=True),
                portfolio_service=None,
                reconciliation_service=None,
                operator_command_client=proxy,
            ),
            _invalidate_cache=Mock(),
        )
        facade = ReconciliationSystemQueryFacade(owner)

        result = await facade.rebaseline(reason="unit_proxy", actor_role="admin")

        self.assertEqual(result["message"], "代理完成")
        self.assertEqual(proxy.calls[0]["command"], "rebaseline")
        owner._invalidate_cache.assert_called_once()

    async def test_gateway_proxy_rebaseline_invalidates_cache_after_failure(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(account_read_enabled=True, account_backend="okx"),
                recovery_policy=SimpleNamespace(operator_rebaseline_supported=True),
                portfolio_service=None,
                reconciliation_service=None,
                operator_command_client=_FailingProxyClient(),
            ),
            _invalidate_cache=Mock(),
        )
        facade = ReconciliationSystemQueryFacade(owner)

        with self.assertRaisesRegex(RuntimeError, "proxy_timeout_after_remote_command"):
            await facade.rebaseline(reason="unit_proxy_failure", actor_role="admin")

        owner._invalidate_cache.assert_called_once()

    async def test_gateway_proxy_resume_invalidates_cache_after_success(self) -> None:
        proxy = _ProxyClient()
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                reconciliation_service=None,
                operator_command_client=proxy,
            ),
            _invalidate_cache=Mock(),
        )
        facade = ReconciliationSystemQueryFacade(owner)

        result = await facade.resume(reason="unit_resume_proxy", actor_role="admin")

        self.assertEqual(result["message"], "代理完成")
        self.assertEqual(proxy.calls[0]["command"], "resume")
        owner._invalidate_cache.assert_called_once()

    async def test_gateway_proxy_resume_invalidates_cache_after_failure(self) -> None:
        owner = SimpleNamespace(
            runtime=SimpleNamespace(
                reconciliation_service=None,
                operator_command_client=_FailingProxyClient(),
            ),
            _invalidate_cache=Mock(),
        )
        facade = ReconciliationSystemQueryFacade(owner)

        with self.assertRaisesRegex(RuntimeError, "proxy_timeout_after_remote_command"):
            await facade.resume(reason="unit_resume_proxy_failure", actor_role="admin")

        owner._invalidate_cache.assert_called_once()

    async def test_blocker_action_uses_rebaseline_result_message(self) -> None:
        async def rebaseline(**_kwargs):
            return {
                "status": "resume_blocked",
                "message": "已接受当前状态为新基线，但当前仍不能恢复自动运行：最新对账要求暂停。",
            }

        owner = SimpleNamespace(
            _build_blocker_control=_blocker_snapshot,
            rebaseline=rebaseline,
        )
        service = BlockerActionService(owner)

        result = await service.execute(
            action_id="accept-rebaseline",
            panel_version=None,
            blocker=None,
            parent_intent_id=None,
            reason="unit_rebaseline",
            actor_role="admin",
        )

        self.assertIn("仍不能恢复自动运行", result.message)

    async def test_blocker_action_uses_resume_result_message(self) -> None:
        async def resume(**_kwargs):
            return {
                "status": "resume_blocked",
                "message": "恢复自动运行被阻断：operator_rebaseline_required。",
            }

        owner = SimpleNamespace(
            _build_blocker_control=_blocker_snapshot,
            resume=resume,
        )
        service = BlockerActionService(owner)

        result = await service.execute(
            action_id="resume-system",
            panel_version=None,
            blocker=None,
            parent_intent_id=None,
            reason="unit_resume",
            actor_role="admin",
        )

        self.assertIn("恢复自动运行被阻断", result.message)


class TestRebaselineMessage(unittest.TestCase):
    def test_blocked_rebaseline_message_lists_actual_blockers(self) -> None:
        message = ReconciliationSystemQueryFacade._rebaseline_result_message(
            rebaseline_status="resume_blocked",
            effective_recovery={
                "recovery_state": "resume_blocked",
                "safe_to_trade": False,
                "resume_eligible": False,
                "resume_blocked_reasons": ["operator_rebaseline_required"],
            },
            baseline={"open_order_count": 2, "requires_operator_review": True},
            reconciliation={
                "halt_required": True,
                "review_required": True,
                "only_reduce_required": False,
            },
            auto_resume=None,
        )

        self.assertIn("已接受当前状态为新基线", message)
        self.assertIn("仍不能恢复自动运行", message)
        self.assertIn("交易所仍有 2 条挂单", message)
        self.assertIn("最新对账要求暂停", message)
        self.assertIn("operator_rebaseline_required", message)

    def test_completed_rebaseline_message_reports_auto_resume(self) -> None:
        message = ReconciliationSystemQueryFacade._rebaseline_result_message(
            rebaseline_status="rebaseline_completed",
            effective_recovery={
                "recovery_state": "normal_operation",
                "safe_to_trade": True,
                "resume_eligible": True,
                "resume_blocked_reasons": [],
            },
            baseline={"open_order_count": 0, "requires_operator_review": False},
            reconciliation={
                "halt_required": False,
                "review_required": False,
                "only_reduce_required": False,
            },
            auto_resume={"status": "resumed"},
        )

        self.assertIn("恢复自动运行", message)

    def test_blocked_resume_message_lists_actual_blockers(self) -> None:
        message = ReconciliationSystemQueryFacade._resume_result_message(
            status="resume_blocked",
            runnable=False,
            blockers=[{"blocker": "operator_rebaseline_required"}],
            recovery={
                "recovery_state": "review_required",
                "safe_to_trade": False,
                "resume_eligible": False,
                "resume_blocked_reasons": ["latest_reconciliation_not_clean"],
            },
        )

        self.assertIn("恢复自动运行被阻断", message)
        self.assertIn("operator_rebaseline_required", message)
        self.assertIn("latest_reconciliation_not_clean", message)


if __name__ == "__main__":
    unittest.main()
