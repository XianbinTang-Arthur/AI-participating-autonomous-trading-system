"""Stage 6 Slice 6.4 集成测试：跨进程 kill_switch 同步真容器 round-trip。

设计文档
========
docs/task/stage_6_slice_6_4_kill_switch_unification_design.md §8.2

覆盖（对应 design doc 四条验收标准）：

1. **跨进程实时广播**（不变量 I2）
   - service A halt → service B 的本地 KillSwitch 在 ≤1s 内变 halted=True
   - 通过共享一台 Redis（testcontainers）+ 共享一条 InMemoryEventBus 模拟
     "两个进程，相同 NATS 总线 + 相同 Redis"

2. **重启 bootstrap 恢复**（不变量 I3）
   - service A halt → service B 关掉重建 → 新 service B bootstrap 后仍然
     halted=True（从 Redis 读出来）

3. **resume 收敛**（不变量 I2 对称）
   - service A halt 后 service B resume → 两个 KillSwitch 最终都 halted=False

4. **乱序事件不退化**（不变量 I6）
   - 直接喂一个 set_at_ts 比当前更老的远端事件，本地 cache 不应该回退

⚠️ **运行条件**：
- 需要 docker（Windows 上请确保 Docker Desktop 正在运行）
- 需要安装可选依赖：``pip install -e .[redis-integration]``
- 需要设置环境变量 ``AATS_RUN_REDIS_INTEGRATION=1`` 才会真正运行
- 默认情况下整个文件被 ``unittest.skipUnless`` 跳过，不会拖慢 CI / 单测

⚠️ **scope 注意**：本文件用 InMemoryEventBus 共享实例 + 真实 Redis 容器
组合模拟"两个进程"——这是 Slice 6.4 设计文档钦定的轻量集成路径，真正的
4 进程 docker compose 真跑由 runbook 验证。两者各管一段：本文件保证
合并后的 KillSwitch 两实例之间的同步语义在 Redis 真容器上 round-trip 正确，
4 进程真跑保证 NATS 真总线 + entry script 串起来的 wire 不掉链。

Slice 6.4 vs 6.2 差异
=====================
- 6.2：``KillSwitch()`` + ``KillSwitchSyncService(kill_switch=...).bootstrap()``
  双对象，5 个写入点用 if/else fallback
- 6.4：``KillSwitch()`` 单对象 + ``await ks.bootstrap(hot_state_store=..., bus=...,
  process_role=..., logger=...)``。写入点直接 ``ks.halt(reason)`` /
  ``await ks.halt_async(reason)``，sidecar 未配线时自动退化本地模式
"""
from __future__ import annotations

import logging
import os
import time
import unittest

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
    """启动一个 redis:7-alpine 容器（与 Slice 6.1 集成测试同款）。"""
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


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[redis-integration] to run Redis integration tests",
)
class TestKillSwitchCrossProcessSync(unittest.IsolatedAsyncioTestCase):
    """跨进程 kill_switch 同步：2 个 KillSwitch（已 bootstrap）共享 Redis + bus。"""

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

    async def _make_kill_switch(
        self,
        *,
        process_role: str,
        bus,
    ):
        """构造一个 (kill_switch, store) 组合，模拟一个进程的同步层。

        Slice 6.4：单对象 KillSwitch + bootstrap 一次性挂上 sidecar deps。
        """
        from aats.services.governance_engine.kill_switch import KillSwitch
        from aats.storage.hot_state_store import RedisHotStateConfig, RedisHotStateStore

        store = RedisHotStateStore(config=RedisHotStateConfig(url=self.redis_url))
        await store.connect()
        ks = KillSwitch()
        await ks.bootstrap(
            hot_state_store=store,
            bus=bus,
            process_role=process_role,
            logger=logging.getLogger(f"test.kill_switch.{process_role}"),
        )
        return ks, store

    async def test_halt_on_a_propagates_to_b_within_one_second(self) -> None:
        """I2：service A halt → service B 的本地 KillSwitch ≤1s 内被同步到 halted=True。

        共享一条 InMemoryEventBus 实例模拟"同一根 NATS 总线"。两个 KillSwitch
        bootstrap 时各自 subscribe 到 ``system.kill_switch_state``，A 的 halt
        会通过 bus 广播给 B 的 _handle_remote_event。
        """
        from aats.bus.memory_bus import InMemoryEventBus

        bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        ks_a, store_a = await self._make_kill_switch(process_role="gateway", bus=bus)
        ks_b, store_b = await self._make_kill_switch(process_role="execution", bus=bus)
        try:
            self.assertFalse(ks_a.halted)
            self.assertFalse(ks_b.halted)

            t0 = time.monotonic()
            await ks_a.halt_async(reason="cross_process_test_halt")
            elapsed = time.monotonic() - t0

            # I1：本地立即生效
            self.assertTrue(ks_a.halted)
            # I2：另一进程 ≤1s 内看到（InMemoryBus 同步交付，实际 << 1s）
            self.assertTrue(ks_b.halted)
            self.assertEqual(ks_b.status()["reason"], "cross_process_test_halt")
            self.assertLess(elapsed, 1.0)
        finally:
            await ks_a.stop()
            await ks_b.stop()
            await store_a.close()
            await store_b.close()

    async def test_restart_service_b_recovers_halt_state_from_redis(self) -> None:
        """I3：service A halt → 重启 service B → 新 service B bootstrap 后 halted=True。

        模拟"execution 进程崩溃 + restart"：旧 B 关掉，新 B 用全新的 store +
        全新的 KillSwitch 跑 bootstrap，必须从 Redis 读到上一次 halt 状态。
        """
        from aats.bus.memory_bus import InMemoryEventBus

        bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        ks_a, store_a = await self._make_kill_switch(process_role="gateway", bus=bus)
        ks_b, store_b = await self._make_kill_switch(process_role="execution", bus=bus)
        try:
            await ks_a.halt_async(reason="restart_recovery_test_halt")
            self.assertTrue(ks_b.halted)

            # 模拟 service B 进程崩溃 + 关闭
            await ks_b.stop()
            await store_b.close()

            # 新进程 service B 启动：全新 KillSwitch（默认 halted=False），
            # bootstrap 后必须从 Redis 读出 halted=True
            bus_b_new = InMemoryEventBus(event_store=None, persistence_mode="permissive")
            ks_b_new, store_b_new = await self._make_kill_switch(
                process_role="execution",
                bus=bus_b_new,
            )
            try:
                self.assertTrue(ks_b_new.halted)
                # bootstrap 时从 Redis hydrate 出来的 reason
                self.assertEqual(
                    ks_b_new.status()["reason"],
                    "restart_recovery_test_halt",
                )
            finally:
                await ks_b_new.stop()
                await store_b_new.close()
        finally:
            await ks_a.stop()
            await store_a.close()

    async def test_a_halts_then_b_resumes_converges_to_running(self) -> None:
        """I2 对称：A halt → B resume → 两个 KillSwitch 最终都 halted=False。"""
        from aats.bus.memory_bus import InMemoryEventBus

        bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        ks_a, store_a = await self._make_kill_switch(process_role="gateway", bus=bus)
        ks_b, store_b = await self._make_kill_switch(process_role="execution", bus=bus)
        try:
            await ks_a.halt_async(reason="converge_test_halt")
            self.assertTrue(ks_a.halted)
            self.assertTrue(ks_b.halted)

            # set_at_ts 必须严格单调：halt 时间和 resume 时间不能相同，否则
            # _handle_remote_event 用 <= 比较时会把 resume 当成"过期事件"丢弃。
            # InMemoryBus 是同步的，两次 time.time() 间隔 << 1ms，所以这里
            # 强制 sleep 一小段保证 set_at_ts 推进。生产路径下两次 NATS 广播
            # 之间天然有 ms 级延迟，不需要这个 sleep。
            import asyncio
            await asyncio.sleep(0.01)

            await ks_b.resume_async()
            self.assertFalse(ks_b.halted)
            self.assertFalse(ks_a.halted)
            self.assertEqual(ks_a.status()["reason"], None)
        finally:
            await ks_a.stop()
            await ks_b.stop()
            await store_a.close()
            await store_b.close()

    async def test_stale_remote_event_does_not_regress_local_cache(self) -> None:
        """I6：set_at_ts 比已知 last_applied_ts 还小的远端事件必须被丢弃。

        本测试不走 bus broadcast，而是直接喂构造好的 envelope dict 给
        ``_handle_remote_event``，因为正常 bus 路径下 set_at_ts 几乎总是
        单调递增（time.time 只在系统时钟回退时倒退）。我们在这里强制乱序，
        断言本地 cache 不退化。
        """
        from aats.bus.memory_bus import InMemoryEventBus
        from aats.events import topics
        from aats.schemas.common import EventEnvelope
        from aats.services.governance_engine.kill_switch import (
            KILL_SWITCH_EVENT_TYPE,
            KILL_SWITCH_SOURCE_COMPONENT,
        )

        bus = InMemoryEventBus(event_store=None, persistence_mode="permissive")
        ks, store = await self._make_kill_switch(process_role="gateway", bus=bus)
        try:
            # 先正常 halt 一次，把 _last_applied_ts 推进到一个较新的时间戳
            await ks.halt_async(reason="newer_state")
            self.assertTrue(ks.halted)
            newer_ts = ks.snapshot()["last_applied_ts"]
            self.assertGreater(newer_ts, 0.0)

            # 构造一个比 newer_ts 还老的"远端 resume"事件，模拟另一进程
            # 用更老的时钟广播过来
            stale_payload = {
                "halted": False,
                "reason": None,
                "set_at_ts": newer_ts - 10.0,  # 10 秒前
                "source_role": "execution",  # 不是 self，不会被 source_role loop 跳过
            }
            stale_envelope = EventEnvelope(
                event_type=KILL_SWITCH_EVENT_TYPE,
                source_component=KILL_SWITCH_SOURCE_COMPONENT,
                topic=topics.KILL_SWITCH_STATE,
                key="execution",
                payload=stale_payload,
            )
            stale_message = {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "execution",
                "payload": stale_envelope.model_dump(mode="json"),
            }
            await ks._handle_remote_event(stale_message)

            # 本地 cache 不应该回退：halt 状态保持
            self.assertTrue(ks.halted)
            self.assertEqual(ks.status()["reason"], "newer_state")
            # last_applied_ts 也不应该回退
            self.assertEqual(ks.snapshot()["last_applied_ts"], newer_ts)
        finally:
            await ks.stop()
            await store.close()


if __name__ == "__main__":
    unittest.main()
