"""Finding 2: Operator 执行动作 gateway→execution 代理扩展测试。

测试 6 个新增命令（validate_reconciliation / cancel_order /
resolve_stuck_submission / refresh_exchange_state / retry_limit_lookup /
safe_cancel_exit_execution）的：

  - Gateway 代理分支：runtime service=None 时走 client.invoke，缺 client 则 RuntimeError
  - Worker dispatch：所有 8 个命令均正确路由到对应 handler
  - E2E roundtrip：新命令在 InMemoryEventBus 上走完整往返

设计思路与 test_operator_command_bridge.py 一致，用 InMemoryEventBus 做
同步 dispatch 避免 NATS 依赖。
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock

from aats.bus.memory_bus import InMemoryEventBus
from aats.services.operator.command_bridge import (
    OperatorCommandClient,
    OperatorCommandRemoteError,
    OperatorCommandWorker,
)


def _make_logger() -> logging.Logger:
    return logging.getLogger("test_operator_command_proxy_expansion")


# ────────────────────────────────────────────────────────────────
# 辅助：最小 mock runtime，只含 gateway proxy 分支需要的字段
# ────────────────────────────────────────────────────────────────


class _GatewayMockRuntime:
    """模拟 gateway 进程的 runtime：execution 侧的 service 为 None。"""

    order_manager = None
    reconciliation_service = None

    def __init__(self, *, client: Any = None) -> None:
        self.operator_command_client = client


# ────────────────────────────────────────────────────────────────
# 1. Gateway 代理分支测试
# ────────────────────────────────────────────────────────────────


class TestGatewayProxyBranch(unittest.IsolatedAsyncioTestCase):
    """测试 query_service.py 中 6 个方法的 gateway proxy 分支。

    用 object.__new__(OperatorQueryService) 绕过复杂的 __init__，
    只设置 proxy 分支需要的 self.runtime 属性。
    """

    def _make_service_with_mock_runtime(self, *, client: Any = None):
        """创建一个最小化的 OperatorQueryService 实例用于 proxy 测试。"""
        # 延迟 import 避免模块加载顺序问题
        from aats.services.operator.query_service import OperatorQueryService

        service = object.__new__(OperatorQueryService)
        service.runtime = _GatewayMockRuntime(client=client)
        return service

    # ── 缺 client 时必须 RuntimeError ──

    async def test_validate_reconciliation_no_client_raises(self) -> None:
        service = self._make_service_with_mock_runtime(client=None)
        with self.assertRaises(RuntimeError) as ctx:
            await service.validate_reconciliation(
                reason="test", actor_role="admin",
            )
        self.assertIn("validate_reconciliation_requires_operator_command_client", str(ctx.exception))

    async def test_cancel_order_no_client_raises(self) -> None:
        service = self._make_service_with_mock_runtime(client=None)
        with self.assertRaises(RuntimeError) as ctx:
            await service.cancel_order(
                client_order_id="ord_1", reason="test", actor_role="admin",
            )
        self.assertIn("cancel_order_requires_operator_command_client", str(ctx.exception))

    async def test_resolve_stuck_submission_no_client_raises(self) -> None:
        service = self._make_service_with_mock_runtime(client=None)
        with self.assertRaises(RuntimeError) as ctx:
            await service.resolve_stuck_submission(
                client_order_id="ord_2", reason="test", actor_role="admin",
            )
        self.assertIn("resolve_stuck_submission_requires_operator_command_client", str(ctx.exception))

    async def test_refresh_exchange_state_no_client_raises(self) -> None:
        service = self._make_service_with_mock_runtime(client=None)
        with self.assertRaises(RuntimeError) as ctx:
            await service.refresh_exchange_state(
                blocker=None, reason="test", actor_role="admin",
            )
        self.assertIn("refresh_exchange_state_requires_operator_command_client", str(ctx.exception))

    async def test_retry_limit_lookup_no_client_raises(self) -> None:
        service = self._make_service_with_mock_runtime(client=None)
        with self.assertRaises(RuntimeError) as ctx:
            await service.retry_limit_lookup(
                parent_intent_id=None, reason="test", actor_role="admin",
            )
        self.assertIn("retry_limit_lookup_requires_operator_command_client", str(ctx.exception))

    async def test_safe_cancel_exit_execution_no_client_raises(self) -> None:
        service = self._make_service_with_mock_runtime(client=None)
        with self.assertRaises(RuntimeError) as ctx:
            await service.safe_cancel_exit_execution(
                parent_intent_id=None, reason="test", actor_role="admin",
            )
        self.assertIn("safe_cancel_exit_execution_requires_operator_command_client", str(ctx.exception))

    # ── 有 client 时走 client.invoke 代理 ──

    async def test_validate_reconciliation_proxies_to_client(self) -> None:
        mock_client = AsyncMock()
        mock_client.invoke.return_value = {"reconciled": True}
        service = self._make_service_with_mock_runtime(client=mock_client)
        service._invalidate_cache = Mock()

        result = await service.validate_reconciliation(
            reason="proxy_test",
            actor_role="admin",
            actor_identity="tester",
            auth_source="password",
        )
        self.assertEqual(result, {"reconciled": True})
        mock_client.invoke.assert_awaited_once_with(
            command="validate_reconciliation",
            payload={
                "reason": "proxy_test",
                "actor_role": "admin",
                "actor_identity": "tester",
                "auth_source": "password",
            },
        )
        service._invalidate_cache.assert_called_once()

    async def test_cancel_order_proxies_to_client(self) -> None:
        mock_client = AsyncMock()
        mock_client.invoke.return_value = {"order": {"status": "cancelled"}}
        service = self._make_service_with_mock_runtime(client=mock_client)
        service._invalidate_cache = Mock()

        result = await service.cancel_order(
            client_order_id="ord_abc",
            reason="proxy_test",
            actor_role="admin",
        )
        self.assertEqual(result, {"order": {"status": "cancelled"}})
        mock_client.invoke.assert_awaited_once()
        call_kwargs = mock_client.invoke.call_args.kwargs
        self.assertEqual(call_kwargs["command"], "cancel_order")
        self.assertEqual(call_kwargs["payload"]["client_order_id"], "ord_abc")
        service._invalidate_cache.assert_called_once()

    async def test_resolve_stuck_submission_proxies_to_client(self) -> None:
        mock_client = AsyncMock()
        mock_client.invoke.return_value = {"resolved": True}
        service = self._make_service_with_mock_runtime(client=mock_client)
        service._invalidate_cache = Mock()

        result = await service.resolve_stuck_submission(
            client_order_id="ord_stuck",
            reason="proxy_test",
            actor_role="admin",
        )
        self.assertEqual(result, {"resolved": True})
        call_kwargs = mock_client.invoke.call_args.kwargs
        self.assertEqual(call_kwargs["command"], "resolve_stuck_submission")
        self.assertEqual(call_kwargs["payload"]["client_order_id"], "ord_stuck")
        service._invalidate_cache.assert_called_once()

    async def test_refresh_exchange_state_proxies_to_client(self) -> None:
        mock_client = AsyncMock()
        mock_client.invoke.return_value = {"status": "completed"}
        service = self._make_service_with_mock_runtime(client=mock_client)
        service._invalidate_cache = Mock()

        result = await service.refresh_exchange_state(
            blocker="some_blocker",
            parent_intent_id="intent_1",
            reason="proxy_test",
            actor_role="admin",
        )
        self.assertEqual(result, {"status": "completed"})
        call_kwargs = mock_client.invoke.call_args.kwargs
        self.assertEqual(call_kwargs["command"], "refresh_exchange_state")
        self.assertEqual(call_kwargs["payload"]["blocker"], "some_blocker")
        self.assertEqual(call_kwargs["payload"]["parent_intent_id"], "intent_1")
        service._invalidate_cache.assert_called_once()

    async def test_retry_limit_lookup_proxies_to_client(self) -> None:
        mock_client = AsyncMock()
        mock_client.invoke.return_value = {"status": "completed", "order": None}
        service = self._make_service_with_mock_runtime(client=mock_client)
        service._invalidate_cache = Mock()

        result = await service.retry_limit_lookup(
            parent_intent_id="intent_2",
            reason="proxy_test",
            actor_role="admin",
        )
        self.assertEqual(result, {"status": "completed", "order": None})
        call_kwargs = mock_client.invoke.call_args.kwargs
        self.assertEqual(call_kwargs["command"], "retry_limit_lookup")
        self.assertEqual(call_kwargs["payload"]["parent_intent_id"], "intent_2")
        service._invalidate_cache.assert_called_once()

    async def test_safe_cancel_exit_execution_proxies_to_client(self) -> None:
        mock_client = AsyncMock()
        mock_client.invoke.return_value = {"status": "completed", "orders": []}
        service = self._make_service_with_mock_runtime(client=mock_client)
        service._invalidate_cache = Mock()

        result = await service.safe_cancel_exit_execution(
            parent_intent_id="intent_3",
            reason="proxy_test",
            actor_role="admin",
        )
        self.assertEqual(result, {"status": "completed", "orders": []})
        call_kwargs = mock_client.invoke.call_args.kwargs
        self.assertEqual(call_kwargs["command"], "safe_cancel_exit_execution")
        self.assertEqual(call_kwargs["payload"]["parent_intent_id"], "intent_3")
        service._invalidate_cache.assert_called_once()

    async def test_proxy_mutation_invalidates_cache_after_remote_failure(self) -> None:
        mock_client = AsyncMock()
        mock_client.invoke.side_effect = RuntimeError("remote_failed_after_mutation")
        service = self._make_service_with_mock_runtime(client=mock_client)
        service._invalidate_cache = Mock()

        with self.assertRaisesRegex(RuntimeError, "remote_failed_after_mutation"):
            await service.validate_reconciliation(reason="proxy_failure", actor_role="admin")

        service._invalidate_cache.assert_called_once()


# ────────────────────────────────────────────────────────────────
# 2. Worker dispatch 测试：8 个命令全覆盖
# ────────────────────────────────────────────────────────────────


class TestWorkerDispatchAllCommands(unittest.IsolatedAsyncioTestCase):
    """验证 Worker 能正确 dispatch 所有 8 个已注册命令。"""

    ALL_COMMANDS = [
        "rebaseline",
        "resume",
        "validate_reconciliation",
        "cancel_order",
        "resolve_stuck_submission",
        "refresh_exchange_state",
        "retry_limit_lookup",
        "safe_cancel_exit_execution",
    ]

    async def test_all_commands_dispatch_to_handlers(self) -> None:
        """每个命令发一条请求，对应 handler 被调用且返回正确结果。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        dispatched: dict[str, dict[str, Any]] = {}

        def _make_handler(cmd_name: str):
            async def _handler(payload: dict[str, Any]) -> dict[str, Any]:
                dispatched[cmd_name] = payload
                return {"command": cmd_name, "handled": True}
            return _handler

        handlers = {cmd: _make_handler(cmd) for cmd in self.ALL_COMMANDS}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers=handlers,
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
        )
        await client.bootstrap()

        for cmd in self.ALL_COMMANDS:
            result = await client.invoke(
                command=cmd,
                payload={"reason": f"test_{cmd}", "actor_role": "admin"},
            )
            self.assertEqual(result["command"], cmd)
            self.assertTrue(result["handled"])
            self.assertEqual(dispatched[cmd]["reason"], f"test_{cmd}")

        await client.stop()
        await worker.stop()


# ────────────────────────────────────────────────────────────────
# 3. E2E roundtrip：新命令代表性测试
# ────────────────────────────────────────────────────────────────


class TestNewCommandE2ERoundtrip(unittest.IsolatedAsyncioTestCase):
    """新增命令在 InMemoryEventBus 上的完整 roundtrip。"""

    async def _setup_e2e(self, handlers: dict[str, Any]):
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers=handlers,
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
        )
        await client.bootstrap()
        return bus, worker, client

    async def test_cancel_order_e2e_success(self) -> None:
        received: list[dict] = []

        async def _handler(payload: dict[str, Any]) -> dict[str, Any]:
            received.append(payload)
            return {"order": {"client_order_id": payload["client_order_id"], "status": "cancelled"}}

        _, worker, client = await self._setup_e2e({"cancel_order": _handler})

        result = await client.invoke(
            command="cancel_order",
            payload={
                "client_order_id": "ord_e2e_1",
                "reason": "e2e_cancel",
                "actor_role": "admin",
                "actor_identity": "tester",
                "auth_source": "password",
            },
        )
        self.assertEqual(result["order"]["client_order_id"], "ord_e2e_1")
        self.assertEqual(result["order"]["status"], "cancelled")
        self.assertEqual(received[0]["client_order_id"], "ord_e2e_1")

        await client.stop()
        await worker.stop()

    async def test_refresh_exchange_state_e2e_success(self) -> None:
        async def _handler(payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "completed",
                "message": "刷新成功",
                "blocker": payload.get("blocker"),
            }

        _, worker, client = await self._setup_e2e({"refresh_exchange_state": _handler})

        result = await client.invoke(
            command="refresh_exchange_state",
            payload={
                "blocker": "risk_snapshot_missing",
                "parent_intent_id": "intent_e2e",
                "reason": "e2e_refresh",
                "actor_role": "admin",
            },
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["blocker"], "risk_snapshot_missing")

        await client.stop()
        await worker.stop()

    async def test_safe_cancel_exit_execution_e2e_success(self) -> None:
        async def _handler(payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "completed",
                "message": "已对退出父任务发起安全取消。",
                "parent_exit_intent": {"parent_intent_id": payload.get("parent_intent_id")},
                "orders": [],
            }

        _, worker, client = await self._setup_e2e({"safe_cancel_exit_execution": _handler})

        result = await client.invoke(
            command="safe_cancel_exit_execution",
            payload={
                "parent_intent_id": "intent_cancel_e2e",
                "reason": "e2e_safe_cancel",
                "actor_role": "admin",
            },
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["parent_exit_intent"]["parent_intent_id"], "intent_cancel_e2e")

        await client.stop()
        await worker.stop()

    async def test_handler_error_surfaces_as_remote_error(self) -> None:
        """新命令 handler 抛异常时也应正确传递为 OperatorCommandRemoteError。"""
        async def _failing_handler(payload: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("cancel_order_not_found")

        _, worker, client = await self._setup_e2e({"cancel_order": _failing_handler})

        with self.assertRaises(OperatorCommandRemoteError) as ctx:
            await client.invoke(
                command="cancel_order",
                payload={"client_order_id": "ord_missing", "reason": "test"},
            )
        self.assertEqual(ctx.exception.error_type, "ValueError")
        self.assertEqual(ctx.exception.error_message, "cancel_order_not_found")

        await client.stop()
        await worker.stop()

    async def test_concurrent_new_commands_are_serialized(self) -> None:
        """Worker asyncio.Lock 保证新命令也是 serial 执行的。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        call_order: list[str] = []

        async def _slow_cancel(payload: dict[str, Any]) -> dict[str, Any]:
            tag = payload.get("tag", "?")
            call_order.append(f"{tag}:start")
            await asyncio.sleep(0.01)
            call_order.append(f"{tag}:end")
            return {"tag": tag}

        async def _slow_refresh(payload: dict[str, Any]) -> dict[str, Any]:
            tag = payload.get("tag", "?")
            call_order.append(f"{tag}:start")
            await asyncio.sleep(0.01)
            call_order.append(f"{tag}:end")
            return {"tag": tag}

        worker = OperatorCommandWorker(
            bus=bus,
            process_role="execution",
            logger=_make_logger(),
            command_handlers={
                "cancel_order": _slow_cancel,
                "refresh_exchange_state": _slow_refresh,
            },
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=bus,
            process_role="gateway",
            logger=_make_logger(),
            timeout_seconds=5.0,
        )
        await client.bootstrap()

        results = await asyncio.gather(
            client.invoke(command="cancel_order", payload={"tag": "A"}),
            client.invoke(command="refresh_exchange_state", payload={"tag": "B"}),
        )

        self.assertEqual({r["tag"] for r in results}, {"A", "B"})
        # Serial: 不能出现 A:start → B:start → A:end → B:end
        self.assertIn(
            call_order,
            [
                ["A:start", "A:end", "B:start", "B:end"],
                ["B:start", "B:end", "A:start", "A:end"],
            ],
        )

        await client.stop()
        await worker.stop()
