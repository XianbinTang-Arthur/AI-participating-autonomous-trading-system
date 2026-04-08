"""Stage 6 Slice 6.2 单元测试：KillSwitchSyncService。

设计文档
========
docs/task/stage_6_slice_6_2_kill_switch_design.md §8.1

覆盖范围
========
- bootstrap：从 Redis 读 halted=True 应用到本地、读到 None 维持本地默认
- halt / resume：本地 + Redis + NATS 三层 apply、Redis/NATS 失败 best-effort
- halt 重复（reason 一致）跳过广播去重
- ``_handle_remote_event``：新事件 apply、旧事件 reject、自己回环 skip
- ``halt_threadsafe`` / ``resume_threadsafe``：从 worker thread 调能更新本地，
  loop 不可用时退化到 local-only

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
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.kill_switch_sync import (
    KILL_SWITCH_EVENT_TYPE,
    KILL_SWITCH_REDIS_KEY,
    KILL_SWITCH_SOURCE_COMPONENT,
    KillSwitchSyncService,
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
    logger = logging.getLogger("test.kill_switch_sync")
    logger.setLevel(logging.DEBUG)
    return logger


def _make_service(
    *,
    process_role: str = "decision",
    hot_state_store: InMemoryHotStateStore | None = None,
    bus: InMemoryEventBus | None = None,
    kill_switch: KillSwitch | None = None,
) -> tuple[KillSwitchSyncService, KillSwitch, InMemoryHotStateStore, InMemoryEventBus]:
    ks = kill_switch or KillSwitch()
    store = hot_state_store or InMemoryHotStateStore()
    bus_obj = bus if bus is not None else InMemoryEventBus()
    service = KillSwitchSyncService(
        kill_switch=ks,
        hot_state_store=store,
        bus=bus_obj,
        process_role=process_role,
        logger=_make_logger(),
    )
    return service, ks, store, bus_obj


# ─────────────────────────────────────────────────────────────────────
# bootstrap
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchSyncBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_from_empty_redis_keeps_local_default(self) -> None:
        service, ks, _store, _bus = _make_service()
        await service.bootstrap()
        self.assertFalse(ks.halted)
        snap = service.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["last_applied_ts"], 0.0)

    async def test_bootstrap_hydrates_halt_state_from_redis(self) -> None:
        service, ks, store, _bus = _make_service()
        await store.set(
            KILL_SWITCH_REDIS_KEY,
            {
                "halted": True,
                "reason": "previous_run_halt",
                "set_at_ts": 12345.6,
                "source_role": "execution",
            },
        )
        await service.bootstrap()
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "previous_run_halt")
        self.assertEqual(service.snapshot()["last_applied_ts"], 12345.6)

    async def test_bootstrap_redis_get_failure_does_not_raise(self) -> None:
        store = _ExplodingHotStateStore(raise_on_set=False, raise_on_get=True)
        service, ks, _, _bus = _make_service(hot_state_store=store)
        # bootstrap 必须不抛
        await service.bootstrap()
        self.assertFalse(ks.halted)
        # 仍然订阅成功
        self.assertTrue(service.snapshot()["subscribed"])

    async def test_bootstrap_subscribes_to_kill_switch_topic(self) -> None:
        service, _ks, _store, bus = _make_service()
        await service.bootstrap()
        # InMemoryEventBus 内部 _subs[topic] 应该有一个 handler
        self.assertEqual(len(bus._subs[topics.KILL_SWITCH_STATE]), 1)


# ─────────────────────────────────────────────────────────────────────
# halt / resume async 路径
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchSyncHaltResume(unittest.IsolatedAsyncioTestCase):
    async def test_halt_updates_local_redis_and_publishes_nats(self) -> None:
        service, ks, store, bus = _make_service(process_role="gateway")
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        await service.bootstrap()

        await service.halt(reason="manual_test_halt")

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
        # 3) NATS 广播：subscribe handler 收到
        self.assertEqual(len(published), 1)
        envelope = EventEnvelope.model_validate(published[0]["payload"])
        self.assertEqual(envelope.topic, topics.KILL_SWITCH_STATE)
        self.assertEqual(envelope.event_type, KILL_SWITCH_EVENT_TYPE)
        self.assertEqual(envelope.source_component, KILL_SWITCH_SOURCE_COMPONENT)
        self.assertEqual(envelope.payload["halted"], True)
        self.assertEqual(envelope.payload["source_role"], "gateway")

    async def test_resume_updates_local_redis_and_publishes_nats(self) -> None:
        service, ks, store, bus = _make_service(process_role="execution")
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        await service.bootstrap()
        await service.halt(reason="will_be_resumed")

        await service.resume()

        self.assertFalse(ks.halted)
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        assert isinstance(stored, dict)
        self.assertFalse(stored["halted"])
        self.assertIsNone(stored["reason"])
        # halt + resume 各发布一次
        self.assertEqual(len(published), 2)

    async def test_halt_redis_failure_still_broadcasts_nats(self) -> None:
        store = _ExplodingHotStateStore(raise_on_set=True)
        service, ks, _, bus = _make_service(hot_state_store=store)
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        await service.bootstrap()
        # 不抛
        await service.halt(reason="trial_guard_fired")
        # 本地 cache OK
        self.assertTrue(ks.halted)
        # NATS 仍然发出去了（best-effort 隔离）
        self.assertEqual(len(published), 1)

    async def test_halt_nats_failure_still_writes_redis_and_local(self) -> None:
        bus = _ExplodingBus()
        service, ks, store, _ = _make_service(bus=bus)
        # bootstrap 时订阅会失败 → log warning，但 service 仍然可用
        await service.bootstrap()
        await service.halt(reason="solo_halt")
        self.assertTrue(ks.halted)
        # Redis 仍然写入
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        assert isinstance(stored, dict)
        self.assertTrue(stored["halted"])

    async def test_halt_dedup_skips_repeat_publish(self) -> None:
        service, _ks, _store, bus = _make_service()
        published: list[dict] = []

        async def capture(message: dict) -> None:
            published.append(message)

        await bus.subscribe(topics.KILL_SWITCH_STATE, capture)
        await service.bootstrap()

        await service.halt(reason="same_reason")
        await service.halt(reason="same_reason")
        await service.halt(reason="same_reason")

        # 仅第一次广播，后两次因 dedup 跳过
        self.assertEqual(len(published), 1)


# ─────────────────────────────────────────────────────────────────────
# _handle_remote_event
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchSyncRemoteEvent(unittest.IsolatedAsyncioTestCase):
    async def test_remote_halt_event_applies_to_local_cache(self) -> None:
        service, ks, _store, _bus = _make_service(process_role="decision")
        await service.bootstrap()

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
        await service._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "gateway",
                "payload": envelope.model_dump(mode="json"),
            }
        )
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "remote_operator_halt")
        self.assertEqual(service.snapshot()["last_applied_ts"], 1000.0)

    async def test_remote_event_with_stale_set_at_ts_does_not_revert(self) -> None:
        service, ks, _store, _bus = _make_service(process_role="decision")
        await service.bootstrap()

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
        await service._handle_remote_event(
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
        await service._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "execution",
                "payload": stale_envelope.model_dump(mode="json"),
            }
        )
        # 本地仍然 halted
        self.assertTrue(ks.halted)
        self.assertEqual(service.snapshot()["last_applied_ts"], 1000.0)

    async def test_remote_event_from_self_is_ignored(self) -> None:
        service, ks, _store, _bus = _make_service(process_role="gateway")
        await service.bootstrap()

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
        await service._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "gateway",
                "payload": envelope.model_dump(mode="json"),
            }
        )
        # 因为 source_role == self._process_role，跳过 → 本地保持默认
        self.assertFalse(ks.halted)

    async def test_remote_resume_event_clears_local_halt(self) -> None:
        service, ks, _store, _bus = _make_service(process_role="execution")
        await service.bootstrap()
        # 先本地 halt
        ks.halt(reason="local_state")

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
        await service._handle_remote_event(
            {
                "topic": topics.KILL_SWITCH_STATE,
                "key": "gateway",
                "payload": envelope.model_dump(mode="json"),
            }
        )
        self.assertFalse(ks.halted)


# ─────────────────────────────────────────────────────────────────────
# halt_threadsafe / resume_threadsafe
# ─────────────────────────────────────────────────────────────────────


class TestKillSwitchSyncThreadsafe(unittest.IsolatedAsyncioTestCase):
    async def test_halt_threadsafe_updates_local_and_redis_from_worker_thread(self) -> None:
        service, ks, store, _bus = _make_service(process_role="execution")
        await service.bootstrap()
        # 用 run_in_executor 模拟 worker thread
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, service.halt_threadsafe, "trial_guard_breach"
        )
        # 给主 loop 一点时间消化 run_coroutine_threadsafe 投递（halt_threadsafe 内
        # future.result(timeout) 已经等过了，但 redis/nats 异步任务仍然在主 loop 调度
        # 上完成；我们这里用 await sleep(0) 让 loop 走一圈）
        await asyncio.sleep(0)
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "trial_guard_breach")
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        assert isinstance(stored, dict)
        self.assertTrue(stored["halted"])
        self.assertEqual(stored["reason"], "trial_guard_breach")

    async def test_resume_threadsafe_updates_local_and_redis(self) -> None:
        service, ks, store, _bus = _make_service(process_role="execution")
        await service.bootstrap()
        await service.halt(reason="will_be_resumed")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, service.resume_threadsafe)
        await asyncio.sleep(0)
        self.assertFalse(ks.halted)
        stored = await store.get(KILL_SWITCH_REDIS_KEY)
        assert isinstance(stored, dict)
        self.assertFalse(stored["halted"])

    def test_halt_threadsafe_without_loop_falls_back_to_local_only(self) -> None:
        # 不调 bootstrap → self._loop is None → fall back
        service, ks, _store, _bus = _make_service(process_role="execution")
        service.halt_threadsafe("emergency_halt_pre_bootstrap")
        self.assertTrue(ks.halted)
        self.assertEqual(ks.status()["reason"], "emergency_halt_pre_bootstrap")

    def test_resume_threadsafe_without_loop_falls_back_to_local_only(self) -> None:
        service, ks, _store, _bus = _make_service(process_role="execution")
        ks.halt(reason="seed")
        service.resume_threadsafe()
        self.assertFalse(ks.halted)


if __name__ == "__main__":
    unittest.main()
