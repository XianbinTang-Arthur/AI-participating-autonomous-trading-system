"""StreamSnapshotCache 单元测试。

覆盖范围
========
- update / latest / recent_by_key / get(event_id) 基础读写
- bootstrap：空 Redis / latest+recent hydrate / Redis 异常 best-effort
- flush_to_hot_state：dirty 写入 Redis（含 TTL）/ 失败重入 dirty / 无 store 跳过
- by_id 索引：容量淘汰 FIFO
- register_remote_subscription
- snapshot() 自省（无——StreamSnapshotCache 无 snapshot()，跳过）

不在本测试范围：
- 真实 Redis / NATS 后端 → 集成测试
"""
from __future__ import annotations

import logging
import unittest
from collections import deque
from datetime import datetime, timezone
from typing import Any

from aats.events import topics
from aats.schemas.common import EventEnvelope
from aats.storage.hot_state_store import InMemoryHotStateStore
from aats.storage.stream_snapshot_cache import (
    STREAM_CACHE_TOPICS,
    StreamSnapshotCache,
    _REDIS_TTL_SECONDS,
    _redis_key_latest,
    _redis_key_recent,
)


# ─────────────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────────────


class _ExplodingStore(InMemoryHotStateStore):
    def __init__(self, *, raise_on_set: bool = True, raise_on_get: bool = False) -> None:
        super().__init__()
        self._raise_on_set = raise_on_set
        self._raise_on_get = raise_on_get

    async def get(self, key: str) -> Any | None:
        if self._raise_on_get:
            raise RuntimeError("redis_get_boom")
        return await super().get(key)

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        if self._raise_on_set:
            raise RuntimeError("redis_set_boom")
        await super().set(key, value, ttl_seconds=ttl_seconds)


class _TTLTrackingStore(InMemoryHotStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.ttls: dict[str, float | None] = {}

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        self.ttls[key] = ttl_seconds
        await super().set(key, value, ttl_seconds=ttl_seconds)


def _logger() -> logging.Logger:
    return logging.getLogger("test.stream_snapshot_cache")


_TOPIC = topics.MARKET_SNAPSHOTS
_BASE_TS = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


_ENVELOPE_SEQ = 0


def _make_envelope(
    *,
    topic: str = _TOPIC,
    key: str = "BTC-USDT",
    event_id: str | None = None,
    ts: datetime | None = None,
) -> EventEnvelope:
    global _ENVELOPE_SEQ
    if ts is None:
        ts = _BASE_TS
    if event_id is None:
        _ENVELOPE_SEQ += 1
        event_id = f"auto-evt-{_ENVELOPE_SEQ}"
    return EventEnvelope(
        event_type="MarketSnapshot",
        source_component="test",
        topic=topic,
        key=key,
        event_id=event_id,
        event_timestamp=ts,
        payload={"px": 60000, "ts": ts.isoformat()},
    )


# ─────────────────────────────────────────────────────────────────────
# 基础读写
# ─────────────────────────────────────────────────────────────────────


class TestStreamSnapshotCacheBasic(unittest.TestCase):
    def test_update_and_latest(self) -> None:
        cache = StreamSnapshotCache()
        env = _make_envelope(key="BTC-USDT")
        cache.update(env)
        self.assertEqual(cache.latest(_TOPIC, "BTC-USDT"), env)

    def test_latest_with_none_key(self) -> None:
        cache = StreamSnapshotCache()
        env = _make_envelope(key="BTC-USDT")
        cache.update(env)
        # latest(topic, None) 返回该 topic 的全局最新
        self.assertEqual(cache.latest(_TOPIC), env)

    def test_latest_returns_none_when_empty(self) -> None:
        cache = StreamSnapshotCache()
        self.assertIsNone(cache.latest(_TOPIC, "BTC-USDT"))

    def test_recent_by_key(self) -> None:
        cache = StreamSnapshotCache()
        envs = []
        for i in range(5):
            from datetime import timedelta
            env = _make_envelope(
                key="BTC-USDT",
                event_id=f"evt-{i}",
                ts=_BASE_TS + timedelta(seconds=i),
            )
            cache.update(env)
            envs.append(env)
        recent = cache.recent_by_key(_TOPIC, "BTC-USDT", 3)
        self.assertEqual(len(recent), 3)
        # 应按时间升序返回最近 3 条
        self.assertEqual(recent[0].event_id, "evt-2")
        self.assertEqual(recent[2].event_id, "evt-4")

    def test_recent_by_key_empty(self) -> None:
        cache = StreamSnapshotCache()
        self.assertEqual(cache.recent_by_key(_TOPIC, "ETH-USDT", 10), [])

    def test_get_by_event_id(self) -> None:
        cache = StreamSnapshotCache()
        env = _make_envelope(event_id="evt-unique")
        cache.update(env)
        self.assertEqual(cache.get("evt-unique"), env)

    def test_get_by_event_id_returns_none(self) -> None:
        cache = StreamSnapshotCache()
        self.assertIsNone(cache.get("nonexistent"))


# ─────────────────────────────────────────────────────────────────────
# by_id 容量淘汰
# ─────────────────────────────────────────────────────────────────────


class TestStreamSnapshotCacheByIdCapacity(unittest.TestCase):
    def test_by_id_fifo_eviction(self) -> None:
        cache = StreamSnapshotCache(by_id_capacity=3)
        for i in range(5):
            env = _make_envelope(event_id=f"evt-{i}")
            cache.update(env)
        # 容量 3，最老的 evt-0, evt-1 应被淘汰
        self.assertIsNone(cache.get("evt-0"))
        self.assertIsNone(cache.get("evt-1"))
        self.assertIsNotNone(cache.get("evt-2"))
        self.assertIsNotNone(cache.get("evt-4"))


# ─────────────────────────────────────────────────────────────────────
# bootstrap
# ─────────────────────────────────────────────────────────────────────


class TestStreamSnapshotCacheBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_from_empty_redis(self) -> None:
        cache = StreamSnapshotCache()
        store = InMemoryHotStateStore()
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        self.assertIsNone(cache.latest(_TOPIC, "BTC-USDT"))

    async def test_bootstrap_hydrates_latest(self) -> None:
        cache = StreamSnapshotCache()
        store = InMemoryHotStateStore()
        env = _make_envelope(key="BTC-USDT", event_id="evt-boot")
        key = _redis_key_latest(_TOPIC, "BTC-USDT")
        await store.set(key, env.model_dump(mode="json"))
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        restored = cache.latest(_TOPIC, "BTC-USDT")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.event_id, "evt-boot")

    async def test_bootstrap_hydrates_recent(self) -> None:
        cache = StreamSnapshotCache()
        store = InMemoryHotStateStore()
        from datetime import timedelta
        envs = []
        for i in range(3):
            env = _make_envelope(
                key="BTC-USDT",
                event_id=f"evt-r{i}",
                ts=_BASE_TS + timedelta(seconds=i),
            )
            envs.append(env.model_dump(mode="json"))
        key = _redis_key_recent(_TOPIC, "BTC-USDT")
        await store.set(key, envs)
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        recent = cache.recent_by_key(_TOPIC, "BTC-USDT", 10)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[2].event_id, "evt-r2")
        # recent 恢复后 latest 也应被补齐
        latest = cache.latest(_TOPIC, "BTC-USDT")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.event_id, "evt-r2")

    async def test_bootstrap_redis_failure_best_effort(self) -> None:
        cache = StreamSnapshotCache()
        store = _ExplodingStore(raise_on_get=True)
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        # 不应抛异常，cache 保持空
        self.assertIsNone(cache.latest(_TOPIC, "BTC-USDT"))

    async def test_bootstrap_bad_data_skipped(self) -> None:
        cache = StreamSnapshotCache()
        store = InMemoryHotStateStore()
        key = _redis_key_latest(_TOPIC, "BTC-USDT")
        await store.set(key, {"not_a_valid_envelope": True})
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        self.assertIsNone(cache.latest(_TOPIC, "BTC-USDT"))


# ─────────────────────────────────────────────────────────────────────
# flush_to_hot_state
# ─────────────────────────────────────────────────────────────────────


class TestStreamSnapshotCacheFlush(unittest.IsolatedAsyncioTestCase):
    async def test_flush_writes_dirty_keys_to_redis(self) -> None:
        cache = StreamSnapshotCache()
        store = InMemoryHotStateStore()
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        env = _make_envelope(key="BTC-USDT", event_id="evt-flush")
        cache.update(env)
        await cache.flush_to_hot_state()
        # latest 应在 Redis 中
        stored = await store.get(_redis_key_latest(_TOPIC, "BTC-USDT"))
        self.assertIsNotNone(stored)
        # recent 应在 Redis 中
        recent_stored = await store.get(_redis_key_recent(_TOPIC, "BTC-USDT"))
        self.assertIsNotNone(recent_stored)
        self.assertEqual(len(recent_stored), 1)

    async def test_flush_clears_dirty_set(self) -> None:
        cache = StreamSnapshotCache()
        store = InMemoryHotStateStore()
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        env = _make_envelope(key="BTC-USDT")
        cache.update(env)
        await cache.flush_to_hot_state()
        # dirty 已清空，再 flush 不应写 Redis（无 dirty）
        self.assertEqual(len(cache._dirty_keys), 0)

    async def test_flush_failure_re_adds_dirty(self) -> None:
        cache = StreamSnapshotCache()
        store = _ExplodingStore(raise_on_set=True, raise_on_get=False)
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        env = _make_envelope(key="BTC-USDT")
        cache.update(env)
        await cache.flush_to_hot_state()
        # 失败的 key 应被重新加入 dirty
        self.assertIn((_TOPIC, "BTC-USDT"), cache._dirty_keys)

    async def test_flush_without_store_is_noop(self) -> None:
        cache = StreamSnapshotCache()
        env = _make_envelope(key="BTC-USDT")
        cache.update(env)
        await cache.flush_to_hot_state()  # 不应抛（_hot_state_store is None）

    async def test_flush_passes_ttl(self) -> None:
        """R-1 修复验证：flush 写入的 Redis keys 应带 TTL。"""
        cache = StreamSnapshotCache()
        store = _TTLTrackingStore()
        await cache.bootstrap(
            hot_state_store=store,
            symbols=["BTC-USDT"],
            logger=_logger(),
        )
        env = _make_envelope(key="BTC-USDT")
        cache.update(env)
        await cache.flush_to_hot_state()
        # 所有写入的 key 都应带 TTL
        for key, ttl in store.ttls.items():
            if key.startswith("aats:hot:stream_cache:"):
                self.assertEqual(
                    ttl,
                    _REDIS_TTL_SECONDS,
                    f"key {key} 的 TTL 应为 {_REDIS_TTL_SECONDS}，实际为 {ttl}",
                )


# ─────────────────────────────────────────────────────────────────────
# deque maxlen
# ─────────────────────────────────────────────────────────────────────


class TestStreamSnapshotCacheDequeMaxlen(unittest.TestCase):
    def test_deque_respects_default_max_recent(self) -> None:
        cache = StreamSnapshotCache(default_max_recent=3)
        from datetime import timedelta
        for i in range(5):
            env = _make_envelope(
                key="BTC-USDT",
                event_id=f"evt-{i}",
                ts=_BASE_TS + timedelta(seconds=i),
            )
            cache.update(env)
        recent = cache.recent_by_key(_TOPIC, "BTC-USDT", 10)
        self.assertEqual(len(recent), 3)
        # 最老的 evt-0, evt-1 应被淘汰
        self.assertEqual(recent[0].event_id, "evt-2")

    def test_per_topic_max_recent_override(self) -> None:
        cache = StreamSnapshotCache(
            default_max_recent=10,
            max_recent_by_topic={_TOPIC: 2},
        )
        from datetime import timedelta
        for i in range(5):
            env = _make_envelope(
                key="BTC-USDT",
                event_id=f"evt-{i}",
                ts=_BASE_TS + timedelta(seconds=i),
            )
            cache.update(env)
        recent = cache.recent_by_key(_TOPIC, "BTC-USDT", 10)
        self.assertEqual(len(recent), 2)


# ─────────────────────────────────────────────────────────────────────
# register_remote_subscription
# ─────────────────────────────────────────────────────────────────────


class _FakeBus:
    def __init__(self) -> None:
        self.subscribed_topics: list[str] = []

    async def subscribe(self, topic: str, handler: Any) -> None:
        self.subscribed_topics.append(topic)


class TestStreamSnapshotCacheRemoteSubscription(unittest.IsolatedAsyncioTestCase):
    async def test_subscribes_to_all_stream_cache_topics(self) -> None:
        cache = StreamSnapshotCache()
        bus = _FakeBus()
        await cache.register_remote_subscription(bus)
        for topic in STREAM_CACHE_TOPICS:
            self.assertIn(topic, bus.subscribed_topics)


if __name__ == "__main__":
    unittest.main()
