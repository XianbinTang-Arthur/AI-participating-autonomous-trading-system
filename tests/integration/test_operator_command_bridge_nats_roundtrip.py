"""Slice 4-proc operator command proxy 真 NATS 集成测试。

设计文档：docs/task/slice_4proc_operator_command_proxy_fix_design.md §6.2

覆盖：

1. **真 NATS roundtrip**
   - 用 testcontainers 起一台 ``nats:2.10-alpine`` + JetStream
   - 构造两条独立的 ``NatsEventBus`` 实例：一条模拟 gateway 进程的 client，
     一条模拟 execution 进程的 worker，都连到同一 NATS server
   - gateway 端 ``OperatorCommandClient.invoke("rebaseline", ...)`` 经由 NATS
     ``system.operator_command_requests`` 把请求送到 execution 端
   - execution 端 ``OperatorCommandWorker`` 的 dispatcher 被触发，返回的
     result 被封成 ``OperatorCommandResponse`` 经由 ``system.operator_command_responses``
     回到 gateway
   - gateway 端 ``invoke()`` 按 correlation_id 匹配 future，返回 result dict

2. **远端 handler 抛错 → RemoteError**
   - dispatcher 抛 ValueError → worker 捕获打包成 success=False 响应 → gateway
     端 ``OperatorCommandRemoteError`` 带远端 error_type / error_message

这是对 unit 层（tests/unit/test_operator_command_bridge.py，跑 InMemoryEventBus）
的补充——验证真正的 JetStream + durable consumer + envelope 序列化路径对
correlation_id pattern 的支持没掉链。

⚠️ **运行条件**：
- 需要 docker（Windows 上请确保 Docker Desktop 正在运行）
- 需要安装可选依赖：``pip install -e .[nats-integration]``
- 需要设置环境变量 ``AATS_RUN_NATS_INTEGRATION=1`` 才会真正运行
- 默认情况下整个文件被 ``unittest.skipUnless`` 跳过，不会拖慢 CI / 单测

⚠️ **scope 注意**：本文件只验证 NATS 层。完整的 "gateway HTTP endpoint 经
OperatorQueryService.rebaseline 走代理分支到 execution 进程" 真跑链路由
deploy/wsl2-dev/docker-compose.aats.{spot,derivatives}.yml smoke test 覆盖，
那一步需要 Postgres + Redis + 真 OKX demo 凭证，留给 runbook 走。
"""
from __future__ import annotations

import asyncio
import logging
import os
import unittest
from typing import Any

from aats.events import topics as _topics

# 软依赖检查：testcontainers + nats-py 可能没装（属于 nats-integration extra）
try:
    from testcontainers.core.container import DockerContainer  # type: ignore[import-not-found]
    from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover - 没装就跳过
    DockerContainer = None  # type: ignore[assignment,misc]
    wait_for_logs = None  # type: ignore[assignment]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import nats  # type: ignore[import-not-found]  # noqa: F401

    _NATS_PY_AVAILABLE = True
except ImportError:  # pragma: no cover - 没装就跳过
    _NATS_PY_AVAILABLE = False


_INTEGRATION_ENV_FLAG = "AATS_RUN_NATS_INTEGRATION"
_SHOULD_RUN = (
    os.getenv(_INTEGRATION_ENV_FLAG) == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _NATS_PY_AVAILABLE
)


def _start_nats_container() -> "DockerContainer":
    """启动一个 nats:2.10-alpine 容器，开 JetStream（与 test_nats_event_bus_roundtrip 同款）。"""
    container = (
        DockerContainer("nats:2.10-alpine")
        .with_exposed_ports(4222, 8222)
        .with_command("-js -m 8222")
    )
    container.start()
    wait_for_logs(container, "Server is ready", timeout=30)
    return container


def _client_url(container: "DockerContainer") -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(4222)
    return f"nats://{host}:{port}"


async def _purge_all_streams(nats_url: str) -> None:
    """工具：清掉 NATS server 上所有 stream，给下一个测试让位。"""
    nc = await nats.connect(nats_url)
    try:
        js = nc.jetstream()
        try:
            names = await js.streams_info()
        except Exception:
            names = []
        for s in names:
            try:
                await js.delete_stream(s.config.name)
            except Exception:
                pass
    finally:
        await nc.drain()


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[nats-integration] to run NATS integration tests",
)
class TestOperatorCommandBridgeNatsRoundTrip(unittest.IsolatedAsyncioTestCase):
    """Client（gateway role）+ Worker（execution role）挂在真实 NATS 上的 roundtrip。"""

    container: "DockerContainer"  # type: ignore[assignment]
    nats_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = _start_nats_container()
        cls.nats_url = _client_url(cls.container)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.container.stop()
        except Exception:
            pass

    async def asyncTearDown(self) -> None:
        # 每个 case 结束清 stream，避免 "subjects overlap with an existing stream"
        await _purge_all_streams(self.nats_url)

    async def _make_bus(
        self,
        *,
        consumer_role: str,
    ):
        """构造一条连到 testcontainer NATS 的 NatsEventBus。

        * 同一个 container 下两条 bus 必须共用 stream name，否则 JetStream
          subject overlap 会炸。
        * consumer_role 让两条 bus 的 durable consumer name 不同，gateway /
          execution 互不干扰。
        """
        from aats.bus.nats_bus import NatsBusConfig, NatsEventBus

        bus = NatsEventBus(
            config=NatsBusConfig(
                servers=(self.nats_url,),
                stream_name="AATS_OPCMD_RT",
                ack_wait_seconds=5.0,
            ),
            event_store=None,
            persistence_mode="permissive",
            consumer_role=consumer_role,
        )
        await bus.start(
            topics=[
                _topics.OPERATOR_COMMAND_REQUESTS,
                _topics.OPERATOR_COMMAND_RESPONSES,
            ]
        )
        return bus

    async def test_rebaseline_roundtrip_over_real_nats(self) -> None:
        """gateway client.invoke → execution worker dispatch → gateway result。

        断言：
            * dispatcher 被调用一次，payload 字段与 invoke 时传的一致
            * client.invoke 返回的 dict 与 dispatcher 返回的 dict 一致
            * client._pending 最终清空（finally 分支执行）
        """
        from aats.services.operator.command_bridge import (
            OperatorCommandClient,
            OperatorCommandWorker,
        )

        gateway_bus = await self._make_bus(consumer_role="gateway_test")
        execution_bus = await self._make_bus(consumer_role="execution_test")

        received_payloads: list[dict[str, Any]] = []

        async def _rebaseline_handler(payload: dict[str, Any]) -> dict[str, Any]:
            received_payloads.append(payload)
            return {
                "baseline_status": "baseline_imported",
                "baseline_event_id": "evt_nats_rt",
                "recovery_state": "rebaseline_pending",
            }

        worker = OperatorCommandWorker(
            bus=execution_bus,
            process_role="execution",
            logger=logging.getLogger("test.operator_command.worker"),
            command_handlers={"rebaseline": _rebaseline_handler},
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=gateway_bus,
            process_role="gateway",
            logger=logging.getLogger("test.operator_command.client"),
            timeout_seconds=10.0,
        )
        await client.bootstrap()

        # 给 durable consumer 一点时间 attach 上 stream（nats-py 没有主动
        # "ready" 信号，这里与 test_nats_event_bus_roundtrip 保持一致用 sleep）
        await asyncio.sleep(0.5)

        try:
            result = await client.invoke(
                command="rebaseline",
                payload={
                    "reason": "nats_roundtrip_test",
                    "actor_role": "admin",
                    "actor_identity": "integration_tester",
                    "auth_source": "password",
                },
            )

            self.assertEqual(len(received_payloads), 1)
            self.assertEqual(received_payloads[0]["reason"], "nats_roundtrip_test")
            self.assertEqual(received_payloads[0]["actor_role"], "admin")
            self.assertEqual(received_payloads[0]["actor_identity"], "integration_tester")

            self.assertEqual(result["baseline_status"], "baseline_imported")
            self.assertEqual(result["baseline_event_id"], "evt_nats_rt")
            self.assertEqual(result["recovery_state"], "rebaseline_pending")

            self.assertEqual(client._pending, {})
        finally:
            await client.stop()
            await worker.stop()
            await gateway_bus.close()
            await execution_bus.close()

    async def test_remote_handler_error_roundtrip_over_real_nats(self) -> None:
        """dispatcher 抛 ValueError → client 看到 OperatorCommandRemoteError。"""
        from aats.services.operator.command_bridge import (
            OperatorCommandClient,
            OperatorCommandRemoteError,
            OperatorCommandWorker,
        )

        gateway_bus = await self._make_bus(consumer_role="gateway_test")
        execution_bus = await self._make_bus(consumer_role="execution_test")

        async def _failing_handler(payload: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("rebaseline_requires_okx_account_read")

        worker = OperatorCommandWorker(
            bus=execution_bus,
            process_role="execution",
            logger=logging.getLogger("test.operator_command.worker"),
            command_handlers={"rebaseline": _failing_handler},
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=gateway_bus,
            process_role="gateway",
            logger=logging.getLogger("test.operator_command.client"),
            timeout_seconds=10.0,
        )
        await client.bootstrap()

        await asyncio.sleep(0.5)

        try:
            with self.assertRaises(OperatorCommandRemoteError) as ctx:
                await client.invoke(
                    command="rebaseline",
                    payload={"reason": "error_roundtrip_test"},
                )
            self.assertEqual(ctx.exception.error_type, "ValueError")
            self.assertEqual(
                ctx.exception.error_message,
                "rebaseline_requires_okx_account_read",
            )
            # finally 分支必须清空 _pending
            self.assertEqual(client._pending, {})
        finally:
            await client.stop()
            await worker.stop()
            await gateway_bus.close()
            await execution_bus.close()


if __name__ == "__main__":
    unittest.main()
