"""AI command proxy 真 NATS 集成测试。

对称版本见 ``tests/integration/test_operator_command_bridge_nats_roundtrip.py``
（execution 侧 rebaseline 代理）。本文件覆盖 AI 侧 3 条命令的真 JetStream
roundtrip，确认 gateway↔decision 的代理路径在真实 NATS 上可用。

覆盖：

1. **ai_operating_mode_select roundtrip**
   - gateway ``OperatorCommandClient.invoke("ai_operating_mode_select", ...)``
     经 ``system.ai_command_requests`` 推到 decision
   - decision ``OperatorCommandWorker`` dispatcher 被触发，拿到 mode / reason /
     actor_* 完整透传
   - result dict 经 ``system.ai_command_responses`` 回 gateway

2. **ai_review_restore + ai_review_degrade_to_baseline roundtrip**
   - 同一 worker 注册 3 条命令，两次 invoke 必须都打到正确的 handler

3. **远端抛 ValueError → RemoteError**
   - 和 execution 侧对称：业务异常被 worker 捕获打包成 success=False

4. **topic 隔离**
   - AI 流量走 AI_COMMAND_* topic；同时监听 OPERATOR_COMMAND_REQUESTS 的
     subscriber 不应收到任何 AI 消息（防两条代理链路 cross-talk）

运行条件同 ``test_operator_command_bridge_nats_roundtrip``：
- docker
- ``pip install -e .[nats-integration]``
- ``AATS_RUN_NATS_INTEGRATION=1``

Scope：本文件只验 NATS 层 AI 代理。完整的 "UI POST /ai/operating-mode/select
经 gateway→decision 真跑到 ai_service" 链路由 deploy/wsl2-dev smoke test 覆盖。
"""
from __future__ import annotations

import asyncio
import logging
import os
import unittest
from typing import Any

from aats.events import topics as _topics

try:
    from testcontainers.core.container import DockerContainer  # type: ignore[import-not-found]
    from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    DockerContainer = None  # type: ignore[assignment,misc]
    wait_for_logs = None  # type: ignore[assignment]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import nats  # type: ignore[import-not-found]  # noqa: F401

    _NATS_PY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NATS_PY_AVAILABLE = False


_INTEGRATION_ENV_FLAG = "AATS_RUN_NATS_INTEGRATION"
_SHOULD_RUN = (
    os.getenv(_INTEGRATION_ENV_FLAG) == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _NATS_PY_AVAILABLE
)


def _start_nats_container() -> "DockerContainer":
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
class TestAICommandBridgeNatsRoundTrip(unittest.IsolatedAsyncioTestCase):
    """Client（gateway role）+ Worker（decision role）挂在真实 NATS 上的 AI roundtrip。"""

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
        await _purge_all_streams(self.nats_url)

    async def _make_bus(self, *, consumer_role: str):
        from aats.bus.nats_bus import NatsBusConfig, NatsEventBus

        bus = NatsEventBus(
            config=NatsBusConfig(
                servers=(self.nats_url,),
                stream_name="AATS_AI_CMD_RT",
                ack_wait_seconds=5.0,
            ),
            event_store=None,
            persistence_mode="permissive",
            consumer_role=consumer_role,
        )
        await bus.start(
            topics=[
                _topics.AI_COMMAND_REQUESTS,
                _topics.AI_COMMAND_RESPONSES,
                _topics.OPERATOR_COMMAND_REQUESTS,
            ]
        )
        return bus

    async def test_ai_operating_mode_select_roundtrip_over_real_nats(self) -> None:
        from aats.services.operator.command_bridge import (
            OperatorCommandClient,
            OperatorCommandWorker,
        )

        gateway_bus = await self._make_bus(consumer_role="gateway_ai_test")
        decision_bus = await self._make_bus(consumer_role="decision_ai_test")

        received_payloads: list[dict[str, Any]] = []

        async def _handler(payload: dict[str, Any]) -> dict[str, Any]:
            received_payloads.append(payload)
            return {
                "status": "completed",
                "effective_operating_mode": payload["mode"],
            }

        worker = OperatorCommandWorker(
            bus=decision_bus,
            process_role="decision",
            logger=logging.getLogger("test.ai_command.worker"),
            command_handlers={"ai_operating_mode_select": _handler},
            request_topic=_topics.AI_COMMAND_REQUESTS,
            response_topic=_topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=gateway_bus,
            process_role="gateway",
            logger=logging.getLogger("test.ai_command.client"),
            timeout_seconds=10.0,
            request_topic=_topics.AI_COMMAND_REQUESTS,
            response_topic=_topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await client.bootstrap()

        await asyncio.sleep(0.5)

        try:
            result = await client.invoke(
                command="ai_operating_mode_select",
                payload={
                    "mode": "ai_decision_maker",
                    "reason": "nats_ai_roundtrip",
                    "actor_role": "admin",
                    "actor_identity": "integration_tester",
                    "auth_source": "session",
                },
            )

            self.assertEqual(len(received_payloads), 1)
            self.assertEqual(received_payloads[0]["mode"], "ai_decision_maker")
            self.assertEqual(received_payloads[0]["actor_identity"], "integration_tester")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["effective_operating_mode"], "ai_decision_maker")
            self.assertEqual(client._pending, {})
        finally:
            await client.stop()
            await worker.stop()
            await gateway_bus.close()
            await decision_bus.close()

    async def test_ai_review_restore_and_degrade_both_dispatch_over_real_nats(self) -> None:
        from aats.services.operator.command_bridge import (
            OperatorCommandClient,
            OperatorCommandWorker,
        )

        gateway_bus = await self._make_bus(consumer_role="gateway_ai_test")
        decision_bus = await self._make_bus(consumer_role="decision_ai_test")

        calls: list[str] = []

        async def _restore(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append("restore")
            return {"status": "completed"}

        async def _degrade(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append("degrade")
            return {"status": "completed"}

        worker = OperatorCommandWorker(
            bus=decision_bus,
            process_role="decision",
            logger=logging.getLogger("test.ai_command.worker"),
            command_handlers={
                "ai_review_restore": _restore,
                "ai_review_degrade_to_baseline": _degrade,
            },
            request_topic=_topics.AI_COMMAND_REQUESTS,
            response_topic=_topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=gateway_bus,
            process_role="gateway",
            logger=logging.getLogger("test.ai_command.client"),
            timeout_seconds=10.0,
            request_topic=_topics.AI_COMMAND_REQUESTS,
            response_topic=_topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await client.bootstrap()

        await asyncio.sleep(0.5)

        try:
            await client.invoke(
                command="ai_review_restore",
                payload={"reason": "r", "actor_role": "admin"},
            )
            await client.invoke(
                command="ai_review_degrade_to_baseline",
                payload={"reason": "d", "actor_role": "admin"},
            )
            self.assertEqual(calls, ["restore", "degrade"])
        finally:
            await client.stop()
            await worker.stop()
            await gateway_bus.close()
            await decision_bus.close()

    async def test_remote_handler_error_maps_to_remote_error_over_real_nats(self) -> None:
        from aats.services.operator.command_bridge import (
            OperatorCommandClient,
            OperatorCommandRemoteError,
            OperatorCommandWorker,
        )

        gateway_bus = await self._make_bus(consumer_role="gateway_ai_test")
        decision_bus = await self._make_bus(consumer_role="decision_ai_test")

        async def _failing(payload: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("ai_operating_mode_freeze_not_elapsed")

        worker = OperatorCommandWorker(
            bus=decision_bus,
            process_role="decision",
            logger=logging.getLogger("test.ai_command.worker"),
            command_handlers={"ai_operating_mode_select": _failing},
            request_topic=_topics.AI_COMMAND_REQUESTS,
            response_topic=_topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=gateway_bus,
            process_role="gateway",
            logger=logging.getLogger("test.ai_command.client"),
            timeout_seconds=10.0,
            request_topic=_topics.AI_COMMAND_REQUESTS,
            response_topic=_topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await client.bootstrap()

        await asyncio.sleep(0.5)

        try:
            with self.assertRaises(OperatorCommandRemoteError) as ctx:
                await client.invoke(
                    command="ai_operating_mode_select",
                    payload={"mode": "ai_decision_maker", "reason": "r"},
                )
            self.assertEqual(ctx.exception.error_type, "ValueError")
            self.assertEqual(
                ctx.exception.error_message,
                "ai_operating_mode_freeze_not_elapsed",
            )
            self.assertEqual(client._pending, {})
        finally:
            await client.stop()
            await worker.stop()
            await gateway_bus.close()
            await decision_bus.close()

    async def test_ai_traffic_does_not_leak_to_operator_command_topic(self) -> None:
        """AI 代理走 AI_COMMAND_*；同时挂到 OPERATOR_COMMAND_REQUESTS 的订阅者
        必须收不到任何 AI 命令——防止两条代理链路 topic cross-talk 把 execution
        worker 激活错的 handler。"""
        from aats.services.operator.command_bridge import (
            OperatorCommandClient,
            OperatorCommandWorker,
        )

        gateway_bus = await self._make_bus(consumer_role="gateway_ai_test")
        decision_bus = await self._make_bus(consumer_role="decision_ai_test")
        sniffer_bus = await self._make_bus(consumer_role="exec_sniffer_test")

        leaked: list[Any] = []

        async def _sniff(msg: dict[str, Any]) -> None:
            leaked.append(msg)

        await sniffer_bus.subscribe(_topics.OPERATOR_COMMAND_REQUESTS, _sniff)

        async def _handler(payload: dict[str, Any]) -> dict[str, Any]:
            return {"status": "completed"}

        worker = OperatorCommandWorker(
            bus=decision_bus,
            process_role="decision",
            logger=logging.getLogger("test.ai_command.worker"),
            command_handlers={"ai_review_restore": _handler},
            request_topic=_topics.AI_COMMAND_REQUESTS,
            response_topic=_topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await worker.bootstrap()

        client = OperatorCommandClient(
            bus=gateway_bus,
            process_role="gateway",
            logger=logging.getLogger("test.ai_command.client"),
            timeout_seconds=10.0,
            request_topic=_topics.AI_COMMAND_REQUESTS,
            response_topic=_topics.AI_COMMAND_RESPONSES,
            component_name="ai_command",
        )
        await client.bootstrap()

        await asyncio.sleep(0.5)

        try:
            await client.invoke(
                command="ai_review_restore",
                payload={"reason": "topic_isolation_check", "actor_role": "admin"},
            )
            await asyncio.sleep(0.3)
            self.assertEqual(leaked, [])
        finally:
            await client.stop()
            await worker.stop()
            await gateway_bus.close()
            await decision_bus.close()
            await sniffer_bus.close()


if __name__ == "__main__":
    unittest.main()
