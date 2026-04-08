"""Stage 6 Slice 6.3 集成测试：跨进程 portfolio_snapshot_cache 真容器 round-trip。

设计文档
========
docs/task/stage_6_slice_6_3_portfolio_snapshot_design.md §6 Step 6

覆盖范围（对应不变量 I2/I3/I8/I9）
==================================
1. **跨进程实时广播**（I2）
   - cache A publish → cache B 的本地 dict 在 ≤1s 内拿到最新 snapshot
   - 共享一台 Redis（testcontainers）+ 共享一条 InMemoryEventBus 模拟
     "两个进程，相同 NATS 总线 + 相同 Redis"。注意 cache.publish **不**
     主动广播 NATS（D5），只是写本地 dict + Redis；本测试为了走 round-
     trip 路径，模拟 outbox publisher 在 publish 之后通过 bus 把同样的
     envelope 广播出去（生产路径的 flush_pending 行为）

2. **重启 bootstrap 从 Redis hydrate**（I3）
   - cache A publish → cache B 关掉重建 → 新 B bootstrap 后从 Redis 拿到上
     一次 publish 的 snapshot

3. **scope 隔离**（I9）
   - 两个 scope (spot:cash / derivatives:isolated) 各自 publish 不互相污染

4. **乱序事件不退化**（I8）
   - 直接喂一个 snapshot_ts 比当前更老的远端事件给 _handle_remote_event，
     本地 cache 不应该回退

⚠️ **运行条件**：
- 需要 docker（Windows 上请确保 Docker Desktop 正在运行）
- 需要安装可选依赖：``pip install -e .[redis-integration]``
- 需要设置环境变量 ``AATS_RUN_REDIS_INTEGRATION=1`` 才会真正运行
- 默认情况下整个文件被 ``unittest.skipUnless`` 跳过，不会拖慢 CI / 单测

⚠️ **scope 注意**：本文件用 InMemoryEventBus 共享实例 + 真实 Redis 容器
组合模拟"两个进程"——这是 Slice 6.3 设计文档 §6 Step 6 钦定的轻量集成
路径，真正的 4 进程 docker compose 真跑由 §6 Step 7 + runbook §11 验证。
两者各管一段：本文件保证 PortfolioSnapshotCache 两实例之间的同步语义在
Redis 真容器上 round-trip 正确，4 进程真跑保证 NATS 真总线 + outbox
publisher 串起来的 wire 不掉链。
"""
from __future__ import annotations

import logging
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

# 软依赖检查：testcontainers + redis-py 可能没装（属于 redis-integration extra）
try:
    from testcontainers.core.container import DockerContainer  # type: ignore[import-not-found]
    from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover - 没装就跳过
    DockerContainer = None  # type: ignore[assignment,misc]
    wait_for_logs = None  # type: ignore[assignment]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import redis  # type: ignore[import-not-found]  # noqa: F401

    _REDIS_PY_AVAILABLE = True
except ImportError:  # pragma: no cover - 没装就跳过
    _REDIS_PY_AVAILABLE = False


_INTEGRATION_ENV_FLAG = "AATS_RUN_REDIS_INTEGRATION"
_SHOULD_RUN = (
    os.getenv(_INTEGRATION_ENV_FLAG) == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _REDIS_PY_AVAILABLE
)


def _start_redis_container() -> "DockerContainer":
    """启动一个 redis:7-alpine 容器（与 Slice 6.1/6.2 集成测试同款）。"""
    container = (
        DockerContainer("redis:7-alpine")
        .with_exposed_ports(6379)
        .with_command("redis-server --save '' --appendonly no")
    )
    container.start()
    wait_for_logs(container, "Ready to accept connections", timeout=30)
    return container


def _client_url(container: "DockerContainer") -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


def _make_snapshot(
    *,
    snapshot_ts: datetime,
    decision_id: str,
    product_type: str = "spot",
    margin_mode: str = "cash",
    total_equity: str = "10000",
):
    from aats.schemas.portfolio import PortfolioSnapshot

    return PortfolioSnapshot(
        decision_id=decision_id,
        snapshot_ts=snapshot_ts,
        balances={"USDT": Decimal(total_equity)},
        positions=[],
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_equity=Decimal(total_equity),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
    )


def _make_scope(
    *,
    product_type: str = "spot",
    margin_mode: str = "cash",
    default_symbol: str = "BTC-USDT",
):
    from aats.services.runtime_scope import RuntimeStateScope

    return RuntimeStateScope(
        product_type=product_type,  # type: ignore[arg-type]
        margin_mode=margin_mode,  # type: ignore[arg-type]
        allowed_symbols=(default_symbol,),
        default_symbol=default_symbol,
    )


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[redis-integration] to run Redis integration tests",
)
class TestPortfolioSnapshotCacheCrossProcess(unittest.IsolatedAsyncioTestCase):
    """跨进程 portfolio_snapshot 同步：2 个 PortfolioSnapshotCache 共享 Redis + bus。"""

    container: "DockerContainer"  # type: ignore[assignment]
    redis_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = _start_redis_container()
        cls.redis_url = _client_url(cls.container)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.container.stop()
        except Exception:
            pass

    async def asyncTearDown(self) -> None:
        # 测试间隔离：用 throwaway client FLUSHDB 清空 db0
        from redis.asyncio import Redis as AsyncRedis  # type: ignore[import-not-found]

        client = AsyncRedis.from_url(self.redis_url)
        try:
            await client.flushdb()
        finally:
            await client.aclose()

    async def _make_cache(
        self,
        *,
        process_role: str,
        bus,
        scope_fingerprint: str = "spot:cash",
    ):
        """构造一个 (cache, store) 组合，模拟一个进程的 portfolio cache 边车。"""
        from aats.services.portfolio_service.snapshot_cache import PortfolioSnapshotCache
        from aats.storage.hot_state_store import RedisHotStateConfig, RedisHotStateStore

        store = RedisHotStateStore(config=RedisHotStateConfig(url=self.redis_url))
        await store.connect()
        cache = PortfolioSnapshotCache(
            hot_state_store=store,
            bus=bus,
            process_role=process_role,
            logger=logging.getLogger(f"test.portfolio_snapshot_cache.{process_role}"),
        )
        await cache.bootstrap(scope_fingerprint=scope_fingerprint)
        return cache, store

    async def _broadcast_snapshot(self, *, bus, snapshot) -> None:
        """模拟 outbox publisher 在 commit 之后通过 bus.publish_envelope 把
        snapshot 广播出去（生产路径的 flush_pending 行为）。"""
        from aats.events import topics
        from aats.events.envelopes import build_envelope

        envelope = build_envelope(
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key=snapshot.decision_id or "test",
            payload_model=snapshot,
            source_component="aats.portfolio.outbox",
        )
        await bus.publish_envelope(envelope, persist=False)

    async def test_publish_on_a_propagates_to_b_within_one_second(self) -> None:
        """I2：cache A publish + bus broadcast → cache B 的本地 dict ≤1s 内拿到 snapshot。

        共享一条 InMemoryEventBus 实例模拟"同一根 NATS 总线"。两个 cache
        bootstrap 时各自 subscribe 到 ``portfolio.snapshots``，A 这边模拟
        outbox publisher 把 envelope 通过 bus 广播出去，B 的 _handle_remote_event
        会 apply 到本地 dict。
        """
        from aats.bus.memory_bus import InMemoryEventBus

        bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        cache_a, store_a = await self._make_cache(process_role="execution", bus=bus)
        cache_b, store_b = await self._make_cache(process_role="gateway", bus=bus)
        try:
            scope = _make_scope()
            self.assertIsNone(cache_a.get_sync(scope))
            self.assertIsNone(cache_b.get_sync(scope))

            snapshot = _make_snapshot(
                snapshot_ts=datetime.now(timezone.utc),
                decision_id="cross-proc-001",
            )

            t0 = time.monotonic()
            # 1) cache A 写本地 dict + best-effort Redis
            await cache_a.publish(snapshot)
            # 2) outbox publisher 模拟：通过 bus 广播 envelope
            await self._broadcast_snapshot(bus=bus, snapshot=snapshot)
            elapsed = time.monotonic() - t0

            # I1：本地立即可见
            cached_a = cache_a.get_sync(scope)
            self.assertIsNotNone(cached_a)
            assert cached_a is not None
            self.assertEqual(cached_a.decision_id, "cross-proc-001")
            # I2：B 通过 bus 接收 → ≤1s 内 apply
            cached_b = cache_b.get_sync(scope)
            self.assertIsNotNone(cached_b)
            assert cached_b is not None
            self.assertEqual(cached_b.decision_id, "cross-proc-001")
            self.assertLess(elapsed, 1.0)
        finally:
            await cache_a.stop()
            await cache_b.stop()
            await store_a.close()
            await store_b.close()

    async def test_restart_cache_b_recovers_snapshot_from_redis(self) -> None:
        """I3：cache A publish → 重启 cache B → 新 cache B bootstrap 后从 Redis hydrate。

        模拟"gateway 进程崩溃 + restart"：旧 B 关掉，新 B 用全新的 store +
        全新的 PortfolioSnapshotCache 跑 bootstrap，必须从 Redis 读到上一次
        publish 的 snapshot。
        """
        from aats.bus.memory_bus import InMemoryEventBus

        bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        cache_a, store_a = await self._make_cache(process_role="execution", bus=bus)
        cache_b, store_b = await self._make_cache(process_role="gateway", bus=bus)
        try:
            snapshot = _make_snapshot(
                snapshot_ts=datetime.now(timezone.utc),
                decision_id="restart-recovery-001",
            )
            await cache_a.publish(snapshot)
            await self._broadcast_snapshot(bus=bus, snapshot=snapshot)

            scope = _make_scope()
            self.assertIsNotNone(cache_b.get_sync(scope))

            # 模拟 gateway 进程崩溃 + 关闭
            await cache_b.stop()
            await store_b.close()

            # 新进程 cache B 启动：全新 PortfolioSnapshotCache，
            # bootstrap 后必须从 Redis hydrate 出 snapshot
            bus_b_new = InMemoryEventBus(event_store=None, persistence_mode="permissive")
            cache_b_new, store_b_new = await self._make_cache(
                process_role="gateway",
                bus=bus_b_new,
            )
            try:
                cached = cache_b_new.get_sync(scope)
                self.assertIsNotNone(cached)
                assert cached is not None
                self.assertEqual(cached.decision_id, "restart-recovery-001")
            finally:
                await cache_b_new.stop()
                await store_b_new.close()
        finally:
            await cache_a.stop()
            await store_a.close()

    async def test_two_scopes_do_not_pollute_each_other(self) -> None:
        """I9：cache A publish spot 与 derivatives 两个 scope 后，cache B 看到的
        两份 snapshot 互不污染。

        模拟"同一进程同时管两个 scope"或"两个 scope 各自的 outbox publisher
        把不同 fingerprint 的 snapshot 写到同一个 Redis"。
        """
        from aats.bus.memory_bus import InMemoryEventBus

        bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        # 注意：bootstrap_scope 决定 hydrate 的 key，但同一 cache 实例可以
        # 处理任意 scope_fingerprint 的 publish。这里两个 cache 都先 bootstrap
        # spot:cash，然后 publish 到两个 scope。
        cache_a, store_a = await self._make_cache(process_role="execution", bus=bus)
        cache_b, store_b = await self._make_cache(process_role="gateway", bus=bus)
        try:
            now = datetime.now(timezone.utc)
            spot_snap = _make_snapshot(
                snapshot_ts=now,
                decision_id="spot-decision",
                product_type="spot",
                margin_mode="cash",
            )
            deriv_snap = _make_snapshot(
                snapshot_ts=now,
                decision_id="deriv-decision",
                product_type="derivatives",
                margin_mode="isolated",
            )
            await cache_a.publish(spot_snap)
            await self._broadcast_snapshot(bus=bus, snapshot=spot_snap)
            await cache_a.publish(deriv_snap)
            await self._broadcast_snapshot(bus=bus, snapshot=deriv_snap)

            spot_scope = _make_scope(product_type="spot", margin_mode="cash")
            deriv_scope = _make_scope(product_type="derivatives", margin_mode="isolated")

            cached_spot_b = cache_b.get_sync(spot_scope)
            cached_deriv_b = cache_b.get_sync(deriv_scope)
            self.assertIsNotNone(cached_spot_b)
            self.assertIsNotNone(cached_deriv_b)
            assert cached_spot_b is not None and cached_deriv_b is not None
            self.assertEqual(cached_spot_b.decision_id, "spot-decision")
            self.assertEqual(cached_deriv_b.decision_id, "deriv-decision")
        finally:
            await cache_a.stop()
            await cache_b.stop()
            await store_a.close()
            await store_b.close()

    async def test_stale_remote_event_does_not_regress_local_cache(self) -> None:
        """I8：snapshot_ts 比已知本地 ts 还老的远端事件必须被丢弃。

        本测试不走 bus broadcast，而是直接喂构造好的 envelope dict 给
        ``_handle_remote_event``，模拟"另一进程因为时钟回退 / 重投 / 乱序"
        把更老的 snapshot 广播过来。断言本地 cache 不退化。
        """
        from aats.bus.memory_bus import InMemoryEventBus
        from aats.events import topics
        from aats.events.envelopes import build_envelope

        bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        cache, store = await self._make_cache(process_role="gateway", bus=bus)
        try:
            now = datetime.now(timezone.utc)
            newer = _make_snapshot(snapshot_ts=now, decision_id="newer_local")
            await cache.publish(newer)

            scope = _make_scope()
            cached = cache.get_sync(scope)
            assert cached is not None
            self.assertEqual(cached.decision_id, "newer_local")

            # 构造一个比 newer 还老的"远端事件"
            stale = _make_snapshot(
                snapshot_ts=now - timedelta(seconds=10),
                decision_id="older_remote",
            )
            stale_envelope = build_envelope(
                topic=topics.PORTFOLIO_SNAPSHOTS,
                key="older_remote",
                payload_model=stale,
                source_component="aats.portfolio.outbox",
            )
            stale_message: dict[str, Any] = {
                "topic": topics.PORTFOLIO_SNAPSHOTS,
                "key": "older_remote",
                "payload": stale_envelope.model_dump(mode="json"),
            }
            await cache._handle_remote_event(stale_message)

            # 本地 cache 不应该回退
            cached_after = cache.get_sync(scope)
            assert cached_after is not None
            self.assertEqual(cached_after.decision_id, "newer_local")
        finally:
            await cache.stop()
            await store.close()


if __name__ == "__main__":
    unittest.main()
