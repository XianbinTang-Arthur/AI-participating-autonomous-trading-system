"""FillEventHotCache 单元测试。

覆盖范围
========
- 未 bootstrap 状态：fills_for_scope_sync 返回 None
- bootstrap：空 Redis / 有 index hydrate / Redis 异常 best-effort
- publish：本地 dict + Redis per-fid + index；fill_id 去重
- fire_and_forget_publish：eager local apply + async task 调度
- _handle_remote_event：apply 新事件 / skip 重复 / parse 失败
- FIFO 淘汰：超过 max_capacity 时淘汰最旧条目
- fills_for_scope_sync：scope 过滤 + since 过滤
- snapshot() 自省
- Redis 失败 best-effort 不阻塞
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from aats.bus.memory_bus import InMemoryEventBus
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.execution import FillEvent
from aats.services.execution_engine.fill_event_cache import (
    FILL_INDEX_KEY,
    FillEventHotCache,
    _fill_key,
    _REDIS_TTL_SECONDS,
)
from aats.services.runtime_scope import RuntimeStateScope
from aats.storage.hot_state_store import InMemoryHotStateStore


# ─────────────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────────────


class _ExplodingStore(InMemoryHotStateStore):
    def __init__(
        self,
        *,
        raise_on_set: bool = True,
        raise_on_get: bool = False,
        raise_on_get_many: bool = False,
    ) -> None:
        super().__init__()
        self._raise_on_set = raise_on_set
        self._raise_on_get = raise_on_get
        self._raise_on_get_many = raise_on_get_many

    async def get(self, key: str) -> Any | None:
        if self._raise_on_get:
            raise RuntimeError("redis_get_boom")
        return await super().get(key)

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        if self._raise_on_set:
            raise RuntimeError("redis_set_boom")
        await super().set(key, value, ttl_seconds=ttl_seconds)

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        if self._raise_on_get_many:
            raise RuntimeError("redis_get_many_boom")
        return await super().get_many(keys)


def _logger() -> logging.Logger:
    return logging.getLogger("test.fill_event_hot_cache")


_BASE_TS = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)

_SCOPE = RuntimeStateScope(
    product_type="spot",
    margin_mode="cash",
    allowed_symbols=("BTC-USDT",),
    default_symbol="BTC-USDT",
)


def _make_fill(
    *,
    fill_id: str = "fill-001",
    client_order_id: str = "coid-001",
    symbol: str = "BTC-USDT",
    ingestion_timestamp: datetime | None = None,
) -> FillEvent:
    if ingestion_timestamp is None:
        ingestion_timestamp = _BASE_TS
    return FillEvent(
        fill_id=fill_id,
        decision_id="dec-test",
        intent_id="int-test",
        client_order_id=client_order_id,
        exchange_order_id="exo-test",
        symbol=symbol,
        side="buy",
        fill_qty=Decimal("0.5"),
        fill_price=Decimal("60000"),
        fee_amount=Decimal("0.01"),
        liquidity_role="taker",
        exchange_timestamp=ingestion_timestamp,
        ingestion_timestamp=ingestion_timestamp,
        product_type="spot",
        margin_mode="cash",
        position_intent="open_long",
    )


async def _boot(
    cache: FillEventHotCache,
    *,
    store: InMemoryHotStateStore | None = None,
    bus: InMemoryEventBus | None = None,
    truth_loader=None,
    subscribe: bool = True,
) -> tuple[InMemoryHotStateStore, InMemoryEventBus]:
    s = store if store is not None else InMemoryHotStateStore()
    b = bus if bus is not None else InMemoryEventBus()
    loader = truth_loader if truth_loader is not None else (lambda _limit: [])
    await cache.bootstrap(
        hot_state_store=s,
        bus=b,
        process_role="execution",
        truth_loader=loader,
        subscribe=subscribe,
    )
    return s, b


def _make_remote_message(fill: FillEvent) -> dict[str, Any]:
    envelope = build_envelope(
        topic=topics.FILL_EVENTS,
        key=fill.fill_id,
        payload_model=fill,
        source_component="aats.execution_engine.fill_event_cache",
    )
    return {
        "topic": topics.FILL_EVENTS,
        "key": fill.fill_id,
        "payload": envelope.model_dump(mode="json"),
    }


# ─────────────────────────────────────────────────────────────────────
# 未 bootstrap 状态
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCacheBare(unittest.TestCase):
    def test_fills_returns_none_before_bootstrap(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        self.assertIsNone(cache.fills_for_scope_sync(_SCOPE))

    def test_snapshot_shows_not_bootstrapped(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        snap = cache.snapshot()
        self.assertFalse(snap["bootstrapped"])
        self.assertFalse(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 0)


# ─────────────────────────────────────────────────────────────────────
# bootstrap
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCacheBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_empty_redis(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 0)
        self.assertEqual(cache.fills_for_scope_sync(_SCOPE), [])

    async def test_bootstrap_without_truth_loader_forces_database_fallback(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        store = InMemoryHotStateStore()
        bus = InMemoryEventBus()

        await cache.bootstrap(
            hot_state_store=store,
            bus=bus,
            process_role="execution",
        )

        self.assertFalse(cache.snapshot()["truth_verified"])
        self.assertIsNone(cache.fills_for_scope_sync(_SCOPE))

    async def test_bootstrap_truth_replaces_incomplete_redis_history(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        store = InMemoryHotStateStore()
        close_fill = _make_fill(
            fill_id="fill-close",
            ingestion_timestamp=_BASE_TS + timedelta(minutes=10),
        )
        open_fill = _make_fill(fill_id="fill-open")
        await store.set(_fill_key(close_fill.fill_id), close_fill.model_dump(mode="json"))
        await store.set(
            FILL_INDEX_KEY,
            {"all_fill_ids": [close_fill.fill_id], "version": 1},
        )

        await _boot(
            cache,
            store=store,
            truth_loader=lambda limit: [open_fill, close_fill][-limit:],
        )

        fills = cache.fills_for_scope_sync(_SCOPE)
        assert fills is not None
        self.assertTrue(cache.snapshot()["truth_verified"])
        self.assertEqual([fill.fill_id for fill in fills], ["fill-open", "fill-close"])

    async def test_bootstrap_truth_failure_forces_database_fallback(self) -> None:
        cache = FillEventHotCache(logger=_logger())

        def _raise(_limit: int) -> list[FillEvent]:
            raise RuntimeError("postgres_unavailable")

        await _boot(cache, truth_loader=_raise)

        self.assertFalse(cache.snapshot()["truth_verified"])
        self.assertIsNone(cache.fills_for_scope_sync(_SCOPE))

    async def test_bootstrap_hydrates_from_index(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        store = InMemoryHotStateStore()
        f1 = _make_fill(fill_id="fill-A")
        f2 = _make_fill(fill_id="fill-B")
        for f in (f1, f2):
            await store.set(_fill_key(f.fill_id), f.model_dump(mode="json"))
        await store.set(FILL_INDEX_KEY, {
            "all_fill_ids": ["fill-A", "fill-B"],
            "version": 1,
        })
        await _boot(
            cache,
            store=store,
            truth_loader=lambda limit: [f1, f2][-limit:],
        )
        self.assertEqual(cache.snapshot()["cached_count"], 2)

    async def test_bootstrap_redis_get_fails_best_effort(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        store = _ExplodingStore(raise_on_set=False, raise_on_get=True)
        await _boot(cache, store=store)
        self.assertTrue(cache.snapshot()["bootstrapped"])
        self.assertEqual(cache.snapshot()["cached_count"], 0)

    async def test_bootstrap_get_many_fails_best_effort(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        store = _ExplodingStore(raise_on_set=False, raise_on_get=False, raise_on_get_many=True)
        await store.set(FILL_INDEX_KEY, {"all_fill_ids": ["x"], "version": 1})
        await _boot(cache, store=store)
        self.assertEqual(cache.snapshot()["cached_count"], 0)

    async def test_bootstrap_subscribe_false(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache, subscribe=False)
        self.assertTrue(cache.snapshot()["bootstrapped"])
        self.assertFalse(cache.snapshot()["subscribed"])


# ─────────────────────────────────────────────────────────────────────
# publish + 去重
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCachePublish(unittest.IsolatedAsyncioTestCase):
    async def test_publish_appends_to_local(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        f = _make_fill(fill_id="fill-1")
        await cache.publish(f)
        fills = cache.fills_for_scope_sync(_SCOPE)
        assert fills is not None
        self.assertEqual(len(fills), 1)

    async def test_publish_writes_redis(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        store = InMemoryHotStateStore()
        await _boot(cache, store=store)
        f = _make_fill(fill_id="fill-1")
        await cache.publish(f)
        stored = await store.get(_fill_key("fill-1"))
        self.assertIsNotNone(stored)
        idx = await store.get(FILL_INDEX_KEY)
        self.assertIsNotNone(idx)
        self.assertIn("fill-1", idx["all_fill_ids"])

    async def test_publish_dedup_by_fill_id(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        f = _make_fill(fill_id="fill-dup")
        await cache.publish(f)
        await cache.publish(f)  # 重复，应被 noop
        fills = cache.fills_for_scope_sync(_SCOPE)
        assert fills is not None
        self.assertEqual(len(fills), 1)

    async def test_publish_redis_failure_does_not_block(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        store = _ExplodingStore(raise_on_set=True, raise_on_get=False)
        await _boot(cache, store=store)
        f = _make_fill(fill_id="fill-boom")
        await cache.publish(f)  # 不应抛
        fills = cache.fills_for_scope_sync(_SCOPE)
        assert fills is not None
        self.assertEqual(len(fills), 1)


# ─────────────────────────────────────────────────────────────────────
# FIFO 淘汰
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCacheFIFO(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_evicts_oldest(self) -> None:
        cache = FillEventHotCache(logger=_logger(), max_capacity=3)
        await _boot(cache)
        for i in range(5):
            f = _make_fill(
                fill_id=f"fill-{i}",
                ingestion_timestamp=_BASE_TS + timedelta(seconds=i),
            )
            await cache.publish(f)
        # 容量 3，应只保留 fill-2, fill-3, fill-4
        self.assertEqual(cache.snapshot()["cached_count"], 3)
        fills = cache.fills_for_scope_sync(_SCOPE)
        assert fills is not None
        fids = {f.fill_id for f in fills}
        self.assertNotIn("fill-0", fids)
        self.assertNotIn("fill-1", fids)
        self.assertIn("fill-4", fids)


# ─────────────────────────────────────────────────────────────────────
# A3 · _pending_evictions bounded queue (2026-04-21)
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCachePendingEvictionsBounded(unittest.IsolatedAsyncioTestCase):
    """确保 subscriber-only 进程不会因为 NATS 消息持续触发 eviction 而内存
    无界增长 —— `_pending_evictions` 必须是 deque(maxlen=N)，满了丢最老。"""

    async def test_pending_evictions_is_bounded_deque(self) -> None:
        """类型必须是带 maxlen 的 deque（而不是无界 list）。"""
        from collections import deque

        cache = FillEventHotCache(logger=_logger(), max_capacity=2)
        self.assertIsInstance(
            cache._pending_evictions,
            deque,
            "FATAL：_pending_evictions 必须是 deque(maxlen=...)，否则 subscriber "
            "only 进程接收 NATS 时会无界增长（20k fills/day 累积到 6+ MB / 天）",
        )
        self.assertIsNotNone(
            cache._pending_evictions.maxlen,
            "deque 必须有 maxlen 才能真正 bounded",
        )

    async def test_subscriber_only_path_does_not_grow_unboundedly(self) -> None:
        """关键属性：走纯 subscriber 路径（_handle_remote_event）触发 eviction，
        `_pending_evictions` 不应超过 maxlen。

        这模拟 decision / gateway / market 进程的真实情况：他们只订阅 fills，
        从不 publish，所以旧代码里的 list 会随 NATS 消息无限增长。"""
        cache = FillEventHotCache(logger=_logger(), max_capacity=5)
        # 人为缩短 maxlen 便于测试
        from collections import deque
        cache._pending_evictions = deque(maxlen=10)
        await _boot(cache)

        # 模拟 100 个远端 fill 到达（触发 95 次 eviction，因 max_capacity=5）
        for i in range(100):
            f = _make_fill(
                fill_id=f"remote-{i:04d}",
                ingestion_timestamp=_BASE_TS + timedelta(seconds=i),
            )
            msg = _make_remote_message(f)
            await cache._handle_remote_event(msg)

        # 关键断言：即使触发了 95 次 eviction，pending 不应超过 maxlen=10
        self.assertLessEqual(
            len(cache._pending_evictions),
            10,
            f"subscriber-only 路径下 _pending_evictions={len(cache._pending_evictions)} "
            f"> maxlen 10，说明 bounded 约束没生效，会 leak memory",
        )

    async def test_publisher_path_drains_pending_evictions(self) -> None:
        """verify publisher 路径正常 drain pending_evictions（不因换 deque 破坏）。"""
        cache = FillEventHotCache(logger=_logger(), max_capacity=2)
        await _boot(cache)

        # Publish 3 个 fill → 触发 1 次 eviction
        for i in range(3):
            f = _make_fill(
                fill_id=f"pub-{i}",
                ingestion_timestamp=_BASE_TS + timedelta(seconds=i),
            )
            await cache.publish(f)

        # publish 结束后 _pending_evictions 应被 drain
        self.assertEqual(
            len(cache._pending_evictions),
            0,
            "publisher 路径应 drain _pending_evictions（_best_effort_redis_delete_evicted）",
        )

    async def test_deque_overflow_silently_drops_oldest(self) -> None:
        """deque(maxlen) 超过上限的 append 会静默丢最老 —— 这是故意的行为，
        被丢的 fid 靠 Redis TTL=7d 兜底清理，不影响正确性。"""
        from collections import deque

        cache = FillEventHotCache(logger=_logger(), max_capacity=2)
        cache._pending_evictions = deque(maxlen=3)

        # 直接操作 deque 模拟 overflow（避免 bootstrap 依赖）
        for i in range(10):
            cache._pending_evictions.append(f"fid-{i}")

        # 只保留最后 3 个
        self.assertEqual(len(cache._pending_evictions), 3)
        self.assertEqual(
            list(cache._pending_evictions),
            ["fid-7", "fid-8", "fid-9"],
            "deque 应该保留最新的 N 个，丢最老",
        )


# ─────────────────────────────────────────────────────────────────────
# fire_and_forget_publish
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCacheFireAndForget(unittest.IsolatedAsyncioTestCase):
    async def test_fire_and_forget_none_is_noop(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        cache.fire_and_forget_publish(None)

    async def test_fire_and_forget_applies_locally(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        f = _make_fill(fill_id="fill-ff")
        cache.fire_and_forget_publish(f)
        fills = cache.fills_for_scope_sync(_SCOPE)
        assert fills is not None
        self.assertEqual(len(fills), 1)
        await asyncio.sleep(0.05)


# ─────────────────────────────────────────────────────────────────────
# remote event handling
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCacheRemoteEvent(unittest.IsolatedAsyncioTestCase):
    async def test_remote_event_applies(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        f = _make_fill(fill_id="fill-remote")
        await cache._handle_remote_event(_make_remote_message(f))
        fills = cache.fills_for_scope_sync(_SCOPE)
        assert fills is not None
        self.assertEqual(len(fills), 1)

    async def test_remote_event_dedup(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        f = _make_fill(fill_id="fill-dup-remote")
        await cache.publish(f)
        await cache._handle_remote_event(_make_remote_message(f))  # 重复
        fills = cache.fills_for_scope_sync(_SCOPE)
        assert fills is not None
        self.assertEqual(len(fills), 1)

    async def test_remote_event_parse_failure_does_not_raise(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        await cache._handle_remote_event({"garbage": True})


# ─────────────────────────────────────────────────────────────────────
# since 过滤
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCacheSinceFilter(unittest.IsolatedAsyncioTestCase):
    async def test_fills_for_scope_since_filters(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        await _boot(cache)
        for i in range(5):
            f = _make_fill(
                fill_id=f"fill-t{i}",
                ingestion_timestamp=_BASE_TS + timedelta(hours=i),
            )
            await cache.publish(f)
        cutoff = _BASE_TS + timedelta(hours=3)
        fills = cache.fills_for_scope_sync(_SCOPE, since=cutoff)
        assert fills is not None
        # 只有 t3, t4 的 ingestion_timestamp >= cutoff
        self.assertEqual(len(fills), 2)


# ─────────────────────────────────────────────────────────────────────
# snapshot 自省
# ─────────────────────────────────────────────────────────────────────


class TestFillEventHotCacheSnapshot(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_reflects_state(self) -> None:
        cache = FillEventHotCache(logger=_logger(), max_capacity=100)
        await _boot(cache)
        f = _make_fill(fill_id="fill-snap")
        await cache.publish(f)
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 1)
        self.assertEqual(snap["max_capacity"], 100)


# ─────────────────────────────────────────────────────────────────────
# Redis TTL 验证
# ─────────────────────────────────────────────────────────────────────


class _TTLTrackingStore(InMemoryHotStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.last_ttl: float | None = None

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        self.last_ttl = ttl_seconds
        await super().set(key, value, ttl_seconds=ttl_seconds)


class TestFillEventHotCacheRedisTTL(unittest.IsolatedAsyncioTestCase):
    async def test_redis_set_passes_ttl(self) -> None:
        cache = FillEventHotCache(logger=_logger())
        store = _TTLTrackingStore()
        bus = InMemoryEventBus()
        await cache.bootstrap(hot_state_store=store, bus=bus, process_role="execution")
        f = _make_fill(fill_id="fill-ttl")
        await cache.publish(f)
        self.assertEqual(store.last_ttl, _REDIS_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
