"""Finding 3: GuardSignalHotStateCache 单元测试。

测试跨进程 guard signal 缓存的：
  - bootstrap 从 Redis 恢复
  - publish 写 local + Redis + NATS
  - snapshot 同步读取 + stale fail-closed
  - NATS 回调更新 + 幂等性（旧消息丢弃）
  - __call__ callable 接口（recovery_status_provider）
  - E2E: execution publish → NATS → decision read
"""
from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.services.governance_engine.guard_signal_cache import (
    GuardSignalHotStateCache,
)
from aats.storage.hot_state_store import InMemoryHotStateStore

import logging


def _make_logger() -> logging.Logger:
    return logging.getLogger("test_guard_signal_cache")


class TestBootstrap(unittest.IsolatedAsyncioTestCase):
    """bootstrap() 生命周期测试。"""

    async def test_bootstrap_without_store(self) -> None:
        """无 hot_state_store 也不报错（退化为纯内存缓存）。"""
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        self.assertTrue(cache.bootstrapped)
        self.assertEqual(cache.snapshot(), {})

    async def test_bootstrap_restores_from_redis(self) -> None:
        """bootstrap 从 Redis 恢复之前发布的快照。"""
        store = InMemoryHotStateStore()
        cache_writer = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await cache_writer.bootstrap(hot_state_store=store, process_role="execution")
        await cache_writer.publish({"status": "clean", "breaches": 0})

        # 新实例 bootstrap 应该恢复
        cache_reader = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await cache_reader.bootstrap(hot_state_store=store, process_role="decision")
        snapshot = cache_reader.snapshot()
        self.assertEqual(snapshot["status"], "clean")
        self.assertEqual(snapshot["breaches"], 0)

    async def test_bootstrap_with_empty_redis(self) -> None:
        """Redis 无数据时 bootstrap 成功，snapshot 返回空 dict。"""
        store = InMemoryHotStateStore()
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap(hot_state_store=store, process_role="decision")
        self.assertTrue(cache.bootstrapped)
        self.assertEqual(cache.snapshot(), {})


class TestPublish(unittest.IsolatedAsyncioTestCase):
    """publish() 写路径测试。"""

    async def test_publish_updates_local_and_redis(self) -> None:
        """publish 同时写 local dict 和 Redis。"""
        store = InMemoryHotStateStore()
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await cache.bootstrap(hot_state_store=store, process_role="execution")

        await cache.publish({
            "status": "active",
            "only_reduce_required": False,
            "auto_halt_required": False,
            "risk_snapshot_stage": "ok",
        })

        # 本地读
        snapshot = cache.snapshot()
        self.assertEqual(snapshot["status"], "active")
        self.assertFalse(snapshot["only_reduce_required"])

        # Redis 读
        raw = await store.get(cache.redis_key)
        self.assertIsInstance(raw, dict)
        self.assertEqual(raw["status"], "active")
        self.assertIn("_cached_at", raw)

    async def test_publish_with_nats_broadcast(self) -> None:
        """publish 也 best-effort 广播到 NATS。"""
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")
        captured: list[dict] = []

        async def _collector(message: dict[str, Any]) -> None:
            captured.append(message)

        from aats.events import topics
        await bus.subscribe(topics.GUARD_SIGNAL_UPDATES, _collector)

        cache = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="execution",
        )
        await cache.publish({"status": "breached", "breaches": 3})

        self.assertEqual(len(captured), 1)

    async def test_publish_without_store_still_works_locally(self) -> None:
        """无 Redis 时 publish 仍然更新本地 dict。"""
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        await cache.publish({"safe_to_trade": True, "review_required": False})

        snapshot = cache.snapshot()
        self.assertTrue(snapshot["safe_to_trade"])


class TestSnapshot(unittest.IsolatedAsyncioTestCase):
    """snapshot() 读路径测试。"""

    async def test_snapshot_returns_copy_without_metadata(self) -> None:
        """snapshot 剥离 _ 开头的内部字段。"""
        cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        await cache.publish({"status": "active", "margin_usage": 0.35})

        snapshot = cache.snapshot()
        self.assertEqual(snapshot["status"], "active")
        self.assertNotIn("_cached_at", snapshot)
        self.assertNotIn("_signal_name", snapshot)
        self.assertNotIn("_writer_role", snapshot)

    async def test_snapshot_fail_closed_on_stale(self) -> None:
        """快照过期时返回空 dict（fail-closed）。"""
        cache = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
            stale_threshold_seconds=0.05,  # 50ms
        )
        await cache.bootstrap()
        await cache.publish({"status": "clean"})

        # 立刻读 → 有数据
        self.assertEqual(cache.snapshot()["status"], "clean")

        # 等超过 stale threshold
        await asyncio.sleep(0.1)
        self.assertEqual(cache.snapshot(), {})

    async def test_snapshot_empty_before_any_publish(self) -> None:
        """未 publish 过时 snapshot 返回空 dict。"""
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        self.assertEqual(cache.snapshot(), {})

    async def test_callable_interface(self) -> None:
        """__call__ 与 snapshot 返回相同结果。"""
        cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await cache.bootstrap()
        await cache.publish({"safe_to_trade": True})

        self.assertEqual(cache(), cache.snapshot())
        self.assertTrue(cache()["safe_to_trade"])


class TestNATSSubscription(unittest.IsolatedAsyncioTestCase):
    """NATS 订阅回调测试。"""

    async def test_nats_update_refreshes_local_snapshot(self) -> None:
        """decision 侧通过 NATS 收到 execution 侧的更新。"""
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        # Execution 侧 publisher
        pub_cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await pub_cache.bootstrap(
            hot_state_store=store, bus=bus, process_role="execution",
        )

        # Decision 侧 subscriber
        sub_cache = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await sub_cache.bootstrap(
            hot_state_store=store, bus=bus,
            process_role="decision", subscribe=True,
        )

        # Execution 发布
        await pub_cache.publish({
            "status": "active",
            "only_reduce_required": True,
            "only_reduce_reasons": ["margin_critical"],
        })

        # Decision 侧应该通过 NATS 收到更新
        snapshot = sub_cache.snapshot()
        self.assertEqual(snapshot["status"], "active")
        self.assertTrue(snapshot["only_reduce_required"])

    async def test_nats_ignores_different_signal_name(self) -> None:
        """不同 signal_name 的消息被忽略。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        # trial publisher
        trial_pub = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
        )
        await trial_pub.bootstrap(bus=bus, process_role="execution")

        # derivatives_live subscriber
        live_sub = GuardSignalHotStateCache(
            signal_name="derivatives_live",
            logger=_make_logger(),
        )
        await live_sub.bootstrap(bus=bus, process_role="decision", subscribe=True)

        await trial_pub.publish({"status": "breached"})

        # derivatives_live subscriber 不应收到 trial 的更新
        self.assertEqual(live_sub.snapshot(), {})

    async def test_nats_idempotent_stale_message_dropped(self) -> None:
        """旧时间戳的 NATS 消息被丢弃（幂等）。"""
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        pub_cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await pub_cache.bootstrap(bus=bus, process_role="execution")

        sub_cache = GuardSignalHotStateCache(
            signal_name="recovery",
            logger=_make_logger(),
        )
        await sub_cache.bootstrap(bus=bus, process_role="decision", subscribe=True)

        # 发布两条，第二条比第一条新
        await pub_cache.publish({"safe_to_trade": False, "version": 1})
        await asyncio.sleep(0.01)
        await pub_cache.publish({"safe_to_trade": True, "version": 2})

        # subscriber 应该持有最新版本
        self.assertTrue(sub_cache.snapshot()["safe_to_trade"])
        self.assertEqual(sub_cache.snapshot()["version"], 2)


class TestE2EPublishSubscribe(unittest.IsolatedAsyncioTestCase):
    """端到端测试：3 个信号的完整 publish → subscribe 流程。"""

    async def test_three_signals_e2e(self) -> None:
        """模拟 4-proc: execution 发布 3 个信号，decision 全部接收。"""
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus(event_store=None, persistence_mode="lenient")

        # Execution 侧：3 个 publisher
        exec_caches: dict[str, GuardSignalHotStateCache] = {}
        for name in ("derivatives_live", "trial", "recovery"):
            c = GuardSignalHotStateCache(
                signal_name=name,
                logger=_make_logger(),
            )
            await c.bootstrap(
                hot_state_store=store, bus=bus, process_role="execution",
            )
            exec_caches[name] = c

        # Decision 侧：3 个 subscriber
        dec_caches: dict[str, GuardSignalHotStateCache] = {}
        for name in ("derivatives_live", "trial", "recovery"):
            c = GuardSignalHotStateCache(
                signal_name=name,
                logger=_make_logger(),
            )
            await c.bootstrap(
                hot_state_store=store, bus=bus,
                process_role="decision", subscribe=True,
            )
            dec_caches[name] = c

        # Execution 发布
        await exec_caches["derivatives_live"].publish({
            "status": "active",
            "only_reduce_required": False,
            "auto_halt_required": False,
            "risk_snapshot_stage": "ok",
        })
        await exec_caches["trial"].publish({
            "status": "clean",
            "breaches": 0,
        })
        await exec_caches["recovery"].publish({
            "safe_to_trade": True,
            "review_required": False,
            "only_reduce_required": False,
        })

        # Decision 侧验证
        live = dec_caches["derivatives_live"].snapshot()
        self.assertEqual(live["status"], "active")
        self.assertFalse(live["auto_halt_required"])

        trial = dec_caches["trial"].snapshot()
        self.assertEqual(trial["status"], "clean")

        recovery = dec_caches["recovery"]()  # callable 接口
        self.assertTrue(recovery["safe_to_trade"])


class TestDiagnostic(unittest.IsolatedAsyncioTestCase):
    """diagnostic() 运维信息测试。"""

    async def test_diagnostic_output(self) -> None:
        cache = GuardSignalHotStateCache(
            signal_name="trial",
            logger=_make_logger(),
            stale_threshold_seconds=60.0,
        )
        await cache.bootstrap(process_role="decision")
        diag = cache.diagnostic()
        self.assertEqual(diag["signal_name"], "trial")
        self.assertTrue(diag["bootstrapped"])
        self.assertFalse(diag["has_data"])
        self.assertEqual(diag["stale_threshold_seconds"], 60.0)
        self.assertEqual(diag["process_role"], "decision")
