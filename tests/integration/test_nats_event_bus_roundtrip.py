"""Stage 4/5 集成测试：NATS JetStream 真实容器 round-trip。

覆盖：

1. **Step 5 — single-bus round-trip**
   - 单个 NatsEventBus 连接 testcontainer 起的真 NATS server
   - publish 一条 EventEnvelope，subscribe 同一 topic 收到完整 envelope
   - 验证 JetStream stream / durable consumer 创建路径 + AckPolicy 真正落地

2. **Step 5 — HybridEventBus 路由验证**
   - HybridEventBus.publish 在真实 critical topic（_topics.AI_DECISION_BRIEFS）上
     真的把消息送到 NATS
   - HybridEventBus.publish 在真实 observer topic（_topics.HEALTH_SNAPSHOTS）上
     不走 NATS，停留在内存（通过 NATS subscribe 不到来验证）
   - **5c 修复后**：所有测试都使用 aats.events.topics 模块的真实常量，确保
     路由表归类正确性也被端到端验证

3. **Step 6 — 真跨进程 round-trip**
   - 用 multiprocessing.Process spawn 一个 publisher 子进程
   - 子进程构造自己的 NatsEventBus，连接同一 testcontainer NATS server，publish
   - 主进程构造 subscriber NatsEventBus，订阅前先 ensure_stream（durable 持久化），
     等待消息到达
   - 这是 4 进程拓扑里 "gateway publish → decision subscribe" 模式的微缩验证

⚠️ **运行条件**：
- 需要规定 WSL2 运行环境（``~/aats-venv``）可访问 Docker daemon
- 需要安装可选依赖：``pip install -e .[nats-integration]``
- 需要设置环境变量 ``AATS_RUN_NATS_INTEGRATION=1`` 才会真正运行
- 默认情况下整个文件被 ``unittest.skipUnless`` 跳过，不会拖慢 CI / 单测

⚠️ **scope 注意**：本文件验证的是 NATS JetStream 路径在 EventBus 层的正确性。
FS-016 针对现有四主服务的 full-down/restart/disconnect 故障注入矩阵仍为 OPEN；
本文件不能替代该部署态证据。
本文件用 multiprocessing 子进程作为 "可移植的 4 进程"代理来验证跨进程语义。
"""
from __future__ import annotations

import asyncio
import multiprocessing
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
    """启动一个 nats:2.10-alpine 容器，开 JetStream。

    与 deploy/wsl2-dev/docker-compose.yml 用同一镜像版本，保持一致性。
    -js 参数启用 JetStream（默认是关闭的）。
    """
    container = (
        DockerContainer("nats:2.10-alpine")
        .with_exposed_ports(4222, 8222)
        .with_command("-js -m 8222")
    )
    container.start()
    # 等到 server 真的接受连接，避免 connect 超时
    wait_for_logs(container, "Server is ready", timeout=30)
    return container


def _client_url(container: "DockerContainer") -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(4222)
    return f"nats://{host}:{port}"


async def _purge_all_streams(nats_url: str) -> None:
    """工具函数：清空一个 NATS server 上的所有 JetStream stream。

    用途：每个测试用例之间隔离。同一容器内多个测试都会注册 critical topic
    （比如 _topics.AI_DECISION_BRIEFS），不同 stream name 想 claim 同一个
    subject 会被 NATS 拒为 'subjects overlap with an existing stream'。在
    asyncTearDown 调用本函数 + 重新启动 fresh stream 状态。
    """
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
class TestNatsEventBusRoundTrip(unittest.IsolatedAsyncioTestCase):
    """单 NatsEventBus 实例的 connect → ensure_stream → publish → subscribe round-trip。"""

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

    async def test_single_nats_bus_publish_subscribe_round_trip(self) -> None:
        """单 bus：publish → subscribe 必须收到完整 EventEnvelope。"""
        from aats.bus.nats_bus import NatsBusConfig, NatsEventBus
        from aats.schemas.common import EventEnvelope

        bus = NatsEventBus(
            config=NatsBusConfig(
                servers=(self.nats_url,),
                stream_name="AATS_RT_SINGLE",
                ack_wait_seconds=5.0,
            ),
            event_store=None,
            persistence_mode="permissive",
            consumer_role="test",
        )
        try:
            await bus.start(topics=[_topics.AI_DECISION_BRIEFS])

            received: list[dict[str, Any]] = []

            async def handler(message: dict) -> None:
                received.append(message)

            await bus.subscribe(_topics.AI_DECISION_BRIEFS, handler)
            # 给 durable consumer 一点时间 attach 上 stream
            await asyncio.sleep(0.5)

            envelope = EventEnvelope(
                event_type="ai_decision_brief",
                source_component="test_single_round_trip",
                topic=_topics.AI_DECISION_BRIEFS,
                key="symbol-BTC",
                payload={"decision_id": "rt-1", "side": "buy"},
            )
            await bus.publish_envelope(envelope, persist=False)

            # 等最多 5 秒
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.1)

            self.assertEqual(len(received), 1)
            msg = received[0]
            self.assertEqual(msg["topic"], _topics.AI_DECISION_BRIEFS)
            self.assertEqual(msg["key"], "symbol-BTC")
            self.assertEqual(msg["payload"]["payload"]["decision_id"], "rt-1")
        finally:
            await bus.close()

    async def test_existing_critical_durable_updates_in_place_without_cursor_reset(
        self,
    ) -> None:
        """v1 max_ack_pending=256 → gated v2=1 必须保留 durable cursor。"""

        from aats.bus.nats_bus import (
            NatsBusConfig,
            NatsDeliveryGate,
            NatsEventBus,
            StreamSpec,
        )
        from aats.schemas.common import EventEnvelope

        topic = _topics.ORDER_INTENTS
        stream_name = "AATS_RT_DURABLE_UPGRADE"
        stream = StreamSpec(
            name=stream_name,
            topics=frozenset({topic}),
            max_age_seconds=300.0,
            max_bytes=16 * 1024 * 1024,
            max_msgs=10_000,
            max_msg_size=1024 * 1024,
        )
        legacy_config = NatsBusConfig(
            servers=(self.nats_url,),
            streams=(stream,),
            ack_wait_seconds=0.3,
            max_ack_pending=256,
            flow_control=False,
        )
        upgraded_config = NatsBusConfig(
            servers=(self.nats_url,),
            streams=(stream,),
            ack_wait_seconds=0.3,
            max_ack_pending=256,
            flow_control=True,
        )
        durable = legacy_config.durable_name_for("execution", topic)
        legacy = NatsEventBus(
            config=legacy_config,
            consumer_role="execution",
        )
        raw_client = await nats.connect(self.nats_url)
        raw_js = raw_client.jetstream()
        legacy_received = asyncio.Event()

        async def _legacy_handler(_message: dict[str, Any]) -> None:
            legacy_received.set()

        try:
            await legacy.start()
            await legacy.subscribe(topic, _legacy_handler)
            first = EventEnvelope(
                event_type="order_intent",
                source_component="durable_upgrade_test",
                topic=topic,
                key="first",
                payload={"sequence": 1},
            )
            await legacy.publish_envelope(first, persist=False)
            await asyncio.wait_for(legacy_received.wait(), timeout=3.0)
            for _ in range(50):
                before = await raw_js.consumer_info(stream_name, durable)
                if before.num_ack_pending == 0:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(before.config.max_ack_pending, 256)
            before_ack_stream = before.ack_floor.stream_seq
            before_ack_consumer = before.ack_floor.consumer_seq
            before_delivered_stream = before.delivered.stream_seq
            before_delivered_consumer = before.delivered.consumer_seq
            before_created = before.created
            before_name = before.name
            await legacy.close()

            pending = EventEnvelope(
                event_type="order_intent",
                source_component="durable_upgrade_test",
                topic=topic,
                key="pending",
                payload={"sequence": 2},
            )
            await raw_js.publish(
                legacy_config.subject_for(topic),
                pending.model_dump_json().encode("utf-8"),
                headers={"Nats-Msg-Id": pending.event_id},
            )

            gate = NatsDeliveryGate()
            upgraded = NatsEventBus(
                config=upgraded_config,
                consumer_role="execution",
                delivery_gate=gate,
            )
            upgraded_received = asyncio.Event()
            upgraded_keys: list[str] = []

            async def _upgraded_handler(message: dict[str, Any]) -> None:
                upgraded_keys.append(str(message["key"]))
                upgraded_received.set()

            try:
                await upgraded.start()
                await upgraded.subscribe(topic, _upgraded_handler)
                for _ in range(50):
                    after_bind = await raw_js.consumer_info(stream_name, durable)
                    if after_bind.num_ack_pending == 1:
                        break
                    await asyncio.sleep(0.02)

                self.assertEqual(after_bind.config.max_ack_pending, 1)
                self.assertEqual(after_bind.name, before_name)
                self.assertEqual(after_bind.created, before_created)
                self.assertGreaterEqual(
                    after_bind.ack_floor.stream_seq,
                    before_ack_stream,
                )
                self.assertGreaterEqual(
                    after_bind.ack_floor.consumer_seq,
                    before_ack_consumer,
                )
                self.assertGreaterEqual(
                    after_bind.delivered.stream_seq,
                    before_delivered_stream,
                )
                self.assertGreaterEqual(
                    after_bind.delivered.consumer_seq,
                    before_delivered_consumer,
                )
                self.assertEqual(after_bind.num_ack_pending, 1)
                self.assertFalse(upgraded_received.is_set())

                await upgraded.activate_delivery()
                await asyncio.wait_for(upgraded_received.wait(), timeout=3.0)
                for _ in range(50):
                    final_info = await raw_js.consumer_info(stream_name, durable)
                    if final_info.num_ack_pending == 0:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(final_info.num_ack_pending, 0)
                self.assertGreaterEqual(
                    final_info.ack_floor.stream_seq,
                    after_bind.delivered.stream_seq,
                )
                self.assertEqual(upgraded_keys, ["pending"])
            finally:
                await upgraded.close()
        finally:
            if legacy._client is not None:
                await legacy.close()
            await raw_client.drain()

    async def test_deleted_critical_durable_is_terminal_while_core_stays_connected(
        self,
    ) -> None:
        """JetStream consumer 删除不能被健康的 core TCP 连接掩盖。"""

        from aats.bus.nats_bus import (
            NatsBusConfig,
            NatsDeliveryGate,
            NatsEventBus,
            StreamSpec,
        )

        topic = _topics.ORDER_INTENTS
        stream_name = "AATS_RT_CONSUMER_SUPERVISION"
        config = NatsBusConfig(
            servers=(self.nats_url,),
            streams=(
                StreamSpec(
                    name=stream_name,
                    topics=frozenset({topic}),
                    max_age_seconds=300.0,
                    max_bytes=16 * 1024 * 1024,
                    max_msgs=10_000,
                    max_msg_size=1024 * 1024,
                ),
            ),
            consumer_supervision_interval_seconds=0.05,
            consumer_supervision_failure_timeout_seconds=0.3,
        )
        gate = NatsDeliveryGate()
        bus = NatsEventBus(
            config=config,
            consumer_role="execution",
            delivery_gate=gate,
        )
        raw_client = await nats.connect(self.nats_url)
        raw_js = raw_client.jetstream()
        durable = config.durable_name_for("execution", topic)

        async def _noop_handler(_message: dict[str, Any]) -> None:
            return None

        try:
            await bus.start()
            await bus.subscribe(topic, _noop_handler)
            before = await raw_js.consumer_info(stream_name, durable)
            self.assertEqual(before.name, durable)
            await bus._supervise_critical_consumers_once()
            self.assertFalse(gate.aborted)

            await raw_js.delete_consumer(stream_name, durable)
            self.assertTrue(raw_client.is_connected)
            with self.assertRaisesRegex(
                RuntimeError,
                "nats_connection_terminal_failure",
            ):
                await asyncio.wait_for(
                    bus.wait_for_terminal_connection_failure(),
                    timeout=2.0,
                )
            self.assertTrue(raw_client.is_connected)
            self.assertTrue(gate.aborted)
        finally:
            await bus.close()
            await raw_client.drain()

    async def test_critical_durable_with_outstanding_acks_refuses_window_upgrade(
        self,
    ) -> None:
        """v1 durable 有未 ACK 消息时，v2 必须保留窗口与 cursor 并失败关闭。"""

        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        from aats.bus.nats_bus import (
            NatsBusConfig,
            NatsDeliveryGate,
            NatsEventBus,
            StreamSpec,
        )
        from aats.schemas.common import EventEnvelope

        topic = _topics.ORDER_INTENTS
        stream_name = "AATS_RT_DURABLE_PENDING_UPGRADE"
        stream = StreamSpec(
            name=stream_name,
            topics=frozenset({topic}),
            max_age_seconds=300.0,
            max_bytes=16 * 1024 * 1024,
            max_msgs=10_000,
            max_msg_size=1024 * 1024,
        )
        legacy_config = NatsBusConfig(
            servers=(self.nats_url,),
            streams=(stream,),
            ack_wait_seconds=30.0,
            max_ack_pending=256,
            flow_control=False,
        )
        durable = legacy_config.durable_name_for("execution", topic)
        subject = legacy_config.subject_for(topic)
        provisioner = NatsEventBus(
            config=legacy_config,
            consumer_role="execution",
        )
        upgraded = NatsEventBus(
            config=NatsBusConfig(
                servers=(self.nats_url,),
                streams=(stream,),
                ack_wait_seconds=30.0,
                max_ack_pending=256,
                flow_control=True,
            ),
            consumer_role="execution",
            delivery_gate=NatsDeliveryGate(),
        )
        raw_client = await nats.connect(self.nats_url)
        raw_js = raw_client.jetstream()
        raw_subscription: Any = None
        received: list[Any] = []
        three_delivered = asyncio.Event()

        async def _leave_unacked(message: Any) -> None:
            received.append(message)
            if len(received) >= 3:
                three_delivered.set()

        try:
            await provisioner.start()
            deliver_subject = raw_client.new_inbox()
            raw_subscription = await raw_client.subscribe(
                deliver_subject,
                cb=_leave_unacked,
            )
            await raw_js.add_consumer(
                stream_name,
                config=ConsumerConfig(
                    durable_name=durable,
                    deliver_subject=deliver_subject,
                    deliver_policy=DeliverPolicy.ALL,
                    ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=30.0,
                    max_ack_pending=256,
                    max_deliver=5,
                    filter_subject=subject,
                ),
            )

            for sequence in range(1, 4):
                envelope = EventEnvelope(
                    event_type="order_intent",
                    source_component="durable_pending_upgrade_test",
                    topic=topic,
                    key=f"pending-{sequence}",
                    payload={"sequence": sequence},
                )
                await raw_js.publish(
                    subject,
                    envelope.model_dump_json().encode("utf-8"),
                    headers={"Nats-Msg-Id": envelope.event_id},
                )

            await asyncio.wait_for(three_delivered.wait(), timeout=3.0)
            for _ in range(100):
                before = await raw_js.consumer_info(stream_name, durable)
                if before.num_ack_pending >= 3:
                    break
                await asyncio.sleep(0.02)
            self.assertGreaterEqual(before.num_ack_pending, 3)
            self.assertEqual(before.config.max_ack_pending, 256)
            before_identity = (before.name, before.created)
            before_ack_floor = (
                before.ack_floor.stream_seq,
                before.ack_floor.consumer_seq,
            )
            before_delivered = (
                before.delivered.stream_seq,
                before.delivered.consumer_seq,
            )

            await upgraded.start()

            async def _noop_handler(_message: dict[str, Any]) -> None:
                return None

            with self.assertRaisesRegex(
                RuntimeError,
                (
                    "^nats_critical_consumer_ack_window_migration_requires_drain:"
                    f"{durable}$"
                ),
            ):
                await upgraded.subscribe(topic, _noop_handler)

            after = await raw_js.consumer_info(stream_name, durable)
            self.assertEqual((after.name, after.created), before_identity)
            self.assertEqual(after.config.max_ack_pending, 256)
            self.assertEqual(after.num_ack_pending, before.num_ack_pending)
            self.assertEqual(
                (
                    after.ack_floor.stream_seq,
                    after.ack_floor.consumer_seq,
                ),
                before_ack_floor,
            )
            self.assertEqual(
                (
                    after.delivered.stream_seq,
                    after.delivered.consumer_seq,
                ),
                before_delivered,
            )
        finally:
            if upgraded._client is not None:
                await upgraded.close()
            if raw_subscription is not None:
                await raw_subscription.unsubscribe()
            await raw_client.drain()
            await provisioner.close()


    async def test_non_event_durables_rebuild_outstanding_window_by_policy(
        self,
    ) -> None:
        """LAST 保留最新、NEW 仅收未来；二者均不得继承旧宽窗 delivery。"""

        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        from aats.bus.nats_bus import (
            NatsBusConfig,
            NatsDeliveryGate,
            NatsEventBus,
            StreamSpec,
        )
        from aats.schemas.common import EventEnvelope

        cases = (
            (
                _topics.MARKET_SNAPSHOTS,
                "decision",
                DeliverPolicy.LAST,
                "AATS_RT_LAST_PENDING_REBUILD",
            ),
            (
                _topics.OPERATOR_COMMAND_REQUESTS,
                "execution",
                DeliverPolicy.NEW,
                "AATS_RT_NEW_PENDING_REBUILD",
            ),
        )
        for topic, role, policy, stream_name in cases:
            with self.subTest(policy=str(policy)):
                stream = StreamSpec(
                    name=stream_name,
                    topics=frozenset({topic}),
                    max_age_seconds=300.0,
                    max_bytes=16 * 1024 * 1024,
                    max_msgs=10_000,
                    max_msg_size=1024 * 1024,
                )
                config = NatsBusConfig(
                    servers=(self.nats_url,),
                    streams=(stream,),
                    ack_wait_seconds=30.0,
                    max_ack_pending=256,
                    flow_control=True,
                )
                durable = config.durable_name_for(role, topic)
                subject = config.subject_for(topic)
                provisioner = NatsEventBus(
                    config=config,
                    consumer_role=role,
                )
                gate = NatsDeliveryGate()
                upgraded = NatsEventBus(
                    config=config,
                    consumer_role=role,
                    delivery_gate=gate,
                )
                raw_client = await nats.connect(self.nats_url)
                raw_js = raw_client.jetstream()
                raw_subscription: Any = None
                received_raw: list[Any] = []
                three_delivered = asyncio.Event()

                async def _leave_unacked(message: Any) -> None:
                    received_raw.append(message)
                    if len(received_raw) >= 3:
                        three_delivered.set()

                try:
                    await provisioner.start()
                    deliver_subject = raw_client.new_inbox()
                    raw_subscription = await raw_client.subscribe(
                        deliver_subject,
                        cb=_leave_unacked,
                    )
                    await raw_js.add_consumer(
                        stream_name,
                        config=ConsumerConfig(
                            durable_name=durable,
                            deliver_subject=deliver_subject,
                            deliver_policy=policy,
                            ack_policy=AckPolicy.EXPLICIT,
                            ack_wait=30.0,
                            max_ack_pending=256,
                            max_deliver=5,
                            filter_subject=subject,
                        ),
                    )
                    for sequence in range(1, 4):
                        envelope = EventEnvelope(
                            event_type="non_event_cutover_probe",
                            source_component="non_event_pending_rebuild_test",
                            topic=topic,
                            key=f"pending-{sequence}",
                            payload={"sequence": sequence},
                        )
                        await raw_js.publish(
                            subject,
                            envelope.model_dump_json().encode("utf-8"),
                            headers={"Nats-Msg-Id": envelope.event_id},
                        )
                    await asyncio.wait_for(three_delivered.wait(), timeout=3.0)
                    before = await raw_js.consumer_info(stream_name, durable)
                    self.assertGreaterEqual(before.num_ack_pending, 3)
                    before_created = before.created

                    await upgraded.start()
                    received_upgraded: list[str] = []
                    delivered = asyncio.Event()

                    async def _upgraded_handler(message: dict[str, Any]) -> None:
                        received_upgraded.append(str(message["key"]))
                        delivered.set()

                    await upgraded.subscribe(topic, _upgraded_handler)
                    after = await raw_js.consumer_info(stream_name, durable)
                    self.assertEqual(after.name, durable)
                    self.assertNotEqual(after.created, before_created)
                    self.assertEqual(after.config.max_ack_pending, 1)
                    self.assertLessEqual(after.num_ack_pending, 1)
                    self.assertFalse(delivered.is_set())

                    await upgraded.activate_delivery()
                    if policy == DeliverPolicy.LAST:
                        await asyncio.wait_for(delivered.wait(), timeout=3.0)
                        self.assertEqual(received_upgraded, ["pending-3"])
                    else:
                        await asyncio.sleep(0.2)
                        self.assertEqual(received_upgraded, [])
                        future = EventEnvelope(
                            event_type="non_event_cutover_probe",
                            source_component="non_event_pending_rebuild_test",
                            topic=topic,
                            key="future",
                            payload={"sequence": 4},
                        )
                        await upgraded.publish_envelope(future, persist=False)
                        await asyncio.wait_for(delivered.wait(), timeout=3.0)
                        self.assertEqual(received_upgraded, ["future"])
                finally:
                    if upgraded._client is not None:
                        await upgraded.close()
                    if raw_subscription is not None:
                        await raw_subscription.unsubscribe()
                    await raw_client.drain()
                    if provisioner._client is not None:
                        await provisioner.close()


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[nats-integration] to run NATS integration tests",
)
class TestHybridEventBusRouting(unittest.IsolatedAsyncioTestCase):
    """HybridEventBus 必须按 routing 把 critical topic 送到 NATS，
    把 observer topic 留在内存。两条路径都通过真 NATS 验证。"""

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
        # 同一容器下两个 hybrid 测试都想 claim subject `aats.decisions`，
        # 不清理就会触发 'subjects overlap with an existing stream'。
        await _purge_all_streams(self.nats_url)

    async def test_hybrid_critical_topic_reaches_nats(self) -> None:
        """HybridEventBus.publish 在 critical topic 上必须真的把消息送到 NATS。
        通过单独建一个 NatsEventBus 订阅同一 topic 来验证。"""
        from aats.bus.memory_bus import InMemoryEventBus
        from aats.bus.nats_bus import (
            HybridBusRouting,
            HybridEventBus,
            NatsBusConfig,
            NatsEventBus,
        )
        from aats.schemas.common import EventEnvelope

        config = NatsBusConfig(
            servers=(self.nats_url,),
            stream_name="AATS_RT_HYBRID",
            ack_wait_seconds=5.0,
        )
        critical_bus = NatsEventBus(
            config=config,
            event_store=None,
            persistence_mode="permissive",
            consumer_role="hybrid_critical",
        )
        observer_bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        hybrid = HybridEventBus(
            critical_bus=critical_bus,
            observer_bus=observer_bus,
            routing=HybridBusRouting(),
        )

        # 单独建一个 verifier bus 订阅同一 stream
        verifier = NatsEventBus(
            config=config,
            event_store=None,
            persistence_mode="permissive",
            consumer_role="verifier",
        )
        try:
            await hybrid.start()
            # slice nats-capacity T2 例外（§7.5a.3）：verifier 只需要连接
            # NATS，不要再 ensure_streams()（stream 已经被 hybrid 创建了）。
            # 之前用 ``start(topics=None)`` 在旧语义下等价于"仅 connect"；
            # 新语义下 topics=None 会走 ensure_streams() 导致重复 upsert —— 不是
            # bug 但是浪费 round-trip。直接用 connect() 表达意图更清晰。
            await verifier.connect()

            received: list[dict[str, Any]] = []

            async def handler(message: dict) -> None:
                received.append(message)

            await verifier.subscribe(_topics.AI_DECISION_BRIEFS, handler)
            await asyncio.sleep(0.5)

            envelope = EventEnvelope(
                event_type="ai_decision_brief",
                source_component="test_hybrid_critical",
                topic=_topics.AI_DECISION_BRIEFS,
                key="symbol-ETH",
                payload={"decision_id": "hybrid-rt-1"},
            )
            await hybrid.publish_envelope(envelope, persist=False)

            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.1)

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["payload"]["payload"]["decision_id"], "hybrid-rt-1")
        finally:
            await verifier.close()
            await hybrid.close()

    async def test_hybrid_observer_topic_does_not_reach_nats(self) -> None:
        """HybridEventBus.publish 在 observer topic 上不应触达 NATS。
        通过 NATS subscribe 验证 — 1 秒内 NATS 上仍然没有任何 observer 消息。"""
        from aats.bus.memory_bus import InMemoryEventBus
        from aats.bus.nats_bus import (
            HybridBusRouting,
            HybridEventBus,
            NatsBusConfig,
            NatsEventBus,
        )
        from aats.schemas.common import EventEnvelope

        config = NatsBusConfig(
            servers=(self.nats_url,),
            stream_name="AATS_RT_OBSERVER",
            ack_wait_seconds=5.0,
        )
        critical_bus = NatsEventBus(
            config=config,
            event_store=None,
            persistence_mode="permissive",
            consumer_role="hybrid_critical",
        )
        observer_bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        hybrid = HybridEventBus(
            critical_bus=critical_bus,
            observer_bus=observer_bus,
            routing=HybridBusRouting(),
        )

        # verifier 必须用**独立 stream 名**：hybrid.start() 已经把 critical
        # subjects 注册到 AATS_RT_OBSERVER；同名 stream 加不同 subjects 会被
        # NATS 拒绝 'stream name already in use with a different configuration'。
        verifier = NatsEventBus(
            config=NatsBusConfig(
                servers=(self.nats_url,),
                stream_name="AATS_RT_OBSERVER_VERIFIER",
                ack_wait_seconds=5.0,
            ),
            event_store=None,
            persistence_mode="permissive",
            consumer_role="observer_verifier",
        )
        try:
            await hybrid.start()
            # 把 observer topic 也声明到独立 stream，这样 subscribe 不会报 stream-not-found
            await verifier.start(topics=[_topics.HEALTH_SNAPSHOTS])

            received: list[dict[str, Any]] = []

            async def handler(message: dict) -> None:
                received.append(message)

            await verifier.subscribe(_topics.HEALTH_SNAPSHOTS, handler)
            await asyncio.sleep(0.3)

            envelope = EventEnvelope(
                event_type="health_snapshot",
                source_component="test_hybrid_observer",
                topic=_topics.HEALTH_SNAPSHOTS,
                key="hint-1",
                payload={"hint": "noise"},
            )
            await hybrid.publish_envelope(envelope, persist=False)

            # 等 1 秒确认 NATS 上确实没收到任何消息（observer 应该走内存）
            await asyncio.sleep(1.0)
            self.assertEqual(received, [], "observer topic 不应该走 NATS")
        finally:
            await verifier.close()
            await hybrid.close()


# ─────────────────────────────────────────────────────────────────────
# Step 6: 真跨进程 round-trip
# ─────────────────────────────────────────────────────────────────────


def _publisher_subprocess_entry(nats_url: str, stream_name: str, payload_marker: str) -> None:
    """multiprocessing 子进程入口：构造自己的 NatsEventBus，publish 一条事件，退出。

    Windows spawn 模式下子进程会重新 import 本模块，所以这个函数必须放在
    模块顶层（不能是 inner function）才能被 pickle 序列化。
    """
    import asyncio as _asyncio  # 子进程独立 import，避免 fork 状态污染

    async def _main() -> None:
        from aats.bus.nats_bus import NatsBusConfig, NatsEventBus
        from aats.events import topics as _topics_inner
        from aats.schemas.common import EventEnvelope

        bus = NatsEventBus(
            config=NatsBusConfig(
                servers=(nats_url,),
                stream_name=stream_name,
                ack_wait_seconds=5.0,
            ),
            event_store=None,
            persistence_mode="permissive",
            consumer_role="subprocess_publisher",
        )
        try:
            await bus.start(topics=[_topics_inner.AI_DECISION_BRIEFS])
            envelope = EventEnvelope(
                event_type="ai_decision_brief",
                source_component="subprocess_publisher",
                topic=_topics_inner.AI_DECISION_BRIEFS,
                key="cross-proc",
                payload={"marker": payload_marker},
            )
            await bus.publish_envelope(envelope, persist=False)
            # 给 NATS 一点时间真的把消息持久化到 stream
            await _asyncio.sleep(0.3)
        finally:
            await bus.close()

    _asyncio.run(_main())


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[nats-integration] to run NATS integration tests",
)
class TestCrossProcessNatsRoundTrip(unittest.IsolatedAsyncioTestCase):
    """Step 6：真跨进程验证。

    spawn 一个独立的 Python 子进程，它构造自己的 NatsEventBus 并 publish；
    主进程构造另一个 NatsEventBus subscribe 并验证消息到达。

    这个测试不依赖 Stage 5 的 entry script / docker 容器，但语义上等价于
    "gateway 进程 publish, decision 进程 subscribe" — 是 4 进程拓扑工作的
    最小可移植证明。
    """

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

    async def test_subprocess_publishes_main_subscribes(self) -> None:
        """主进程订阅 → 子进程发布 → 主进程收到。"""
        from aats.bus.nats_bus import NatsBusConfig, NatsEventBus

        stream_name = "AATS_RT_CROSSPROC"
        marker = "cross-proc-msg-42"

        # 主进程先建好 stream + durable consumer，确保子进程 publish 时 stream 已存在
        subscriber = NatsEventBus(
            config=NatsBusConfig(
                servers=(self.nats_url,),
                stream_name=stream_name,
                ack_wait_seconds=5.0,
            ),
            event_store=None,
            persistence_mode="permissive",
            consumer_role="main_subscriber",
        )
        try:
            await subscriber.start(topics=[_topics.AI_DECISION_BRIEFS])

            received: list[dict[str, Any]] = []

            async def handler(message: dict) -> None:
                received.append(message)

            await subscriber.subscribe(_topics.AI_DECISION_BRIEFS, handler)
            await asyncio.sleep(0.5)

            # spawn 子进程做 publish
            ctx = multiprocessing.get_context("spawn")
            proc = ctx.Process(
                target=_publisher_subprocess_entry,
                args=(self.nats_url, stream_name, marker),
            )
            proc.start()
            # 等子进程退出（最多 30 秒）
            proc.join(timeout=30)
            self.assertEqual(proc.exitcode, 0, f"publisher subprocess failed with exitcode {proc.exitcode}")

            # 等主进程订阅器收到消息（最多 5 秒）
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.1)

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["payload"]["payload"]["marker"], marker)
        finally:
            await subscriber.close()


if __name__ == "__main__":
    unittest.main()
