"""Stage 6 Slice 6.5 单元测试：ObligationHotStateCache。

设计文档
========
docs/task/stage_6_slice_6_5_obligation_hot_state_design.md §9

覆盖范围
========
- bootstrap：空 / index hydrate / get_many 失败 / parse 失败 / 订阅 NATS
- publish：本地 dict + Redis per-coid + index + NATS 广播；best-effort 失败
- publish idempotent：last_update_ts 退化 / 相同 / 更新
- get_sync / active_sync / all_sync：未 bootstrap → None；bootstrap 后正常
- _handle_remote_event：apply 新事件 / skip 旧事件 / parse 失败不抛
- snapshot() 自省
- register_remote_subscription deferred 路径
- apply_sync（无 event loop 的 sync path）

不在本测试范围：
- 真实 Redis 后端 → 集成测试（待 Slice 6.5.1 补）
- 4 进程跨 NATS 跨容器 → 真跑验证（runbook §9.8）
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
from aats.schemas.execution import OrderObligation
from aats.services.execution_engine.obligation_cache import (
    OBLIGATION_EVENT_TYPE,
    OBLIGATION_INDEX_KEY,
    ObligationHotStateCache,
    _obligation_key,
)
from aats.storage.hot_state_store import InMemoryHotStateStore


# ─────────────────────────────────────────────────────────────────────
# Test 双件
# ─────────────────────────────────────────────────────────────────────


class _ExplodingHotStateStore(InMemoryHotStateStore):
    """所有 set 都抛异常的 HotStateStore，用于测试 best-effort 写。"""

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

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        if self._raise_on_get_many:
            raise RuntimeError("redis_get_many_boom")
        return await super().get_many(keys)


class _ExplodingBus(InMemoryEventBus):
    """publish 抛异常的 bus，用于测试 NATS 失败 best-effort。"""

    async def publish(self, *, topic: str, key: str, payload: Any) -> None:  # type: ignore[override]
        raise RuntimeError("nats_publish_boom")


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test.obligation_hot_state_cache")
    logger.setLevel(logging.DEBUG)
    return logger


def _make_obligation(
    *,
    client_order_id: str = "coid-001",
    status: str = "ACTIVE",
    reserved_amount: str = "100.0",
    consumed_amount: str = "0",
    released_amount: str = "0",
    last_update_ts: datetime | None = None,
) -> OrderObligation:
    if last_update_ts is None:
        last_update_ts = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
    return OrderObligation(
        client_order_id=client_order_id,
        decision_id="decision-test",
        intent_id="intent-test",
        symbol="BTC-USDT",
        side="buy",
        reserve_currency="USDT",
        reserved_amount=Decimal(reserved_amount),
        consumed_amount=Decimal(consumed_amount),
        released_amount=Decimal(released_amount),
        status=status,  # type: ignore[arg-type]
        product_type="spot",
        margin_mode="cash",
        last_update_ts=last_update_ts,
    )


def _make_cache(*, process_role: str = "execution") -> ObligationHotStateCache:
    return ObligationHotStateCache(logger=_make_logger())


async def _boot(
    cache: ObligationHotStateCache,
    *,
    store: InMemoryHotStateStore | None = None,
    bus: InMemoryEventBus | None = None,
    process_role: str = "execution",
    subscribe: bool = True,
) -> tuple[InMemoryHotStateStore, InMemoryEventBus]:
    store_obj = store if store is not None else InMemoryHotStateStore()
    bus_obj = bus if bus is not None else InMemoryEventBus()
    await cache.bootstrap(
        hot_state_store=store_obj,
        bus=bus_obj,
        process_role=process_role,
        subscribe=subscribe,
    )
    return store_obj, bus_obj


def _make_remote_message(obligation: OrderObligation) -> dict[str, Any]:
    """模拟 NATS OBLIGATION_UPDATES envelope dict。"""
    envelope = build_envelope(
        topic=topics.OBLIGATION_UPDATES,
        key=obligation.client_order_id,
        payload_model=obligation,
        source_component="aats.execution_engine.obligation_cache",
    )
    return {
        "topic": topics.OBLIGATION_UPDATES,
        "key": obligation.client_order_id,
        "payload": envelope.model_dump(mode="json"),
    }


# ─────────────────────────────────────────────────────────────────────
# 零参构造 / 未 bootstrap 状态
# ─────────────────────────────────────────────────────────────────────


class TestObligationHotStateCacheBareConstruct(unittest.TestCase):
    def test_bare_construct_get_sync_returns_none(self) -> None:
        cache = _make_cache()
        self.assertIsNone(cache.get_sync("coid-001"))

    def test_bare_construct_active_sync_returns_none(self) -> None:
        cache = _make_cache()
        self.assertIsNone(cache.active_sync())

    def test_bare_construct_all_sync_returns_none(self) -> None:
        cache = _make_cache()
        self.assertIsNone(cache.all_sync())

    def test_bare_construct_snapshot_shows_not_bootstrapped(self) -> None:
        cache = _make_cache()
        snap = cache.snapshot()
        self.assertFalse(snap["bootstrapped"])
        self.assertFalse(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 0)


# ─────────────────────────────────────────────────────────────────────
# bootstrap
# ─────────────────────────────────────────────────────────────────────


class TestObligationHotStateCacheBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_from_empty_redis_keeps_local_dict_empty(self) -> None:
        cache = _make_cache()
        await _boot(cache)
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 0)
        self.assertEqual(snap["active_count"], 0)
        # bootstrap 后 active_sync 应返回空 list（不是 None）
        self.assertEqual(cache.active_sync(), [])
        self.assertEqual(cache.all_sync(), [])

    async def test_bootstrap_hydrates_from_redis_index(self) -> None:
        cache = _make_cache()
        store = InMemoryHotStateStore()
        o1 = _make_obligation(client_order_id="coid-A", status="ACTIVE")
        o2 = _make_obligation(client_order_id="coid-B", status="PARTIALLY_CONSUMED")
        o3 = _make_obligation(client_order_id="coid-C", status="RELEASED")
        for o in (o1, o2, o3):
            await store.set(_obligation_key(o.client_order_id), o.model_dump(mode="json"))
        await store.set(
            OBLIGATION_INDEX_KEY,
            {
                "all_coids": ["coid-A", "coid-B", "coid-C"],
                "active_coids": ["coid-A", "coid-B"],
                "version": 42,
                "updated_at": "2026-04-08T11:00:00+00:00",
                "writer_role": "execution",
            },
        )
        await _boot(cache, store=store)
        self.assertEqual(cache.snapshot()["cached_count"], 3)
        self.assertEqual(cache.snapshot()["active_count"], 2)
        self.assertIsNotNone(cache.get_sync("coid-A"))
        self.assertIsNotNone(cache.get_sync("coid-B"))
        self.assertIsNotNone(cache.get_sync("coid-C"))
        # active_sync 只返回 ACTIVE + PARTIALLY_CONSUMED
        active = cache.active_sync()
        assert active is not None
        self.assertEqual({o.client_order_id for o in active}, {"coid-A", "coid-B"})

    async def test_bootstrap_redis_get_failure_does_not_raise(self) -> None:
        store = _ExplodingHotStateStore(raise_on_get=True, raise_on_set=False)
        cache = _make_cache()
        await _boot(cache, store=store)
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertTrue(snap["subscribed"])
        self.assertEqual(snap["cached_count"], 0)

    async def test_bootstrap_get_many_failure_does_not_raise(self) -> None:
        store = _ExplodingHotStateStore(
            raise_on_set=False,
            raise_on_get=False,
            raise_on_get_many=True,
        )
        await store.set(
            OBLIGATION_INDEX_KEY,
            {"all_coids": ["coid-A"], "active_coids": [], "version": 1, "updated_at": "x", "writer_role": "e"},
        )
        cache = _make_cache()
        await _boot(cache, store=store)
        # get_many 抛了但 bootstrap 仍完成
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertEqual(snap["cached_count"], 0)

    async def test_bootstrap_parse_failure_does_not_raise(self) -> None:
        store = InMemoryHotStateStore()
        # Index 说有 coid-BAD，但存的 payload 不能 parse
        await store.set(_obligation_key("coid-BAD"), {"not": "an_obligation"})
        await store.set(
            OBLIGATION_INDEX_KEY,
            {"all_coids": ["coid-BAD"], "active_coids": [], "version": 1, "updated_at": "x", "writer_role": "e"},
        )
        cache = _make_cache()
        await _boot(cache, store=store)
        snap = cache.snapshot()
        self.assertTrue(snap["bootstrapped"])
        self.assertEqual(snap["cached_count"], 0)

    async def test_bootstrap_subscribes_to_obligation_updates_topic(self) -> None:
        cache = _make_cache()
        _store, bus = await _boot(cache)
        self.assertEqual(len(bus._subs[topics.OBLIGATION_UPDATES]), 1)

    async def test_bootstrap_with_subscribe_false_defers_subscription(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache, subscribe=False)
        # bootstrap 完成但未订阅
        self.assertTrue(cache.snapshot()["bootstrapped"])
        self.assertFalse(cache.snapshot()["subscribed"])
        self.assertEqual(len(bus._subs[topics.OBLIGATION_UPDATES]), 0)
        # 显式调 register_remote_subscription 才订阅
        await cache.register_remote_subscription(bus)
        self.assertTrue(cache.snapshot()["subscribed"])
        self.assertEqual(len(bus._subs[topics.OBLIGATION_UPDATES]), 1)


# ─────────────────────────────────────────────────────────────────────
# publish
# ─────────────────────────────────────────────────────────────────────


class TestObligationHotStateCachePublish(unittest.IsolatedAsyncioTestCase):
    async def test_publish_updates_local_redis_and_nats(self) -> None:
        cache = _make_cache()
        store, bus = await _boot(cache)
        # Spy 把所有 publish 记下来
        captured: list[dict] = []

        async def _recorder(message: Any) -> None:
            captured.append(message)

        await bus.subscribe(topics.OBLIGATION_UPDATES, _recorder)

        o = _make_obligation(client_order_id="coid-P1", status="ACTIVE")
        await cache.publish(o)

        # 本地 dict 立即可见
        self.assertIsNotNone(cache.get_sync("coid-P1"))
        # Redis per-coid 也写入
        stored = await store.get(_obligation_key("coid-P1"))
        self.assertIsNotNone(stored)
        assert isinstance(stored, dict)
        self.assertEqual(stored["client_order_id"], "coid-P1")
        # Redis index 也写入
        index = await store.get(OBLIGATION_INDEX_KEY)
        assert isinstance(index, dict)
        self.assertEqual(index["all_coids"], ["coid-P1"])
        self.assertEqual(index["active_coids"], ["coid-P1"])
        self.assertEqual(index["version"], 1)
        self.assertEqual(index["writer_role"], "execution")
        # NATS 广播一次（是 cache 自己发的 + recorder 订阅的）
        # 注意：InMemoryEventBus 的 publish 是同步 fan-out，recorder 会收到
        self.assertEqual(len(captured), 1)

    async def test_publish_redis_failure_still_updates_local_dict(self) -> None:
        store = _ExplodingHotStateStore(raise_on_set=True)
        cache = _make_cache()
        await _boot(cache, store=store)

        o = _make_obligation(client_order_id="coid-REDISFAIL")
        # 不抛
        await cache.publish(o)

        # 本地 cache 仍然有
        self.assertIsNotNone(cache.get_sync("coid-REDISFAIL"))

    async def test_publish_nats_failure_still_updates_local_dict(self) -> None:
        bus = _ExplodingBus()
        cache = _make_cache()
        # 用 _ExplodingBus 绕过 bootstrap subscribe 调用（否则会抛）
        # 手动先 set _bus 然后 bootstrap subscribe=False
        store = InMemoryHotStateStore()
        await cache.bootstrap(
            hot_state_store=store,
            bus=bus,
            process_role="execution",
            subscribe=False,
        )

        o = _make_obligation(client_order_id="coid-NATSFAIL")
        # 不抛
        await cache.publish(o)
        self.assertIsNotNone(cache.get_sync("coid-NATSFAIL"))

    async def test_publish_fresher_overwrites_older(self) -> None:
        cache = _make_cache()
        await _boot(cache)

        old = _make_obligation(
            client_order_id="coid-TS",
            status="ACTIVE",
            reserved_amount="100",
            last_update_ts=datetime(2026, 4, 8, 11, 0, 0, tzinfo=timezone.utc),
        )
        new = _make_obligation(
            client_order_id="coid-TS",
            status="PARTIALLY_CONSUMED",
            reserved_amount="100",
            consumed_amount="50",
            last_update_ts=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
        )
        await cache.publish(old)
        await cache.publish(new)

        cached = cache.get_sync("coid-TS")
        assert cached is not None
        self.assertEqual(cached.status, "PARTIALLY_CONSUMED")
        self.assertEqual(cached.consumed_amount, Decimal("50"))

    async def test_publish_stale_is_noop(self) -> None:
        """D9: publish 一个比本地更老的 obligation → 完全 noop。"""
        cache = _make_cache()
        store, _bus = await _boot(cache)

        new = _make_obligation(
            client_order_id="coid-STALE",
            status="PARTIALLY_CONSUMED",
            consumed_amount="50",
            last_update_ts=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
        )
        old = _make_obligation(
            client_order_id="coid-STALE",
            status="ACTIVE",
            consumed_amount="0",
            last_update_ts=datetime(2026, 4, 8, 11, 0, 0, tzinfo=timezone.utc),
        )
        await cache.publish(new)
        # 记录 publish new 之后的 index version
        index_after_new = await store.get(OBLIGATION_INDEX_KEY)
        assert isinstance(index_after_new, dict)
        version_after_new = index_after_new["version"]

        await cache.publish(old)
        # 本地仍然是 new
        cached = cache.get_sync("coid-STALE")
        assert cached is not None
        self.assertEqual(cached.status, "PARTIALLY_CONSUMED")
        # Index version 不变（noop 没递增）
        index_after_old = await store.get(OBLIGATION_INDEX_KEY)
        assert isinstance(index_after_old, dict)
        self.assertEqual(index_after_old["version"], version_after_new)

    async def test_publish_equal_ts_is_noop(self) -> None:
        cache = _make_cache()
        await _boot(cache)

        ts = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        o1 = _make_obligation(
            client_order_id="coid-EQ", reserved_amount="100", last_update_ts=ts
        )
        o2 = _make_obligation(
            client_order_id="coid-EQ",
            reserved_amount="999",  # 第二次有意改值
            last_update_ts=ts,  # 同 ts
        )
        await cache.publish(o1)
        await cache.publish(o2)

        # 本地仍是第一次的
        cached = cache.get_sync("coid-EQ")
        assert cached is not None
        self.assertEqual(cached.reserved_amount, Decimal("100"))


# ─────────────────────────────────────────────────────────────────────
# 读路径：active_sync / all_sync / get_sync
# ─────────────────────────────────────────────────────────────────────


class TestObligationHotStateCacheReadPath(unittest.IsolatedAsyncioTestCase):
    async def test_active_sync_filters_by_status(self) -> None:
        cache = _make_cache()
        await _boot(cache)
        await cache.publish(_make_obligation(client_order_id="a1", status="ACTIVE"))
        await cache.publish(_make_obligation(client_order_id="a2", status="PARTIALLY_CONSUMED"))
        await cache.publish(_make_obligation(client_order_id="a3", status="RELEASED"))
        await cache.publish(_make_obligation(client_order_id="a4", status="CANCELED"))
        await cache.publish(_make_obligation(client_order_id="a5", status="FAILED"))

        active = cache.active_sync()
        assert active is not None
        self.assertEqual({o.client_order_id for o in active}, {"a1", "a2"})

    async def test_all_sync_returns_every_status(self) -> None:
        cache = _make_cache()
        await _boot(cache)
        await cache.publish(_make_obligation(client_order_id="b1", status="ACTIVE"))
        await cache.publish(_make_obligation(client_order_id="b2", status="RELEASED"))

        all_o = cache.all_sync()
        assert all_o is not None
        self.assertEqual({o.client_order_id for o in all_o}, {"b1", "b2"})

    async def test_get_sync_miss_returns_none(self) -> None:
        cache = _make_cache()
        await _boot(cache)
        self.assertIsNone(cache.get_sync("does-not-exist"))


# ─────────────────────────────────────────────────────────────────────
# _handle_remote_event
# ─────────────────────────────────────────────────────────────────────


class TestObligationHotStateCacheRemoteEvent(unittest.IsolatedAsyncioTestCase):
    async def test_remote_event_applies_new_obligation(self) -> None:
        cache = _make_cache()
        await _boot(cache, process_role="decision")
        o = _make_obligation(client_order_id="coid-R1", status="ACTIVE")

        await cache._handle_remote_event(_make_remote_message(o))

        cached = cache.get_sync("coid-R1")
        assert cached is not None
        self.assertEqual(cached.status, "ACTIVE")

    async def test_remote_event_stale_is_noop(self) -> None:
        cache = _make_cache()
        await _boot(cache, process_role="decision")
        newer = _make_obligation(
            client_order_id="coid-R2",
            status="PARTIALLY_CONSUMED",
            last_update_ts=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
        )
        older = _make_obligation(
            client_order_id="coid-R2",
            status="ACTIVE",
            last_update_ts=datetime(2026, 4, 8, 11, 0, 0, tzinfo=timezone.utc),
        )
        await cache._handle_remote_event(_make_remote_message(newer))
        await cache._handle_remote_event(_make_remote_message(older))

        cached = cache.get_sync("coid-R2")
        assert cached is not None
        self.assertEqual(cached.status, "PARTIALLY_CONSUMED")

    async def test_remote_event_parse_failure_does_not_raise(self) -> None:
        cache = _make_cache()
        await _boot(cache)
        # envelope 里 payload 缺字段
        bad_message = {
            "topic": topics.OBLIGATION_UPDATES,
            "key": "bad",
            "payload": {
                "event_type": "OrderObligationUpdated",
                "source_component": "test",
                "topic": topics.OBLIGATION_UPDATES,
                "key": "bad",
                "payload": {"not": "an_obligation"},
                "source_timestamp": datetime.now(timezone.utc).isoformat(),
                "event_id": "test-evt-001",
            },
        }
        # 不抛
        await cache._handle_remote_event(bad_message)
        # 本地 dict 没被污染
        self.assertEqual(cache.snapshot()["cached_count"], 0)


# ─────────────────────────────────────────────────────────────────────
# apply_sync（sync path helper）
# ─────────────────────────────────────────────────────────────────────


class TestObligationHotStateCacheApplySync(unittest.TestCase):
    def test_apply_sync_updates_local_dict(self) -> None:
        cache = _make_cache()
        # 注意：apply_sync 不需要 bootstrap，这是 sync helper
        o = _make_obligation(client_order_id="coid-SYNC")
        cache.apply_sync(o)
        # 本地 dict 虽然 set 了，但 get_sync 在未 bootstrap 时返回 None
        self.assertIsNone(cache.get_sync("coid-SYNC"))
        # snapshot 里能看到 count=1
        self.assertEqual(cache.snapshot()["cached_count"], 1)

    def test_apply_sync_idempotent_same_ts(self) -> None:
        cache = _make_cache()
        ts = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        o1 = _make_obligation(
            client_order_id="coid-SYNC2", reserved_amount="10", last_update_ts=ts
        )
        o2 = _make_obligation(
            client_order_id="coid-SYNC2", reserved_amount="99", last_update_ts=ts
        )
        cache.apply_sync(o1)
        cache.apply_sync(o2)
        # 第二次被 noop，本地是 o1
        self.assertEqual(cache._latest["coid-SYNC2"].reserved_amount, Decimal("10"))


# ─────────────────────────────────────────────────────────────────────
# publish(skip_local=True) + fire_and_forget_publish（sync write path）
# ─────────────────────────────────────────────────────────────────────


class TestObligationHotStateCachePublishSkipLocal(unittest.IsolatedAsyncioTestCase):
    async def test_publish_skip_local_writes_redis_and_nats(self) -> None:
        """skip_local=True 跳过本地 apply，只走远端 best-effort。"""
        cache = _make_cache()
        store, bus = await _boot(cache)
        captured: list[Any] = []

        async def _rec(msg: Any) -> None:
            captured.append(msg)

        await bus.subscribe(topics.OBLIGATION_UPDATES, _rec)

        o = _make_obligation(client_order_id="coid-SK1")
        # 先手动 _apply_locally 模拟 fire_and_forget 的 eager apply
        cache._apply_locally(o)
        # 再调 publish(skip_local=True)
        await cache.publish(o, skip_local=True)

        # Redis per-coid 写入
        stored = await store.get(_obligation_key("coid-SK1"))
        self.assertIsNotNone(stored)
        # Index 写入
        index = await store.get(OBLIGATION_INDEX_KEY)
        assert isinstance(index, dict)
        self.assertEqual(index["all_coids"], ["coid-SK1"])
        # NATS 广播一次
        self.assertEqual(len(captured), 1)

    async def test_publish_skip_local_bypasses_stale_check(self) -> None:
        """skip_local=True 意图是让 fire_and_forget 专用，绕过 D9。"""
        cache = _make_cache()
        store, _bus = await _boot(cache)

        ts_old = datetime(2026, 4, 8, 11, 0, 0, tzinfo=timezone.utc)
        ts_new = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        old = _make_obligation(client_order_id="coid-SK2", last_update_ts=ts_old)
        new = _make_obligation(client_order_id="coid-SK2", last_update_ts=ts_new)

        # 先 publish 一个 newer 把本地 dict 建立起来
        await cache.publish(new)
        version_after_new = (await store.get(OBLIGATION_INDEX_KEY))["version"]

        # 然后以 skip_local=True 强制写 older
        # 本地 dict 不动（skip_local=True 跳过 _apply_locally），但 Redis +
        # NATS 会强制写 older —— 这是危险操作，注释里明确要求 caller 先
        # _apply_locally。本 test 验证 contract：skip_local 确实绕过了 D9。
        await cache.publish(old, skip_local=True)

        # 本地仍是 new（skip_local 跳过本地更新）
        cached = cache.get_sync("coid-SK2")
        assert cached is not None
        self.assertEqual(cached.last_update_ts, ts_new)
        # 但 index version 递增了（远端被更新）
        version_after_old = (await store.get(OBLIGATION_INDEX_KEY))["version"]
        self.assertEqual(version_after_old, version_after_new + 1)


class TestObligationHotStateCacheFireAndForget(unittest.IsolatedAsyncioTestCase):
    async def test_fire_and_forget_schedules_publish_in_running_loop(self) -> None:
        """在 async context 内同步调用 fire_and_forget_publish → eager apply +
        schedule async task 完成 Redis + NATS。"""
        cache = _make_cache()
        store, bus = await _boot(cache)
        captured: list[Any] = []

        async def _rec(msg: Any) -> None:
            captured.append(msg)

        await bus.subscribe(topics.OBLIGATION_UPDATES, _rec)

        o = _make_obligation(client_order_id="coid-FF1")
        # fire-and-forget，同步返回
        cache.fire_and_forget_publish(o)

        # 本地 dict 立即可见（eager apply）
        self.assertIsNotNone(cache.get_sync("coid-FF1"))

        # yield to scheduler 让后台 task 跑完
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Redis + NATS 已经被 scheduled task 写过了
        stored = await store.get(_obligation_key("coid-FF1"))
        self.assertIsNotNone(stored)
        self.assertEqual(len(captured), 1)

    async def test_fire_and_forget_stale_obligation_noop_full_chain(self) -> None:
        """eager apply 的 D9 stale 检查失败 → 完全不调度 task。"""
        cache = _make_cache()
        store, bus = await _boot(cache)
        captured: list[Any] = []

        async def _rec(msg: Any) -> None:
            captured.append(msg)

        await bus.subscribe(topics.OBLIGATION_UPDATES, _rec)

        ts_old = datetime(2026, 4, 8, 11, 0, 0, tzinfo=timezone.utc)
        ts_new = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        new = _make_obligation(client_order_id="coid-FF2", last_update_ts=ts_new)
        old = _make_obligation(client_order_id="coid-FF2", last_update_ts=ts_old)

        await cache.publish(new)
        captured.clear()
        version_before = (await store.get(OBLIGATION_INDEX_KEY))["version"]

        # old 应该 eager apply 失败 → 整个 fire_and_forget noop
        cache.fire_and_forget_publish(old)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # 无 NATS 广播
        self.assertEqual(len(captured), 0)
        # Index version 不变
        version_after = (await store.get(OBLIGATION_INDEX_KEY))["version"]
        self.assertEqual(version_after, version_before)

    async def test_fire_and_forget_none_obligation_is_noop(self) -> None:
        cache = _make_cache()
        await _boot(cache)
        # 不抛，什么都不做
        cache.fire_and_forget_publish(None)
        self.assertEqual(cache.snapshot()["cached_count"], 0)

    async def test_fire_and_forget_without_bootstrap_updates_local_only(self) -> None:
        """未 bootstrapped 的 cache: fire_and_forget 仍然会 eager apply 本地 dict。"""
        cache = _make_cache()
        # 故意不 bootstrap
        o = _make_obligation(client_order_id="coid-FF3")
        cache.fire_and_forget_publish(o)

        # 本地 dict 更新了（snapshot count=1），但 get_sync 在未 bootstrap 时
        # 仍返回 None 以便 caller fallback obligation_repo。
        self.assertEqual(cache.snapshot()["cached_count"], 1)
        self.assertIsNone(cache.get_sync("coid-FF3"))

    def test_fire_and_forget_outside_event_loop_is_safe(self) -> None:
        """纯 sync context（无 running loop）下调用 fire_and_forget → 只做 eager
        local apply，不抛异常、不调度 task。"""
        cache = _make_cache()
        # 未 bootstrap + 无 loop
        o = _make_obligation(client_order_id="coid-FF4")
        # 不抛
        cache.fire_and_forget_publish(o)
        # 本地 dict 有
        self.assertEqual(cache.snapshot()["cached_count"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
