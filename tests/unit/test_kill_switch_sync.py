"""Stage 6 Slice 6.4 单元测试：合并的 KillSwitch 类（替换 6.2 的 KillSwitchSyncService）。

设计文档
========
docs/task/stage_6_slice_6_4_kill_switch_unification_design.md §7

覆盖范围
========
- 零参构造 + sync halt/resume 立即生效（无 sidecar 模式）
- bootstrap：从 Redis 读 halted=True 应用到本地、读到 None 维持本地默认
- halt_async / resume_async：本地 + Redis + NATS 三层 apply、Redis/NATS 失败 best-effort
- halt_async dedup（同 reason 重复）跳过广播
- ``_handle_remote_event``：新事件 apply、旧事件 reject、自己回环 skip
- sync ``halt`` / ``resume`` 从 worker thread 调（自动 dispatch 到主 loop）
- sync ``halt`` 从主 loop 线程调（fire-and-forget create_task）
- 未 bootstrap 时 sync halt 退化到 local-only

不在本测试范围：
- 真实 Redis 后端 → 集成测试
- 4 进程跨 NATS 跨容器 → 真跑验证（runbook §10）
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.schemas.common import EventEnvelope
from aats.services.governance_engine.kill_switch import (
    KILL_SWITCH_EVENT_TYPE,
    KILL_SWITCH_REDIS_KEY,
    KILL_SWITCH_SOURCE_COMPONENT,
    KillSwitch,
)
from aats.storage.hot_state_store import InMemoryHotStateStore


# ─────────────────────────────────────────────────────────────────────
# Test 双件
# ─────────────────────────────────────────────────────────────────────


class _ExplodingHotStateStore(InMemoryHotStateStore):
    """所有 set 都抛异常的 HotStateStore，用于测试 best-effort 写。"""

    def __init__(self, *, raise_on_set: bool = True, raise_on_get: bool = False) -> None:
        super().__init__()
        self._raise_on_set = raise_on_set
        self._raise_on_get = raise_on_get

    async def get(self, key: str) -> Any | None:
        if self._raise_on_get:
            raise RuntimeError("redis_get_boom")
        return await super().get(key)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        if self._raise_on_set:
            raise RuntimeError("redis_set_boom")
        await super().set(key, value, ttl_seconds=ttl_seconds)


class _ExplodingBus(InMemoryEventBus):
    """所有 publish 都抛异常的 EventBus，用于测试 best-effort NATS 写。"""

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        raise RuntimeError("nats_publish_boom")


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test.kill_switch")
    logger.setLevel(logging.DEBUG)
    return logger


async def _make_kill_switch(
    *,
    process_role: str = "decision",
    hot_state_store: InMemoryHotStateStore | None = None,
    bus: InMemoryEventBus | None = None,
    bootstrap: bool = True,
) -> tuple[KillSwitch, InMemoryHotStateStore, InMemoryEventBus]:
    """工厂方法：创建 KillSwitch + 默认 sidecar 依赖，可选 bootstrap。"""
    ks = KillSwitch()
    store = hot_state_store or InMemoryHotStateStore()
    bus_obj = bus if bus is not None else InMemoryEventBus()
    if bootstrap:
        await ks.bootstrap(
            hot_state_store=store,
            bus=bus_obj,
            process_role=process_role,
            logger=_make_logger(),
        )
    return ks, store, bus_obj


# ─────────────────────────────────────────────────────────────────────
# 零参 / 本地模式
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchLocalOnly(unittest.TestCase):
    def test_default_state_is_not_halted(self) -> None:
        ks = KillSwitch()
        self.assertFalse(ks.halted)
        self.assertEqual(ks.status(), {"halted": False, "reason": None})

    def test_halt_sets_local_state_immediately_without_bootstrap(self) -> None:
        ks = KillSwitch()
        ks.halt(reason="local_only_halt")
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "local_only_halt")

    def test_resume_clears_local_state(self) -> None:
        ks = KillSwitch()
        ks.halt(reason="halt_then_resume")
        ks.resume()
        self.assertFalse(ks.halted)
        self.assertIsNone(ks.status()["reason"])

    def test_status_is_atomic_tuple_assignment(self) -> None:
        # 不会爆出 partial state（halted=True, reason=None）
        ks = KillSwitch()
        ks.halt(reason="atomic_test")
        snap = ks.status()
        self.assertEqual(snap["halted"], True)
        self.assertEqual(snap["reason"], "atomic_test")


# ─────────────────────────────────────────────────────────────────────
# bootstrap
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_from_empty_redis_keeps_local_default(self) -> None:
        ks, _store, _bus = await _make_kill_switch()
        self.assertFalse(ks.halted)
        snap = ks.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["last_applied_ts"], 0.0)

    async def test_bootstrap_hydrates_halt_state_from_redis(self) -> None:
        store = InMemoryHotStateStore()
        await store.set(
            KILL_SWITCH_REDIS_KEY,
            {
                "halted": True,
                "reason": "previous_run_halt",
                "set_at_ts": 12345.6,
                "source_role": "execution",
            },
        )
        ks, _, _bus = await _make_kill_switch(hot_state_store=store)
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "previous_run_halt")
        self.assertEqual(ks.snapshot()["last_applied_ts"], 12345.6)

    async def test_bootstrap_redis_get_failure_does_not_raise(self) -> None:
        store = _ExplodingHotStateStore(raise_on_set=False, raise_on_get=True)
        ks, _, _bus = await _make_kill_switch(hot_state_store=store)
        self.assertFalse(ks.halted)
        # 仍然订阅成功
        self.assertTrue(ks.snapshot()["subscribed"])

    async def test_bootstrap_subscribes_to_kill_switch_topic(self) -> None:
        ks, _store, bus = await _make_kill_switch()
        # InMemoryEventBus 内部 _subs[topic] 应该有一个 handler
        self.assertEqual(len(bus._subs[topics.KILL_SWITCH_STATE]), 1)


# ─────────────────────────────────────────────────────────────────────
# halt_async / resume_async
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchAsyncHaltResume(unittest.IsolatedAsyncioTestCase):
    async def test_halt_async_updates_local_redis_and_publishes_nats(self) -> None:
        bus = InMemoryEventBus()
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        ks, store, _ = await _make_kill_switch(process_role="gateway", bus=bus)

        await ks.halt_async(reason="manual_test_halt")

        # 1) 本地立即生效
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "manual_test_halt")
        # 2) Redis 写入
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        self.assertIsNotNone(stored)
        assert isinstance(stored, dict)
        self.assertTrue(stored["halted"])
        self.assertEqual(stored["reason"], "manual_test_halt")
        self.assertEqual(stored["source_role"], "gateway")
        self.assertGreater(stored["set_at_ts"], 0.0)
        # 3) NATS 广播：subscribe handler 收到（除自身订阅外多了一个 capture handler）
        self.assertEqual(len(published), 1)
        envelope = EventEnvelope.model_validate(published[0]["payload"])
        self.assertEqual(envelope.topic, topics.KILL_SWITCH_STATE)
        self.assertEqual(envelope.event_type, KILL_SWITCH_EVENT_TYPE)
        self.assertEqual(envelope.source_component, KILL_SWITCH_SOURCE_COMPONENT)
        self.assertEqual(envelope.payload["halted"], True)
        self.assertEqual(envelope.payload["source_role"], "gateway")

    async def test_resume_async_updates_local_redis_and_publishes_nats(self) -> None:
        bus = InMemoryEventBus()
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        ks, store, _ = await _make_kill_switch(process_role="execution", bus=bus)

        await ks.halt_async(reason="will_be_resumed")
        await ks.resume_async()

        self.assertFalse(ks.halted)
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        assert isinstance(stored, dict)
        self.assertFalse(stored["halted"])
        self.assertIsNone(stored["reason"])
        # halt + resume 各发布一次
        self.assertEqual(len(published), 2)

    async def test_halt_async_redis_failure_still_broadcasts_nats(self) -> None:
        store = _ExplodingHotStateStore(raise_on_set=True)
        bus = InMemoryEventBus()
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        ks, _, _ = await _make_kill_switch(hot_state_store=store, bus=bus)
        # 不抛
        await ks.halt_async(reason="trial_guard_fired")
        # 本地 cache OK
        self.assertTrue(ks.halted)
        # NATS 仍然发出去了（best-effort 隔离）
        self.assertEqual(len(published), 1)

    async def test_halt_async_nats_failure_still_writes_redis_and_local(self) -> None:
        bus = _ExplodingBus()
        # bootstrap 时订阅会失败 → log warning，但 KillSwitch 仍然可用
        ks, store, _ = await _make_kill_switch(bus=bus)
        await ks.halt_async(reason="solo_halt")
        self.assertTrue(ks.halted)
        # Redis 仍然写入
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        assert isinstance(stored, dict)
        self.assertTrue(stored["halted"])

    async def test_halt_async_dedup_skips_repeat_publish(self) -> None:
        bus = InMemoryEventBus()
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        ks, _store, _ = await _make_kill_switch(bus=bus)

        await ks.halt_async(reason="same_reason")
        await ks.halt_async(reason="same_reason")
        await ks.halt_async(reason="same_reason")

        # 仅第一次广播，后两次因 dedup 跳过
        self.assertEqual(len(published), 1)


# ─────────────────────────────────────────────────────────────────────
# _handle_remote_event
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchRemoteEvent(unittest.IsolatedAsyncioTestCase):
    async def test_remote_halt_event_applies_to_local_cache(self) -> None:
        ks, _store, _bus = await _make_kill_switch(process_role="decision")

        envelope = EventEnvelope(
            event_type=KILL_SWITCH_EVENT_TYPE,
            source_component=KILL_SWITCH_SOURCE_COMPONENT,
            topic=topics.KILL_SWITCH_STATE,
            key="gateway",
            payload={
                "halted": True,
                "reason": "remote_operator_halt",
                "set_at_ts": 1000.0,
                "source_role": "gateway",
            },
        )
        await ks._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "gateway",
                "payload": envelope.model_dump(mode="json"),
            }
        )
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "remote_operator_halt")
        self.assertEqual(ks.snapshot()["last_applied_ts"], 1000.0)

    async def test_remote_event_with_stale_set_at_ts_does_not_revert(self) -> None:
        ks, _store, _bus = await _make_kill_switch(process_role="decision")

        # 先 apply 一个 ts=1000 的 halt
        new_envelope = EventEnvelope(
            event_type=KILL_SWITCH_EVENT_TYPE,
            source_component=KILL_SWITCH_SOURCE_COMPONENT,
            topic=topics.KILL_SWITCH_STATE,
            key="gateway",
            payload={
                "halted": True,
                "reason": "newer_halt",
                "set_at_ts": 1000.0,
                "source_role": "gateway",
            },
        )
        await ks._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "gateway",
                "payload": new_envelope.model_dump(mode="json"),
            }
        )
        self.assertTrue(ks.halted)

        # 然后到达一个 ts=500 的 resume（更旧）→ 必须被忽略
        stale_envelope = EventEnvelope(
            event_type=KILL_SWITCH_EVENT_TYPE,
            source_component=KILL_SWITCH_SOURCE_COMPONENT,
            topic=topics.KILL_SWITCH_STATE,
            key="execution",
            payload={
                "halted": False,
                "reason": None,
                "set_at_ts": 500.0,
                "source_role": "execution",
            },
        )
        await ks._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "execution",
                "payload": stale_envelope.model_dump(mode="json"),
            }
        )
        # 本地仍然 halted
        self.assertTrue(ks.halted)
        self.assertEqual(ks.snapshot()["last_applied_ts"], 1000.0)

    async def test_remote_event_from_self_is_ignored(self) -> None:
        ks, _store, _bus = await _make_kill_switch(process_role="gateway")

        # 模拟自己回环：source_role 是自己
        envelope = EventEnvelope(
            event_type=KILL_SWITCH_EVENT_TYPE,
            source_component=KILL_SWITCH_SOURCE_COMPONENT,
            topic=topics.KILL_SWITCH_STATE,
            key="gateway",
            payload={
                "halted": True,
                "reason": "loop_back",
                "set_at_ts": 9999.0,
                "source_role": "gateway",  # 自己
            },
        )
        await ks._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "gateway",
                "payload": envelope.model_dump(mode="json"),
            }
        )
        # 因为 source_role == self._process_role，跳过 → 本地保持默认
        self.assertFalse(ks.halted)

    async def test_remote_resume_event_clears_local_halt(self) -> None:
        ks, _store, _bus = await _make_kill_switch(process_role="execution")
        # 先用远端 halt envelope 把本地推到 halted（避免 ks.halt() 把 _last_applied_ts
        # 推到 time.time()，导致随后的小时间戳 resume envelope 被当成 stale 丢弃）
        halt_envelope = EventEnvelope(
            event_type=KILL_SWITCH_EVENT_TYPE,
            source_component=KILL_SWITCH_SOURCE_COMPONENT,
            topic=topics.KILL_SWITCH_STATE,
            key="gateway",
            payload={
                "halted": True,
                "reason": "remote_seed_halt",
                "set_at_ts": 5000.0,
                "source_role": "gateway",
            },
        )
        await ks._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "gateway",
                "payload": halt_envelope.model_dump(mode="json"),
            }
        )
        self.assertTrue(ks.halted)

        envelope = EventEnvelope(
            event_type=KILL_SWITCH_EVENT_TYPE,
            source_component=KILL_SWITCH_SOURCE_COMPONENT,
            topic=topics.KILL_SWITCH_STATE,
            key="gateway",
            payload={
                "halted": False,
                "reason": None,
                "set_at_ts": 7777.0,
                "source_role": "gateway",
            },
        )
        await ks._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "gateway",
                "payload": envelope.model_dump(mode="json"),
            }
        )
        self.assertFalse(ks.halted)


# ─────────────────────────────────────────────────────────────────────
# sync halt / resume：从 worker thread 与主 loop 线程
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchSyncDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_sync_halt_from_worker_thread_updates_local_and_redis(self) -> None:
        ks, store, _bus = await _make_kill_switch(process_role="execution")
        # 用 run_in_executor 模拟 worker thread
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, ks.halt, "trial_guard_breach")
        # 给主 loop 一点时间消化（halt 内 future.result(timeout) 已经等过了，但是
        # redis/nats publish 任务还在主 loop 调度上跑）
        await asyncio.sleep(0)
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "trial_guard_breach")
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        assert isinstance(stored, dict)
        self.assertTrue(stored["halted"])
        self.assertEqual(stored["reason"], "trial_guard_breach")

    async def test_sync_resume_from_worker_thread_updates_local_and_redis(self) -> None:
        ks, store, _bus = await _make_kill_switch(process_role="execution")
        await ks.halt_async(reason="will_be_resumed")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, ks.resume)
        await asyncio.sleep(0)
        self.assertFalse(ks.halted)
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        assert isinstance(stored, dict)
        self.assertFalse(stored["halted"])

    async def test_sync_halt_from_main_loop_thread_fires_publish_task(self) -> None:
        bus = InMemoryEventBus()
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        ks, _store, _ = await _make_kill_switch(process_role="gateway", bus=bus)
        # 在主 loop 线程内直接 sync halt
        ks.halt(reason="from_main_loop")
        # 本地立即生效
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "from_main_loop")
        # 让 fire-and-forget task 跑一圈
        await asyncio.sleep(0)
        # publish 任务里又有 await，所以再让 loop 转一圈
        await asyncio.sleep(0)
        # NATS 广播应该已发出
        self.assertEqual(len(published), 1)

    def test_sync_halt_without_bootstrap_falls_back_to_local_only(self) -> None:
        # 不调 bootstrap → _loop is None → fall back
        ks = KillSwitch()
        ks.halt(reason="emergency_halt_pre_bootstrap")
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "emergency_halt_pre_bootstrap")

    def test_sync_resume_without_bootstrap_falls_back_to_local_only(self) -> None:
        ks = KillSwitch()
        ks.halt(reason="seed")
        ks.resume()
        self.assertFalse(ks.halted)


if __name__ == "__main__":
    unittest.main()
