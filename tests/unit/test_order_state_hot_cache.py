"""OrderStateHotCache 单元测试。

覆盖范围
========
- 未 bootstrap 状态：open_orders_for_scope_sync 返回 None
- bootstrap：空 Redis / 有 index hydrate / Redis 异常 best-effort
- publish：本地 dict + Redis per-coid + index；idempotent ts 规则
- fire_and_forget_publish：eager local apply + async task 调度
- _handle_remote_event：apply 新事件 / skip 旧事件 / parse 失败
- open_orders_for_scope_sync：scope 过滤 + terminal 排除
- snapshot() 自省
- Redis/NATS 失败 best-effort 不阻塞
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
from aats.schemas.execution import OrderState
from aats.services.execution_engine.order_state_cache import (
    ORDER_STATE_INDEX_KEY,
    OrderStateHotCache,
    _order_state_key,
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
    return logging.getLogger("test.order_state_hot_cache")


_BASE_TS = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)

_SCOPE = RuntimeStateScope(
    product_type="spot",
    margin_mode="cash",
    allowed_symbols=("BTC-USDT",),
    default_symbol="BTC-USDT",
)


def _make_order(
    *,
    client_order_id: str = "coid-001",
    status: str = "SUBMITTED",
    symbol: str = "BTC-USDT",
    last_update_ts: datetime | None = None,
) -> OrderState:
    if last_update_ts is None:
        last_update_ts = _BASE_TS
    return OrderState(
        decision_id="dec-test",
        intent_id="int-test",
        symbol=symbol,
        client_order_id=client_order_id,
        status=status,  # type: ignore[arg-type]
        requested_qty=Decimal("1.0"),
        remaining_qty=Decimal("1.0"),
        last_update_ts=last_update_ts,
        submitted_ts=last_update_ts,
        product_type="spot",
        margin_mode="cash",
        position_intent="open_long",
    )


async def _boot(
    cache: OrderStateHotCache,
    *,
    store: InMemoryHotStateStore | None = None,
    bus: InMemoryEventBus | None = None,
    subscribe: bool = True,
) -> tuple[InMemoryHotStateStore, InMemoryEventBus]:
    s = store if store is not None else InMemoryHotStateStore()
    b = bus if bus is not None else InMemoryEventBus()
    await cache.bootstrap(hot_state_store=s, bus=b, process_role="execution", subscribe=subscribe)
    return s, b


def _make_remote_message(order: OrderState) -> dict[str, Any]:
    envelope = build_envelope(
        topic=topics.ORDER_UPDATES,
        key=order.client_order_id,
        payload_model=order,
        source_component="aats.execution_engine.order_state_cache",
    )
    return {
        "topic": topics.ORDER_UPDATES,
        "key": order.client_order_id,
        "payload": envelope.model_dump(mode="json"),
    }


# ─────────────────────────────────────────────────────────────────────
# 未 bootstrap 状态
# ─────────────────────────────────────────────────────────────────────


class TestOrderStateHotCacheBare(unittest.TestCase):
    def test_open_orders_returns_none_before_bootstrap(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        self.assertIsNone(cache.open_orders_for_scope_sync(_SCOPE))

    def test_snapshot_shows_not_bootstrapped(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        snap = cache.snapshot()
        self.assertFalse(snap["bootstrapped"])
        self.assertFalse(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 0)


# ─────────────────────────────────────────────────────────────────────
# bootstrap
# ─────────────────────────────────────────────────────────────────────


class TestOrderStateHotCacheBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_empty_redis(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 0)
        self.assertEqual(cache.open_orders_for_scope_sync(_SCOPE), [])

    async def test_bootstrap_hydrates_from_index(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        store = InMemoryHotStateStore()
        o1 = _make_order(client_order_id="coid-A", status="SUBMITTED")
        o2 = _make_order(client_order_id="coid-B", status="FILLED")
        for o in (o1, o2):
            await store.set(_order_state_key(o.client_order_id), o.model_dump(mode="json"))
        await store.set(ORDER_STATE_INDEX_KEY, {
            "all_coids": ["coid-A", "coid-B"],
            "version": 1,
        })
        await _boot(cache, store=store)
        self.assertEqual(cache.snapshot()["cached_count"], 2)
        # open orders 只包含非 terminal
        open_orders = cache.open_orders_for_scope_sync(_SCOPE)
        assert open_orders is not None
        self.assertEqual(len(open_orders), 1)
        self.assertEqual(open_orders[0].client_order_id, "coid-A")

    async def test_bootstrap_redis_get_fails_best_effort(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        store = _ExplodingStore(raise_on_set=False, raise_on_get=True)
        await _boot(cache, store=store)
        self.assertTrue(cache.snapshot()["bootstrapped"])
        self.assertEqual(cache.snapshot()["cached_count"], 0)

    async def test_bootstrap_get_many_fails_best_effort(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        store = _ExplodingStore(raise_on_set=False, raise_on_get=False, raise_on_get_many=True)
        await store.set(ORDER_STATE_INDEX_KEY, {"all_coids": ["x"], "version": 1})
        await _boot(cache, store=store)
        self.assertTrue(cache.snapshot()["bootstrapped"])
        self.assertEqual(cache.snapshot()["cached_count"], 0)

    async def test_bootstrap_subscribe_false_defers_subscription(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache, subscribe=False)
        self.assertTrue(cache.snapshot()["bootstrapped"])
        self.assertFalse(cache.snapshot()["subscribed"])


# ─────────────────────────────────────────────────────────────────────
# publish + idempotent
# ─────────────────────────────────────────────────────────────────────


class TestOrderStateHotCachePublish(unittest.IsolatedAsyncioTestCase):
    async def test_publish_updates_local_dict(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        o = _make_order(client_order_id="coid-1", status="SUBMITTED")
        await cache.publish(o)
        orders = cache.open_orders_for_scope_sync(_SCOPE)
        assert orders is not None
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].client_order_id, "coid-1")

    async def test_publish_writes_redis_per_coid_and_index(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        store = InMemoryHotStateStore()
        await _boot(cache, store=store)
        o = _make_order(client_order_id="coid-1")
        await cache.publish(o)
        # per-coid key 应存在
        stored = await store.get(_order_state_key("coid-1"))
        self.assertIsNotNone(stored)
        # index key 应存在
        idx = await store.get(ORDER_STATE_INDEX_KEY)
        self.assertIsNotNone(idx)
        self.assertIn("coid-1", idx["all_coids"])

    async def test_publish_idempotent_skips_stale(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        o_new = _make_order(client_order_id="coid-1", last_update_ts=_BASE_TS + timedelta(seconds=10))
        o_old = _make_order(client_order_id="coid-1", last_update_ts=_BASE_TS)
        await cache.publish(o_new)
        await cache.publish(o_old)  # stale，应被 noop
        orders = cache.open_orders_for_scope_sync(_SCOPE)
        assert orders is not None
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].last_update_ts, o_new.last_update_ts)

    async def test_publish_redis_failure_does_not_block(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        store = _ExplodingStore(raise_on_set=True, raise_on_get=False)
        await _boot(cache, store=store)
        o = _make_order(client_order_id="coid-1")
        await cache.publish(o)  # 不应抛异常
        orders = cache.open_orders_for_scope_sync(_SCOPE)
        assert orders is not None
        self.assertEqual(len(orders), 1)


# ─────────────────────────────────────────────────────────────────────
# fire_and_forget_publish
# ─────────────────────────────────────────────────────────────────────


class TestOrderStateHotCacheFireAndForget(unittest.IsolatedAsyncioTestCase):
    async def test_fire_and_forget_none_is_noop(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        cache.fire_and_forget_publish(None)  # 不应抛

    async def test_fire_and_forget_applies_locally_immediately(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        o = _make_order(client_order_id="coid-ff")
        cache.fire_and_forget_publish(o)
        orders = cache.open_orders_for_scope_sync(_SCOPE)
        assert orders is not None
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].client_order_id, "coid-ff")
        await asyncio.sleep(0.05)  # 让 background task 完成


# ─────────────────────────────────────────────────────────────────────
# remote event handling
# ─────────────────────────────────────────────────────────────────────


class TestOrderStateHotCacheRemoteEvent(unittest.IsolatedAsyncioTestCase):
    async def test_remote_event_applies_new_order(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        o = _make_order(client_order_id="coid-remote", status="SUBMITTED")
        await cache._handle_remote_event(_make_remote_message(o))
        orders = cache.open_orders_for_scope_sync(_SCOPE)
        assert orders is not None
        self.assertEqual(len(orders), 1)

    async def test_remote_event_skips_stale(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        o_new = _make_order(client_order_id="coid-1", last_update_ts=_BASE_TS + timedelta(seconds=10))
        o_old = _make_order(client_order_id="coid-1", last_update_ts=_BASE_TS)
        await cache.publish(o_new)
        await cache._handle_remote_event(_make_remote_message(o_old))
        orders = cache.open_orders_for_scope_sync(_SCOPE)
        assert orders is not None
        self.assertEqual(orders[0].last_update_ts, o_new.last_update_ts)

    async def test_remote_event_parse_failure_does_not_raise(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        bad = {"topic": "x", "key": "y", "payload": {"garbage": True}}
        await cache._handle_remote_event(bad)  # 不应抛


# ─────────────────────────────────────────────────────────────────────
# scope 过滤
# ─────────────────────────────────────────────────────────────────────


class TestOrderStateHotCacheScopeFilter(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_orders_excluded(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        ts = _BASE_TS
        for i, status in enumerate(["SUBMITTED", "FILLED", "CANCELED", "PARTIALLY_FILLED"]):
            o = _make_order(
                client_order_id=f"coid-{i}",
                status=status,
                last_update_ts=ts + timedelta(seconds=i),
            )
            await cache.publish(o)
        orders = cache.open_orders_for_scope_sync(_SCOPE)
        assert orders is not None
        # FILLED 和 CANCELED 是 terminal，不应出现
        coids = {o.client_order_id for o in orders}
        self.assertIn("coid-0", coids)  # SUBMITTED
        self.assertNotIn("coid-1", coids)  # FILLED
        self.assertNotIn("coid-2", coids)  # CANCELED
        self.assertIn("coid-3", coids)  # PARTIALLY_FILLED


# ─────────────────────────────────────────────────────────────────────
# snapshot 自省
# ─────────────────────────────────────────────────────────────────────


class TestOrderStateHotCacheSnapshot(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_reflects_state(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        await _boot(cache)
        o = _make_order(client_order_id="coid-snap", status="SUBMITTED")
        await cache.publish(o)
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 1)
        self.assertEqual(snap["open_count"], 1)


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


class TestOrderStateHotCacheRedisTTL(unittest.IsolatedAsyncioTestCase):
    async def test_redis_set_passes_ttl(self) -> None:
        cache = OrderStateHotCache(logger=_logger())
        store = _TTLTrackingStore()
        bus = InMemoryEventBus()
        await cache.bootstrap(hot_state_store=store, bus=bus, process_role="execution")
        o = _make_order(client_order_id="coid-ttl")
        await cache.publish(o)
        self.assertEqual(store.last_ttl, _REDIS_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
