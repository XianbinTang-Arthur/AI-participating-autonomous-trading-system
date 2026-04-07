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
- 需要 docker（Windows 上请确保 Docker Desktop 正在运行）
- 需要安装可选依赖：``pip install -e .[nats-integration]``
- 需要设置环境变量 ``AATS_RUN_NATS_INTEGRATION=1`` 才会真正运行
- 默认情况下整个文件被 ``unittest.skipUnless`` 跳过，不会拖慢 CI / 单测

⚠️ **scope 注意**：本文件验证的是 NATS JetStream 路径在 EventBus 层的正确性。
完整的 4 进程 docker-compose smoke test（gateway/market/decision/execution 容器
互相通信）依赖 Stage 5 的 entry script + 4 个新 docker service，那部分尚未做。
本文件用 multiprocessing 子进程作为 "可移植的 4 进程"代理来验证跨进程语义。
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
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
            await verifier.start(topics=None)  # stream 已经被 hybrid 创建了

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
